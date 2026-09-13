"""P4-ADM-04 管线监控接口：任务看板、失败原因聚合、单任务重试。

复用 P1 任务模型（processing_job）与重试入口（asset_service.retry_asset）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_celery, get_session, require_admin
from loomvec.api.services import assets as asset_service
from loomvec.core.db.models import Asset, JobStatus, ProcessingJob

router = APIRouter(tags=["admin-pipeline"])


class JobOut(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID | None = None
    asset_name: str | None = None
    job_type: str
    status: str
    progress: float
    attempts: int
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


def _job_out(j: ProcessingJob, asset_name: str | None = None) -> dict[str, Any]:
    return JobOut(
        id=j.id,
        asset_id=j.asset_id,
        asset_name=asset_name,
        job_type=j.job_type.value,
        status=j.status.value,
        progress=j.progress,
        attempts=j.attempts,
        error=j.error,
        started_at=j.started_at,
        finished_at=j.finished_at,
    ).model_dump(mode="json")


@router.get("/pipeline/jobs")
async def list_jobs(
    status: str | None = Query(default=None),
    job_type: str | None = Query(default=None),
    asset_id: uuid.UUID | None = Query(default=None),
    mime_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(ProcessingJob, Asset.name).join(
        Asset, Asset.id == ProcessingJob.asset_id, isouter=True
    )
    if status:
        stmt = stmt.where(ProcessingJob.status == status)
    if job_type:
        stmt = stmt.where(ProcessingJob.job_type == job_type)
    if asset_id:
        stmt = stmt.where(ProcessingJob.asset_id == asset_id)
    if mime_type:
        stmt = stmt.where(Asset.mime_type == mime_type)
    total = (
        await session.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            stmt.order_by(ProcessingJob.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return {
        "items": [_job_out(j, name) for j, name in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/pipeline/failures")
async def failure_summary(
    limit: int = Query(default=10, ge=1, le=50),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """失败原因聚合 Top N（按错误前缀聚类，附最新样例资产）。"""
    rows = (
        await session.execute(
            select(ProcessingJob.error, func.count())
            .where(ProcessingJob.status == JobStatus.FAILED, ProcessingJob.error.isnot(None))
            .group_by(func.left(ProcessingJob.error, 120))
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "error_prefix": (prefix or "")[:200],
                "count": count,
            }
            for prefix, count in rows
        ]
    }


@router.get("/pipeline/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    job = (
        await session.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        from loomvec.core.errors import NotFoundError

        raise NotFoundError(resource="job", id=str(job_id))
    asset_name = await session.scalar(select(Asset.name).where(Asset.id == job.asset_id))
    # 同资产全部步骤（任务详情：各阶段耗时分解）
    siblings = (
        (
            await session.execute(
                select(ProcessingJob)
                .where(ProcessingJob.asset_id == job.asset_id)
                .order_by(ProcessingJob.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        **_job_out(job, asset_name),
        "asset_steps": [_job_out(j, asset_name) for j in siblings],
    }


class AdminRetryRequest(BaseModel):
    step: str | None = None


@router.post("/pipeline/jobs/{job_id}/retry", status_code=202)
async def retry_job(
    job_id: uuid.UUID,
    body: AdminRetryRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> dict[str, Any]:
    job = (
        await session.execute(select(ProcessingJob).where(ProcessingJob.id == job_id))
    ).scalar_one_or_none()
    if job is None:
        from loomvec.core.errors import NotFoundError

        raise NotFoundError(resource="job", id=str(job_id))
    await asset_service.retry_asset(session, celery=celery, asset_id=job.asset_id, step=body.step)
    return {"accepted": True, "asset_id": str(job.asset_id)}
