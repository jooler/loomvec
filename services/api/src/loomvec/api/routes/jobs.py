"""P1-API-05 任务查询与单步重跑（轮询用；SSE/Webhook 后置，事件已发 Redis）。

空间闸门：list 要求 viewer（含公共空间虚拟 viewer）；retry 要求可管理该资产的 editor+。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_celery, get_session, require_scope
from loomvec.api.identity import user_uuid
from loomvec.api.routes.assets import _load_asset_detail, _require_asset_manager
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
    await _load_asset_detail(session, asset_id, identity)
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
    """单步重跑：默认从首个失败步骤起跑（退出标准：失败可见原因 + 单步重试）。

    仅资产管理者（owner / 上传该资产的 editor；API Key 租户兜底同口径）。
    """
    access, asset = await _load_asset_detail(session, asset_id, identity)
    _require_asset_manager(
        access, asset, user_uuid(identity) if not identity.user_id.startswith("apikey:") else None
    )
    await asset_service.retry_asset(session, celery=celery, asset_id=asset_id, step=body.step)
    latest = (await ProcessingJobRepo(session).recent_for_asset(asset_id, limit=1))[0]
    return JobOut(
        id=latest.id,
        job_type=latest.job_type.value,
        status=latest.status.value,
        progress=latest.progress,
        attempts=latest.attempts,
    )
