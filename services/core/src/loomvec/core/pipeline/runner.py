"""P1-CORE-04 管线编排器：按步骤执行，失败记录任务行 + asset.failed + 事件。

- 每步独立提交（单步重跑的原子单元）；
- 失败：任务行记录原因、asset 置 failed、事件发布；步骤内 DB 错误走独立会话兜底；
- 事件经 Redis 发布（constants.EVENTS_CHANNEL），P4 SSE/Webhook 挂此扩展。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.constants import (
    EVENT_ASSET_FAILED,
    EVENT_ASSET_READY,
    EVENT_JOB_PROGRESS,
    EVENT_REVIEW_PENDING,
)
from loomvec.core.db.models import (
    Asset,
    AssetStatus,
    AssetVersion,
    JobStatus,
    JobType,
    ProcessingJob,
    ReviewStatus,
    Space,
)
from loomvec.core.errors import NotFoundError, ValidationError
from loomvec.core.events import publish_event
from loomvec.core.pipeline.base import LAST_STEP, PIPELINE_STEPS, PipelineDeps
from loomvec.core.pipeline.paper_meta import auto_rename_asset

logger = structlog.get_logger("loomvec.pipeline")


class PipelineRunner:
    """按步骤执行；每步独立提交，失败记录任务行 + asset.failed + 事件。"""

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    async def run(
        self, asset_id: uuid.UUID, from_step: str = "parse", attempt: int = 1
    ) -> list[dict]:
        if from_step not in PIPELINE_STEPS:
            raise ValidationError(f"未知步骤：{from_step}", steps=PIPELINE_STEPS)
        results: list[dict] = []
        for name in PIPELINE_STEPS[PIPELINE_STEPS.index(from_step) :]:
            results.append(await self._run_step(asset_id, name, attempt=attempt))
            if name == "chunk":
                # 须在 LAST_STEP 置 ready 之前：前端列表只在 processing 期间轮询
                await auto_rename_asset(self.deps, asset_id)
        return results

    async def _run_step(self, asset_id: uuid.UUID, step_name: str, attempt: int = 1) -> dict:
        deps = self.deps
        step = _step_instance(step_name, deps)
        now = datetime.now(UTC)

        session: AsyncSession = deps.session_factory()
        try:
            asset = (
                await session.execute(
                    select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if asset is None:
                raise NotFoundError(resource="asset", id=str(asset_id))
            version = (
                await session.execute(
                    select(AssetVersion)
                    .where(AssetVersion.asset_id == asset_id)
                    .order_by(AssetVersion.version.desc())
                    .limit(1)
                )
            ).scalar_one()
            if asset.status != AssetStatus.PROCESSING:
                asset.status = AssetStatus.PROCESSING
                asset.status_reason = None

            job = ProcessingJob(
                asset_id=asset_id,
                version_id=version.id,
                tenant_id=asset.tenant_id,
                job_type=JobType(step_name),
                status=JobStatus.RUNNING,
                attempts=attempt,
                started_at=now,
            )
            session.add(job)
            await session.commit()
            job_id = job.id
            meta: dict = {}
            try:
                meta = await step.run(session, asset, version)
            except Exception as e:
                detail = getattr(e, "details", None)
                error = f"{type(e).__name__}: {e}" + (f" {detail}" if detail else "")
                error = error[:4000]
                logger.error(
                    "pipeline_step_failed", asset_id=str(asset_id), step=step_name, error=error
                )
                try:
                    job.status = JobStatus.FAILED
                    job.error = error
                    job.finished_at = datetime.now(UTC)
                    asset.status = AssetStatus.FAILED
                    asset.status_reason = f"[{step_name}] {error}"[:2000]
                    await session.commit()
                except Exception:
                    await session.rollback()
                    await self._record_failure(asset_id, job_id, step_name, error)
                await self._publish(
                    {
                        "type": EVENT_ASSET_FAILED,
                        "asset_id": str(asset_id),
                        "job_type": step_name,
                        "error": error,
                    }
                )
                raise
            job.status = JobStatus.SUCCEEDED
            job.progress = 1.0
            job.finished_at = datetime.now(UTC)
            job.job_meta = meta
            if step_name == LAST_STEP:
                asset.status = AssetStatus.READY
                # P2-WRK-02 先审后见：review_required 空间的资产完成后进入待审，
                # 通过/驳回事件（API 审核接口）驱动可见性切换
                space = (
                    await session.execute(select(Space).where(Space.id == asset.space_id))
                ).scalar_one_or_none()
                if space is not None and space.review_required:
                    asset.review_status = ReviewStatus.PENDING_REVIEW
                    asset.review_reason = None
            await session.commit()
            await self._publish(
                {
                    "type": EVENT_JOB_PROGRESS,
                    "asset_id": str(asset_id),
                    "job_type": step_name,
                    "status": "succeeded",
                    "progress": 1.0,
                }
            )
            if step_name == LAST_STEP:
                await self._publish({"type": EVENT_ASSET_READY, "asset_id": str(asset_id)})
                if asset.review_status == ReviewStatus.PENDING_REVIEW:
                    await self._publish(
                        {
                            "type": EVENT_REVIEW_PENDING,
                            "asset_id": str(asset_id),
                            "space_id": str(asset.space_id),
                        }
                    )
            return meta
        finally:
            await session.close()

    async def _record_failure(
        self, asset_id: uuid.UUID, job_id: uuid.UUID, step_name: str, error: str
    ) -> None:
        """步骤内 DB 错误后的独立会话兜底记录。"""
        session: AsyncSession = self.deps.session_factory()
        try:
            job = (
                await session.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
            ).scalar_one_or_none()
            if job:
                job.status = JobStatus.FAILED
                job.error = error
                job.finished_at = datetime.now(UTC)
            asset = (
                await session.execute(select(Asset).where(Asset.id == asset_id))
            ).scalar_one_or_none()
            if asset:
                asset.status = AssetStatus.FAILED
                asset.status_reason = f"[{step_name}] {error}"[:2000]
            await session.commit()
        except Exception:
            await session.rollback()
            logger.critical("pipeline_failure_record_failed", asset_id=str(asset_id), error=error)
        finally:
            await session.close()

    async def _publish(self, event: dict[str, Any]) -> None:
        await publish_event(self.deps.redis, event)


def _step_instance(step_name: str, deps: PipelineDeps):
    """装配步骤实例（步骤注册表在 steps.py，避免循环导入）。"""
    from loomvec.core.pipeline.steps import STEP_CLASSES

    step = STEP_CLASSES[step_name]()
    step._deps = deps
    return step
