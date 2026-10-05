"""P1-WRK 管线执行器：依赖装配 + Celery 任务。

- `pipeline.process_asset`：整链（或从指定步骤）执行 parse→chunk→embed→index；
  上游不可用（MinerU/AI/Milvus）自动指数退避重试，最终失败进死信记录；
  跑完后自动归类知识库（_auto_assign_knowledge_bases，可新建 pending 节点）；
- `worker.heartbeat`：beat 每 30s 心跳写 Redis（存活与队列积压可观测）；
- `ingest.watch_scan`：beat 周期单层扫描监听目录，自动上传入库（worker/ingest_watch.py）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import redis as sync_redis
from celery import Task

from loomvec.core.ai import AiGateway
from loomvec.core.config import apply_ai_overrides, get_settings
from loomvec.core.constants import (
    DEAD_LETTER_KEY,
    DEAD_LETTER_MAX_LEN,
    HEARTBEAT_KEY,
    HEARTBEAT_TTL_SECONDS,
    QUEUE_PIPELINE,
    QUEUE_PIPELINE_LOW,
)
from loomvec.core.db.base import create_engine_and_sessionmaker
from loomvec.core.db.system_config import load_ai_overrides
from loomvec.core.errors import UpstreamUnavailableError
from loomvec.core.logging import get_logger
from loomvec.core.mineru_client import MineruClient
from loomvec.core.mineru_slots import MineruBusyError, MineruSlots
from loomvec.core.pipeline import PipelineDeps, PipelineRunner
from loomvec.core.retrieval import MilvusStore
from loomvec.core.storage import ObjectStorage
from loomvec.worker.celery_app import celery_app
from loomvec.worker.metrics import pipeline_jobs_total

logger = get_logger("loomvec.worker.tasks")


async def build_deps() -> PipelineDeps:
    """逐任务装配：engine/redis/httpx 绑定当前事件循环，任务结束 dispose（见 PipelineDeps）。

    AI 供方生效值在装配时读取：admin 动态配置（DB system_config）有非空值即
    覆盖 config/loomvec.json（core.config 三层配置语义）；DB 不可达时回落
    文件配置，不阻塞管线。
    """
    settings = get_settings()
    import redis.asyncio as aioredis

    redis = aioredis.from_url(settings.redis.url, decode_responses=True)
    # 名额租约 TTL 须覆盖一次完整解析（上传 + 等待 timeout_seconds + 下载），
    # 进程被杀时到期自动释放
    slots = MineruSlots(
        redis,
        capacity=settings.mineru.max_concurrency,
        ttl_seconds=int(settings.mineru.timeout_seconds) + 900,
    )
    engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
    try:
        async with session_factory() as session:
            ai_settings = apply_ai_overrides(settings.ai, await load_ai_overrides(session))
    except Exception:
        logger.warning("ai_overrides_load_failed", detail="DB 不可达，AI 供方回落文件配置")
        ai_settings = settings.ai
    return PipelineDeps(
        settings=settings,
        session_factory=session_factory,
        storage=ObjectStorage(settings.storage),
        ai=AiGateway(ai_settings),
        mineru=MineruClient(settings.mineru, slots=slots),
        milvus=MilvusStore(settings.milvus, ai_settings),
        redis=redis,
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
def process_asset(
    self,
    asset_id: str,
    from_step: str = "parse",
    chunk_strategy: str | None = None,
    graph_extraction: str | None = None,
) -> dict:
    """执行资产管线；from_step 支持单步重跑（retry 入口）。

    chunk_strategy/graph_extraction：本次运行覆盖（retry 请求带入，不落库），
    透传 PipelineRunner.run，由 chunk 步消费。
    """
    overrides = {"chunk_strategy": chunk_strategy, "graph_extraction": graph_extraction}

    async def _run() -> dict:
        deps = await build_deps()
        try:
            # attempts 观测口径：随 celery autoretry 递增（P4 已知限制修复）
            results = await PipelineRunner(deps).run(
                uuid.UUID(asset_id), from_step, attempt=self.request.retries + 1, **overrides
            )
            await _auto_assign_knowledge_bases(deps, asset_id)
            await _maybe_trigger_merge(deps, asset_id)
        finally:
            await _dispose_deps(deps)
        return {"asset_id": asset_id, "from_step": from_step, "steps": results}

    try:
        return asyncio.run(_run())
    except MineruBusyError:
        # MinerU 名额满：延后作为新任务重新入队（原队列；新任务 retries 从 0 计，
        # 不占 autoretry 次数），本任务正常结束、释放 worker 进程
        delay = get_settings().mineru.busy_retry_seconds
        queue = (self.request.delivery_info or {}).get("routing_key") or QUEUE_PIPELINE
        process_asset.apply_async(
            args=[asset_id, from_step], kwargs=overrides, queue=queue, countdown=delay
        )
        logger.info("mineru_busy_deferred", asset_id=asset_id, retry_in_seconds=delay)
        return {"asset_id": asset_id, "from_step": from_step, "deferred": "mineru_busy"}


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


# 自动归类采纳 LLM 结果（选中现有节点 / 建议新建节点）的最低置信度
_AUTO_TAG_MIN_CONFIDENCE = 0.75

_AUTO_TAG_SELECT_PROMPT = (
    "你是文献知识库分类器。从给定知识库目录中选择与文献直接相关的节点，"
    "不要选择仅因词面偶然出现的节点。"
)
_AUTO_TAG_NEW_PROMPT = (
    "优先复用现有知识库：只要目录中的节点（包括较宽泛的上级节点）能覆盖文献的核心主题，"
    "就不要建议新建；只有目录中没有任何节点能覆盖文献的核心主题时，才建议新建一个知识库，"
    "名称用简洁的学科或主题名（不超过 20 个字，不要带“相关研究”“文献”等泛化词），"
    "可从目录中选一个节点作为它的上级。"
)
_AUTO_TAG_TAGS_SCHEMA = '"tags": [{"id": "<目录中的 id>", "confidence": <0~1>}]'
_AUTO_TAG_NEW_SCHEMA = (
    '"new_knowledge_base": null 或 {"name": "<新知识库名>", '
    '"parent_id": "<目录中的上级 id 或 null>", "confidence": <0~1>, "reason": "<一句话理由>"}'
)


def _auto_tag_system_prompt(allow_new: bool) -> str:
    """allow_new=False（开关关闭 / pending 达上限）时不向 LLM 索要新知识库建议。"""
    if not allow_new:
        return f"{_AUTO_TAG_SELECT_PROMPT}输出 JSON：{{{_AUTO_TAG_TAGS_SCHEMA}}}。"
    return (
        f"{_AUTO_TAG_SELECT_PROMPT}{_AUTO_TAG_NEW_PROMPT}"
        f"输出 JSON：{{{_AUTO_TAG_TAGS_SCHEMA}, {_AUTO_TAG_NEW_SCHEMA}}}。"
    )


async def _create_suggested_knowledge_base(
    session, space, suggestion, catalog_ids: set[uuid.UUID], *, max_pending: int | None = None
) -> tuple[uuid.UUID, float] | None:
    """LLM 建议的新知识库 → create_system_tag（同名复用 / 合并重定向 / 新建 pending 节点）。

    建议不合法、置信度不足、同名节点曾被人工删除、或 pending 已达上限时返回 None。
    """
    from loomvec.core.knowledge_base_auto import create_system_tag

    if not isinstance(suggestion, dict):
        return None
    name = str(suggestion.get("name") or "").strip()
    try:
        confidence = float(suggestion.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    if not name or confidence < _AUTO_TAG_MIN_CONFIDENCE:
        return None
    try:
        parent_id = uuid.UUID(str(suggestion.get("parent_id")))
    except ValueError:
        parent_id = None
    tag = await create_system_tag(
        session,
        space=space,
        name=name,
        parent_id=parent_id if parent_id in catalog_ids else None,
        reason=str(suggestion.get("reason") or "")[:500] or None,
        max_pending=max_pending,
    )
    return (tag.id, confidence) if tag is not None else None


async def _auto_assign_knowledge_bases(deps: PipelineDeps, asset_id: str) -> None:
    """管线完成后按标题/摘要/正文给文献挂一个或多个知识库节点（docs/17「管线自动归类」）。

    有 LLM 时从当前空间知识库目录（含 pending 节点）选择节点；pipeline.auto_create_knowledge_bases
    打开且空间 pending 节点未达 pipeline.max_pending_knowledge_bases 时，目录中没有能覆盖
    文献核心主题的节点时 LLM 还可建议一个新知识库，经 core.knowledge_base_auto.create_system_tag
    落库（目录为空时同样走 LLM）。规则命中作为 LLM 不可用或无结果时的确定性兜底，只匹配现有
    节点、不新建。写关联前对所有候选节点按 id 升序一次性加 FOR KEY SHARE 锁复核（见
    core.knowledge_base_auto.resolve_live_tags）：已被合并的改挂合并目标，已删除的跳过。
    自动分类失败不影响已完成的解析和向量化。只写直接命中的节点。

    候选节点在加锁期间又被合并一次以上（多跳）时，新牵涉进来的节点 id 可能不在最初
    那条批量语句的排序范围内，理论上仍可能和一次跨多跳的人工合并构成死锁环（概率很
    低，resolve_live_tags 的文档有详细说明）；这类死锁 PG 会主动中止其中一个事务并
    报 SQLSTATE 40P01，重试一次即可自愈，不需要人工介入。
    """
    from sqlalchemy.exc import DBAPIError

    def _is_deadlock(exc: BaseException) -> bool:
        return isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) == "40P01"

    for attempt in range(2):
        try:
            await _auto_assign_knowledge_bases_once(deps, asset_id)
            return
        except Exception as e:
            if _is_deadlock(e) and attempt == 0:
                logger.info("knowledge_base_auto_classification_deadlock_retry", asset_id=asset_id)
                continue
            logger.warning(
                "knowledge_base_auto_classification_failed", asset_id=asset_id, error=str(e)
            )
            return


async def _auto_assign_knowledge_bases_once(deps: PipelineDeps, asset_id: str) -> None:
    from sqlalchemy import select

    from loomvec.core.db.models import Asset, AssetTag, AssetVersion, Space, Tag
    from loomvec.core.knowledge_base_auto import count_pending_tags, resolve_live_tags

    pipeline_cfg = deps.settings.pipeline
    async with deps.session_factory() as session:
        asset = (
            await session.execute(
                select(Asset).where(Asset.id == uuid.UUID(asset_id), Asset.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if asset is None:
            return
        tags = list(
            (
                await session.execute(
                    select(Tag).where(
                        Tag.deleted_at.is_(None),
                        Tag.tenant_id == asset.tenant_id,
                        (Tag.space_id == asset.space_id) | Tag.space_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        llm_available = deps.ai.llm_available()
        allow_new = (
            llm_available
            and pipeline_cfg.auto_create_knowledge_bases
            and await count_pending_tags(session, asset.space_id)
            < pipeline_cfg.max_pending_knowledge_bases
        )
        if not tags and not allow_new:
            return  # 无目录可匹配、也不允许新建
        version = (
            await session.execute(
                select(AssetVersion)
                .where(AssetVersion.asset_id == asset.id)
                .order_by(AssetVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        text_parts = [asset.name]
        pdf_info = asset.asset_meta.get("pdf_info") or {}
        text_parts.extend(str(pdf_info.get(k) or "") for k in ("title", "subject", "author"))
        if version is not None:
            parse_meta = (version.version_meta or {}).get("parse") or {}
            md_key = parse_meta.get("md_key")
            if md_key:
                text_parts.append(
                    (
                        await deps.storage.get_object(
                            deps.settings.storage.bucket_derived,
                            md_key,
                        )
                    ).decode("utf-8", errors="ignore")[:24000]
                )
        corpus = "\n".join(text_parts)
        chosen: dict[uuid.UUID, float] = {}
        suggestion = None
        catalog_ids = {tag.id for tag in tags}
        if llm_available:
            catalog = "\n".join(f"{tag.id}\t{tag.name}" for tag in tags) or "（空）"
            try:
                result = await deps.ai.complete_json(
                    [
                        {"role": "system", "content": _auto_tag_system_prompt(allow_new)},
                        {
                            "role": "user",
                            "content": f"知识库目录：\n{catalog}\n\n文献内容：\n{corpus}",
                        },
                    ],
                    temperature=0,
                    max_tokens=1000,
                )
                for item in result.get("tags") or []:
                    try:
                        tag_id = uuid.UUID(str(item.get("id")))
                        confidence = float(item.get("confidence", 0))
                    except (TypeError, ValueError):
                        continue
                    if tag_id in catalog_ids and confidence >= _AUTO_TAG_MIN_CONFIDENCE:
                        chosen[tag_id] = confidence
                if allow_new:
                    suggestion = result.get("new_knowledge_base")
            except Exception as e:
                logger.info(
                    "knowledge_base_llm_classification_fallback",
                    asset_id=asset_id,
                    error=str(e),
                )
        if suggestion is not None:
            try:
                space = await session.get(Space, asset.space_id)
                created = await _create_suggested_knowledge_base(
                    session,
                    space,
                    suggestion,
                    catalog_ids,
                    max_pending=pipeline_cfg.max_pending_knowledge_bases,
                )
                if created is not None:
                    chosen.setdefault(*created)
            except Exception as e:
                logger.warning("knowledge_base_auto_create_failed", asset_id=asset_id, error=str(e))
        if not chosen:
            folded = corpus.casefold()
            chosen = {
                tag.id: 1.0 for tag in tags if tag.name.strip() and tag.name.casefold() in folded
            }
        # 写关联前复核目标节点（FOR KEY SHARE 持有到提交，与人工 merge 的 FOR UPDATE
        # 互斥，但不与 merge 改挂子节点 parent_id 的 UPDATE 互斥；按 id 升序批量加锁，
        # 顺序与 merge 一致，避免两边各自加锁顺序不同造成死锁，细节见 resolve_live_tags）
        live_tags = await resolve_live_tags(session, list(chosen), lock=True)
        resolved: dict[uuid.UUID, float] = {}
        for tag_id, confidence in chosen.items():
            live = live_tags.get(tag_id)
            if live is not None:
                resolved.setdefault(live.id, confidence)
        existing = {
            row.tag_id
            for row in (
                await session.execute(select(AssetTag).where(AssetTag.asset_id == asset.id))
            )
            .scalars()
            .all()
        }
        for tag_id, confidence in resolved.items():
            if tag_id not in existing:
                session.add(
                    AssetTag(
                        tenant_id=asset.tenant_id,
                        asset_id=asset.id,
                        tag_id=tag_id,
                        source="auto",
                        confidence=confidence,
                    )
                )
        await session.commit()


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

        deps = await build_deps()
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

        deps = await build_deps()
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

        deps = await build_deps()
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

        deps = await build_deps()
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

        deps = await build_deps()
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

        deps = await build_deps()
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


# ---------------------------------------------------------------------------
# 目录监听自动入库（beat 周期任务；实现见 worker/ingest_watch.py）
# ---------------------------------------------------------------------------


@celery_app.task(name="ingest.watch_scan")
def ingest_watch_scan() -> dict:
    """单层扫描 ingest_watch.dir：稳定的新文件上传登记并入队管线；dir 为空时 no-op。"""
    if not get_settings().ingest_watch.dir:
        return {"status": "disabled"}

    async def _run() -> dict:
        from loomvec.worker.ingest_watch import scan_watch_dir

        deps = await build_deps()
        try:
            return await scan_watch_dir(deps)
        finally:
            await _dispose_deps(deps)

    return asyncio.run(_run())
