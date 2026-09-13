"""P1-API-05 任务查询与单步重跑（轮询用；SSE/Webhook 后置，事件已发 Redis）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_celery, get_session, require_scope
from loomvec.api.schemas.assets import JobOut, RetryRequest
from loomvec.api.services import assets as asset_service
from loomvec.core.db.repos import ProcessingJobRepo

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def _job_out(j) -> JobOut:
    return JobOut(
        id=j.id,
        job_type=j.job_type.value,
        status=j.status.value,
        progress=j.progress,
        attempts=j.attempts,
        error=j.error,
        job_meta=j.job_meta,
        started_at=j.started_at,
        finished_at=j.finished_at,
    )


@router.get("/assets/{asset_id}/jobs", response_model=list[JobOut])
async def list_jobs(
    asset_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> list[JobOut]:
    jobs = await ProcessingJobRepo(session).recent_for_asset(asset_id)
    return [_job_out(j) for j in jobs]


@router.post("/assets/{asset_id}/retry", response_model=JobOut, status_code=202)
async def retry_asset(
    asset_id: uuid.UUID,
    body: RetryRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> JobOut:
    """单步重跑：默认从首个失败步骤起跑（退出标准：失败可见原因 + 单步重试）。"""
    await asset_service.retry_asset(session, celery=celery, asset_id=asset_id, step=body.step)
    latest = (await ProcessingJobRepo(session).recent_for_asset(asset_id, limit=1))[0]
    return JobOut(
        id=latest.id,
        job_type=latest.job_type.value,
        status=latest.status.value,
        progress=latest.progress,
        attempts=latest.attempts,
    )
