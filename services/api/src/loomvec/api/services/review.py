"""P2-API-04 审核服务：待审队列、通过/驳回（理由必填、通知与事件）。

可见性语义（P2-WRK-02）：review_required 空间的资产管线完成后置
pending_review；通过 → approved（viewer 可见）；驳回 → rejected（对 viewer
下架）并通知上传者。审核操作 owner 专属；队列查看 editor+。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.services import notifications as notification_service
from loomvec.core.db.models import Asset, ReviewStatus, Space
from loomvec.core.db.repos import AssetRepo
from loomvec.core.errors import NotFoundError, ValidationError

logger = structlog.get_logger("loomvec.api.review")


async def review_queue(session: AsyncSession, *, space: Space, limit: int = 50) -> list[Asset]:
    """待审队列：pending_review 资产按提交时间升序（先到先审）。"""
    stmt = (
        select(Asset)
        .where(
            Asset.space_id == space.id,
            Asset.deleted_at.is_(None),
            Asset.review_status == ReviewStatus.PENDING_REVIEW,
        )
        .order_by(Asset.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def decide_review(
    session: AsyncSession,
    *,
    reviewer_id: uuid.UUID | None,
    redis,
    space: Space,
    asset_id: uuid.UUID,
    action: str,
    reason: str | None,
) -> Asset:
    """通过/驳回：驳回理由必填；结果通知上传者并广播事件。"""
    from loomvec.core.constants import (
        EVENT_REVIEW_APPROVED,
        EVENT_REVIEW_REJECTED,
        NOTIFY_REVIEW_APPROVED,
        NOTIFY_REVIEW_REJECTED,
    )
    from loomvec.core.events import publish_event

    if action not in ("approve", "reject"):
        raise ValidationError("action 仅支持 approve/reject", got=action)
    if action == "reject" and not (reason and reason.strip()):
        raise ValidationError("驳回必须填写理由")

    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None or asset.space_id != space.id:
        raise NotFoundError(resource="asset", id=str(asset_id))
    if asset.review_status == ReviewStatus.APPROVED and action == "approve":
        raise ValidationError("资产已通过审核")

    if action == "approve":
        asset.review_status = ReviewStatus.APPROVED
        asset.review_reason = None
    else:
        asset.review_status = ReviewStatus.REJECTED
        asset.review_reason = reason
    asset.reviewed_by = reviewer_id
    asset.reviewed_at = datetime.now(UTC)
    await session.flush()

    approved = action == "approve"
    await notification_service.notify(
        session,
        user_id=asset.created_by,
        tenant_id=space.tenant_id,
        type_=NOTIFY_REVIEW_APPROVED if approved else NOTIFY_REVIEW_REJECTED,
        title=(
            f"资产「{asset.name}」已通过审核"
            if approved
            else f"资产「{asset.name}」被驳回：{reason}"
        ),
        payload={"asset_id": str(asset.id), "space_id": str(space.id), "reason": reason},
    )
    await session.commit()
    await publish_event(
        redis,
        {
            "type": EVENT_REVIEW_APPROVED if approved else EVENT_REVIEW_REJECTED,
            "asset_id": str(asset.id),
            "space_id": str(space.id),
            "reason": reason,
        },
    )
    return asset


def review_asset_out(asset: Asset) -> dict:
    return {
        "id": asset.id,
        "name": asset.name,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "status": asset.status.value,
        "review_status": asset.review_status.value if asset.review_status else None,
        "review_reason": asset.review_reason,
        "created_by": asset.created_by,
        "created_at": asset.created_at,
    }
