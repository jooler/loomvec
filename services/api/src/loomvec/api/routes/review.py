"""P2-API-04 审核接口：待审队列、通过/驳回（理由必填、通知事件）。

能力矩阵：队列查看 editor+；通过/驳回 owner。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_redis,
    get_session,
    require_scope,
    require_space,
    resolve_space_access,
)
from loomvec.api.services import review as review_service
from loomvec.core.authz import SpaceAccess
from loomvec.core.db.models import SpaceRole

router = APIRouter(prefix="/api/v1", tags=["review"])


class ReviewAssetOut(BaseModel):
    id: uuid.UUID
    name: str
    mime_type: str
    size_bytes: int
    status: str
    review_status: str | None
    review_reason: str | None
    created_by: uuid.UUID | None
    created_at: datetime


class ReviewQueueOut(BaseModel):
    items: list[ReviewAssetOut]


class ReviewDecisionRequest(BaseModel):
    action: str = Field(description="approve / reject")
    reason: str | None = Field(default=None, max_length=2000)


@router.get("/spaces/{space_id}/review/queue", response_model=ReviewQueueOut)
async def review_queue(
    access: SpaceAccess = Depends(require_space(SpaceRole.EDITOR)),
    session: AsyncSession = Depends(get_session),
) -> ReviewQueueOut:
    rows = await review_service.review_queue(session, space=access.space)
    items = [ReviewAssetOut(**review_service.review_asset_out(a)) for a in rows]
    return ReviewQueueOut(items=items)


@router.post("/assets/{asset_id}/review", response_model=ReviewAssetOut)
async def decide_review(
    asset_id: uuid.UUID,
    body: ReviewDecisionRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
) -> ReviewAssetOut:
    """通过/驳回资产审核（owner；驳回理由必填并通知上传者）。

    路径不在 /spaces/{space_id} 下：先定位资产所属空间再做 owner 闸门
    （require_space 依赖仅适用于含 {space_id} 路径参数的路由）。
    """
    from loomvec.core.db.repos import AssetRepo
    from loomvec.core.errors import NotFoundError

    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))
    access = await resolve_space_access(session, identity, asset.space_id, SpaceRole.OWNER)
    asset = await review_service.decide_review(
        session,
        reviewer_id=access.member.user_id if access.member else None,
        redis=redis,
        space=access.space,
        asset_id=asset_id,
        action=body.action,
        reason=body.reason,
    )
    return ReviewAssetOut(**review_service.review_asset_out(asset))
