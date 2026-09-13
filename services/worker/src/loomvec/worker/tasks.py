"""P1-WRK 管线执行器：依赖装配 + Celery 任务。

- `pipeline.process_asset`：整链（或从指定步骤）执行 parse→chunk→embed→index；
  上游不可用（MinerU/AI/Milvus）自动指数退避重试，最终失败进死信记录；
- `worker.heartbeat`：beat 每 30s 心跳写 Redis（存活与队列积压可观测）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import redis as sync_redis
from celery import Task

from loomvec.core.ai import AiGateway
from loomvec.core.config import get_settings
from loomvec.core.constants import (
    DEAD_LETTER_KEY,
    DEAD_LETTER_MAX_LEN,
    HEARTBEAT_KEY,
    HEARTBEAT_TTL_SECONDS,
    QUEUE_PIPELINE_LOW,
)
from loomvec.core.db.base import create_engine_and_sessionmaker
from loomvec.core.errors import UpstreamUnavailableError
from loomvec.core.logging import get_logger
from loomvec.core.mineru_client import MineruClient
from loomvec.core.pipeline import PipelineDeps, PipelineRunner
from loomvec.core.retrieval import MilvusStore
from loomvec.core.storage import ObjectStorage
from loomvec.worker.celery_app import celery_app
from loomvec.worker.metrics import pipeline_jobs_total

logger = get_logger("loomvec.worker.tasks")


def build_deps() -> PipelineDeps:
    """逐任务装配：engine/redis/httpx 绑定当前事件循环，任务结束 dispose（见 PipelineDeps）。"""
    settings = get_settings()
    import redis.asyncio as aioredis

    engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
    return PipelineDeps(
        settings=settings,
        session_factory=session_factory,
        storage=ObjectStorage(settings.storage),
        ai=AiGateway(settings.ai),
        mineru=MineruClient(settings.mineru),
        milvus=MilvusStore(settings.milvus, settings.ai),
        redis=aioredis.from_url(settings.redis.url, decode_responses=True),
        engine=engine,
    )


async def _dispose_deps(deps: PipelineDeps) -> None:
    if deps.redis is not None:
        await deps.redis.aclose()
    if deps.engine is not None:
        await deps.engine.dispose()


class PipelineTask(Task):
    """管线任务基类：结果计数 + 最终失败写死信记录（P1-WRK-01）。"""

    def __call__(self, *args, **kwargs):
        from celery.exceptions import Retry

        try:
            result = super().__call__(*args, **kwargs)
        except Retry:
            pipeline_jobs_total.labels(status="retrying").inc()
            raise
        except Exception:
            pipeline_jobs_total.labels(status="failed").inc()
            raise
        pipeline_jobs_total.labels(status="succeeded").inc()
        return result

    def on_failure(self, exc, task_id, args, kwargs, einfo) -> None:
        try:
            client = sync_redis.from_url(get_settings().redis.url, decode_responses=True)
            client.lpush(
                DEAD_LETTER_KEY,
                json.dumps(
                    {
                        "task": self.name,
                        "task_id": task_id,
                        "args": args,
                        "kwargs": kwargs,
                        "error": str(exc)[:2000],
                        "failed_at": time.time(),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            )
            # 只保留最近 DEAD_LETTER_MAX_LEN 条
            client.ltrim(DEAD_LETTER_KEY, 0, DEAD_LETTER_MAX_LEN - 1)
            client.close()
        except Exception:  # 死信记录失败不影响任务语义
            pass
        return super().on_failure(exc, task_id, args, kwargs, einfo)


@celery_app.task(
    name="pipeline.process_asset",
    bind=True,
    base=PipelineTask,
    autoretry_for=(UpstreamUnavailableError,),
    retry_backoff=10,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def process_asset(self, asset_id: str, from_step: str = "parse") -> dict:
    """执行资产管线；from_step 支持单步重跑（retry 入口）。"""

    async def _run() -> dict:
        deps = build_deps()
        try:
            # attempts 观测口径：随 celery autoretry 递增（P4 已知限制修复）
            results = await PipelineRunner(deps).run(
                uuid.UUID(asset_id), from_step, attempt=self.request.retries + 1
            )
            await _maybe_trigger_merge(deps, asset_id)
        finally:
            await _dispose_deps(deps)
        return {"asset_id": asset_id, "from_step": from_step, "steps": results}

    return asyncio.run(_run())


async def _maybe_trigger_merge(deps: PipelineDeps, asset_id: str) -> None:
    """P3-CORE-03 触发判定：图写入后空间实体增量达阈值 → 低优队列合并任务。"""
    from sqlalchemy import select

    from loomvec.core.db.models import Asset
    from loomvec.core.graph.merge import should_trigger_merge

    try:
        async with deps.session_factory() as session:
            asset = (
                await session.execute(select(Asset).where(Asset.id == uuid.UUID(asset_id)))
            ).scalar_one_or_none()
            if asset is None:
                return
            if await should_trigger_merge(session, deps.settings, asset.space_id):
                celery_app.send_task(
                    "graph.merge_space",
                    args=[str(asset.space_id)],
                    queue=QUEUE_PIPELINE_LOW,
                )
    except Exception as e:
        logger.warning("merge_trigger_check_failed", asset_id=asset_id, error=str(e))


@celery_app.task(name="worker.ping")
def ping() -> dict:
    """存活/链路验证：broker 收发 + result 回读。"""
    return {"status": "ok"}


@celery_app.task(name="worker.heartbeat")
def heartbeat() -> dict:
    """worker 心跳：写 Redis 时间戳（供 readyz/运营端观测）。"""
    try:
        client = sync_redis.from_url(get_settings().redis.url, decode_responses=True)
        client.set(HEARTBEAT_KEY, json.dumps({"ts": time.time(), "pid": __import__("os").getpid()}))
        client.expire(HEARTBEAT_KEY, HEARTBEAT_TTL_SECONDS)
        client.close()
    except Exception:
        pass
    return {"status": "ok"}


@celery_app.task(name="pipeline.reembed_space")
def reembed_space(task_id: str) -> dict:
    """重嵌入任务（P4-ADM-05）：重置空间单元版本 → 逐资产重跑 embed/index →
    原子切换 space.embedding_model。

    过渡语义：检索期间旧向量继续服务（单元向量自带 model_version），
    完成后一次切换空间模型字段；单资产失败不中断整体（计 failed_assets）。
    取消语义：运营将任务置 FAILED 后本任务在下一资产前协作停止。
    """

    async def _run() -> dict:
        from datetime import UTC, datetime

        from sqlalchemy import select

        from loomvec.core.db.models import (
            Asset,
            ReembedTask,
            ReembedTaskStatus,
            SemanticUnit,
            Space,
        )

        deps = build_deps()
        try:
            async with deps.session_factory() as session:
                task_row = (
                    await session.execute(
                        select(ReembedTask).where(ReembedTask.id == uuid.UUID(task_id))
                    )
                ).scalar_one_or_none()
                if task_row is None or task_row.status == ReembedTaskStatus.FAILED:
                    return {"status": "cancelled", "task_id": task_id}
                space = (
                    await session.execute(
                        select(Space).where(
                            Space.id == task_row.space_id, Space.deleted_at.is_(None)
                        )
                    )
                ).scalar_one_or_none()
                if space is None:
                    task_row.status = ReembedTaskStatus.FAILED
                    task_row.error = "空间不存在或已删除"
                    await session.commit()
                    return {"status": "failed", "reason": "space missing"}

                task_row.status = ReembedTaskStatus.RUNNING
                # 重置版本 → embed 步骤幂等检查失效，强制重嵌入
                from sqlalchemy import update

                await session.execute(
                    update(SemanticUnit)
                    .where(SemanticUnit.space_id == space.id)
                    .values(embed_model_version=None)
                )
                asset_ids = (
                    (
                        await session.execute(
                            select(Asset.id).where(
                                Asset.space_id == space.id, Asset.deleted_at.is_(None)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                task_row.total_assets = len(asset_ids)
                await session.commit()

            runner = PipelineRunner(deps)
            done = failed = 0
            for aid in asset_ids:
                async with deps.session_factory() as session:
                    task_row = (
                        await session.execute(
                            select(ReembedTask).where(ReembedTask.id == uuid.UUID(task_id))
                        )
                    ).scalar_one_or_none()
                    if task_row is None or task_row.status == ReembedTaskStatus.FAILED:
                        return {"status": "cancelled", "done": done, "failed": failed}
                try:
                    await runner.run(aid, from_step="embed")
                    done += 1
                except Exception as e:  # 单资产失败不中断整体
                    logger.warning("reembed_asset_failed", asset_id=str(aid), error=str(e))
                    failed += 1
                async with deps.session_factory() as session:
                    task_row = (
                        await session.execute(
                            select(ReembedTask).where(ReembedTask.id == uuid.UUID(task_id))
                        )
                    ).scalar_one_or_none()
                    if task_row is not None:
                        task_row.done_assets = done
                        task_row.failed_assets = failed
                        await session.commit()

            async with deps.session_factory() as session:
                task_row = (
                    await session.execute(
                        select(ReembedTask).where(ReembedTask.id == uuid.UUID(task_id))
                    )
                ).scalar_one_or_none()
                space = (
                    await session.execute(select(Space).where(Space.id == task_row.space_id))
                ).scalar_one_or_none()
                if task_row is not None:
                    task_row.status = ReembedTaskStatus.SUCCEEDED
                    task_row.done_assets = done
                    task_row.failed_assets = failed
                    task_row.updated_at = datetime.now(UTC)
                if space is not None:
                    space.embedding_model = task_row.target_model  # 原子切换
                await session.commit()
            return {"status": "succeeded", "done": done, "failed": failed}
        finally:
            await _dispose_deps(deps)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# P3-CORE-03 / P3-WRK-02 空间级周期任务（低优先级队列）
# ---------------------------------------------------------------------------


@celery_app.task(name="graph.merge_scan")
def merge_scan() -> dict:
    """扫描全部空间：实体增量 ≥ 阈值者入队空间合并任务。"""

    async def _run() -> dict:
        from sqlalchemy import select

        from loomvec.core.db.models import Space
        from loomvec.core.graph.merge import should_trigger_merge

        deps = build_deps()
        try:
            async with deps.session_factory() as session:
                space_ids = (
                    (await session.execute(select(Space.id).where(Space.deleted_at.is_(None))))
                    .scalars()
                    .all()
                )
                due = []
                for sid in space_ids:
                    if await should_trigger_merge(session, deps.settings, sid):
                        due.append(str(sid))
            for sid in due:
                celery_app.send_task("graph.merge_space", args=[sid], queue=QUEUE_PIPELINE_LOW)
            return {"scanned": len(space_ids), "triggered": len(due)}
        finally:
            await _dispose_deps(deps)

    return asyncio.run(_run())


@celery_app.task(name="graph.merge_space")
def merge_space(space_id: str, created_by: str = "system") -> dict:
    """空间实体合并（P3-CORE-03 阶段二）：候选发现 → 逐对合并 + merge_log。"""

    async def _run() -> dict:

        from loomvec.core.graph.merge import run_space_merge

        deps = build_deps()
        try:

            async def vector_fetch(entities):
                _ = entities
                return await deps.milvus.async_fetch_entity_vectors(uuid.UUID(space_id))

            async with deps.session_factory() as session:
                result = await run_space_merge(
                    session,
                    deps.settings,
                    uuid.UUID(space_id),
                    vector_fetch=vector_fetch,
                    created_by=created_by,
                )
                await session.commit()
            return {"space_id": space_id, **result}
        finally:
            await _dispose_deps(deps)

    return asyncio.run(_run())


@celery_app.task(name="graph.community_scan")
def community_scan() -> dict:
    """扫描全部空间（有实体者）入队社区任务。"""

    async def _run() -> dict:
        from sqlalchemy import func, select

        from loomvec.core.db.models import Entity, Space

        deps = build_deps()
        try:
            async with deps.session_factory() as session:
                rows = (
                    await session.execute(
                        select(Entity.space_id, func.count())
                        .where(Entity.deleted_at.is_(None), Entity.merged_into.is_(None))
                        .join(Space, Space.id == Entity.space_id)
                        .where(Space.deleted_at.is_(None))
                        .group_by(Entity.space_id)
                        .having(func.count() >= 3)
                    )
                ).all()
            for sid, _cnt in rows:
                celery_app.send_task(
                    "graph.community_space", args=[str(sid)], queue=QUEUE_PIPELINE_LOW
                )
            return {"spaces": len(rows)}
        finally:
            await _dispose_deps(deps)

    return asyncio.run(_run())


@celery_app.task(name="graph.community_space")
def community_space(space_id: str) -> dict:
    """空间社区任务（P3-WRK-02）：Leiden 检测 + LLM 摘要 → community 单元入 Milvus。"""

    async def _run() -> dict:
        from sqlalchemy import select

        from loomvec.core.db.models import Community, Space
        from loomvec.core.graph.communities import (
            community_summary_messages,
            community_unit_row,
            run_space_communities,
        )

        deps = build_deps()
        try:
            async with deps.session_factory() as session:
                space = (
                    await session.execute(
                        select(Space).where(
                            Space.id == uuid.UUID(space_id), Space.deleted_at.is_(None)
                        )
                    )
                ).scalar_one_or_none()
                if space is None:
                    return {"space_id": space_id, "skipped": "space_missing"}

                async def summarize(community: Community, members):
                    messages = community_summary_messages(community, members)
                    data = await deps.ai.complete_json(messages)
                    return str(data.get("summary") or "")

                result = await run_space_communities(
                    session,
                    deps.settings,
                    uuid.UUID(space_id),
                    space.tenant_id,
                    summarize=summarize,
                )
                await session.commit()

            # 摘要单元重写（先清后写，幂等）；无摘要的社区跳过
            communities = (
                (
                    await session.execute(
                        select(Community).where(
                            Community.space_id == uuid.UUID(space_id),
                            Community.summary_status == "ready",
                        )
                    )
                )
                .scalars()
                .all()
            )
            written = 0
            if communities:
                await deps.milvus.async_ensure_collection()
                await deps.milvus.async_delete_community_units(uuid.UUID(space_id))
                model_version = (
                    "mock" if deps.settings.ai.mock else (deps.settings.ai.llm.model or "unknown")
                )
                vectors = await deps.ai.embed([c.summary or c.label for c in communities])
                rows = [
                    community_unit_row(
                        c,
                        v,
                        model_version=model_version,
                        text_dim=deps.milvus.text_dim,
                        clip_dim=deps.milvus.clip_dim,
                    )
                    for c, v in zip(communities, vectors, strict=True)
                ]
                await deps.milvus.async_upsert_units(rows)
                written = len(rows)
            return {"space_id": space_id, "community_units": written, **result}
        finally:
            await _dispose_deps(deps)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# P3-WRK-05 懒转码
# ---------------------------------------------------------------------------


@celery_app.task(
    name="media.transcode",
    bind=True,
    base=PipelineTask,
    autoretry_for=(UpstreamUnavailableError,),
    retry_backoff=10,
    max_retries=2,
)
def transcode_asset(self, asset_id: str) -> dict:
    """懒转码（首次播放触发）：ffmpeg → H.264/AAC 预览，产物入 asset_rendition。"""

    async def _run() -> dict:
        import tempfile
        from datetime import UTC, datetime
        from pathlib import Path

        from sqlalchemy import select

        from loomvec.core import transcode as tc
        from loomvec.core.db.models import (
            Asset,
            AssetRendition,
            AssetVersion,
            JobStatus,
            JobType,
            ProcessingJob,
            RenditionKind,
        )
        from loomvec.core.storage_keys import transcode_key

        deps = build_deps()
        settings = deps.settings
        try:
            async with deps.session_factory() as session:
                asset = (
                    await session.execute(select(Asset).where(Asset.id == uuid.UUID(asset_id)))
                ).scalar_one_or_none()
                if asset is None:
                    return {"status": "missing_asset"}
                version = (
                    (
                        await session.execute(
                            select(AssetVersion)
                            .where(AssetVersion.asset_id == asset.id)
                            .order_by(AssetVersion.version.desc())
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
                if version is None or not version.storage_key:
                    return {"status": "missing_source"}
                existing = (
                    await session.execute(
                        select(AssetRendition).where(
                            AssetRendition.asset_id == asset.id,
                            AssetRendition.version_id == version.id,
                            AssetRendition.kind == RenditionKind.TRANSCODE,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return {"status": "cached", "rendition_id": str(existing.id)}
                # 任务行（播放接口以此判定转码进行中）
                job = ProcessingJob(
                    asset_id=asset.id,
                    version_id=version.id,
                    tenant_id=asset.tenant_id,
                    job_type=JobType.TRANSCODE,
                    status=JobStatus.RUNNING,
                    attempts=self.request.retries + 1,
                    started_at=datetime.now(UTC),
                )
                session.add(job)
                await session.commit()
                job_id = job.id

            media = settings.media
            await tc.write_progress(deps.redis, asset_id, 0.0, "running")
            with tempfile.TemporaryDirectory(prefix="loomvec-transcode-") as tmp:
                src = Path(tmp) / f"source{Path(asset.name).suffix or '.bin'}"
                out = Path(tmp) / "preview.mp4"
                src.write_bytes(
                    await deps.storage.get_object(settings.storage.bucket_raw, version.storage_key)
                )

                async def on_progress(p: float) -> None:
                    await tc.write_progress(deps.redis, asset_id, p, "running")

                await tc.transcode_preview(
                    src,
                    out,
                    height=media.transcode_height,
                    crf=media.transcode_crf,
                    preset=media.transcode_preset,
                    timeout=media.transcode_timeout_seconds,
                    on_progress=on_progress,
                )
                payload = out.read_bytes()
                key = transcode_key(asset.id, version.version)
                await deps.storage.put_object(
                    settings.storage.bucket_derived,
                    key,
                    payload,
                    content_type="video/mp4",
                )

            async with deps.session_factory() as session:
                rendition = AssetRendition(
                    tenant_id=asset.tenant_id,
                    asset_id=asset.id,
                    version_id=version.id,
                    kind=RenditionKind.TRANSCODE,
                    storage_key=key,
                    mime_type="video/mp4",
                    size_bytes=len(payload),
                )
                session.add(rendition)
                job = (
                    await session.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
                ).scalar_one_or_none()
                if job is not None:
                    job.status = JobStatus.SUCCEEDED
                    job.progress = 1.0
                    job.finished_at = datetime.now(UTC)
                await session.commit()
                result = {"status": "ready", "rendition_id": str(rendition.id)}
            await tc.write_progress(deps.redis, asset_id, 1.0, "ready")
            return result
        except Exception:
            await tc.write_progress(deps.redis, asset_id, 0.0, "failed")
            raise
        finally:
            await _dispose_deps(deps)

    return asyncio.run(_run())
