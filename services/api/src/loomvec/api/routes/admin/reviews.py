"""P4-API-04 运营接口：跨空间审核队列、下架（连带向量清理）、chunk 复核、批量重跑。

下架语义（docs/04 §5.5）：管理端下架 = rejected + 向量删除（连带清理），
区别于空间级驳回（reviewer 流程）。批量重跑任务化（逐资产派发管线）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_celery, get_redis, get_session, require_admin
from loomvec.core.constants import QUEUE_PIPELINE
from loomvec.core.db.models import (
    Asset,
    AssetStatus,
    ReviewStatus,
    SemanticUnit,
    Space,
)
from loomvec.core.db.repos import AssetRepo
from loomvec.core.errors import NotFoundError, ValidationError

router = APIRouter(tags=["admin-reviews"])


class DecideRequest(BaseModel):
    action: str = Field(description="approve / reject")
    reason: str | None = Field(default=None, max_length=1000)


class TakedownRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class BatchRerunRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    from_step: str = Field(default="parse")


def _asset_out(a: Asset, space_slug: str | None = None) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "space_id": str(a.space_id),
        "space_slug": space_slug,
        "mime_type": a.mime_type,
        "size_bytes": a.size_bytes,
        "status": a.status.value,
        "review_status": a.review_status.value if a.review_status else None,
        "review_reason": a.review_reason,
        "created_by": str(a.created_by) if a.created_by else None,
        "created_at": a.created_at.isoformat(),
    }


@router.get("/reviews")
async def admin_review_queue(
    space_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """跨空间审核队列：全部 review_required 空间的待审资产。"""
    stmt = (
        select(Asset, Space.slug)
        .join(Space, Space.id == Asset.space_id)
        .where(
            Asset.deleted_at.is_(None),
            Asset.review_status == ReviewStatus.PENDING_REVIEW,
            Space.review_required.is_(True),
        )
        .order_by(Asset.created_at.asc())
    )
    if space_id:
        stmt = stmt.where(Asset.space_id == space_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
    return {
        "items": [_asset_out(a, slug) for a, slug in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/reviews/{asset_id}/decide")
async def admin_decide_review(
    asset_id: uuid.UUID,
    body: DecideRequest,
    request: Request,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
) -> dict[str, Any]:
    """平台级审核决定：与空间审核同一套语义（驳回理由必填、通知上传者）。"""
    if body.action not in ("approve", "reject"):
        raise ValidationError(reason="action 仅支持 approve/reject")
    if body.action == "reject" and not (body.reason and body.reason.strip()):
        raise ValidationError(reason="驳回必须填写理由")
    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))
    before = asset.review_status.value if asset.review_status else None
    asset.review_status = (
        ReviewStatus.APPROVED if body.action == "approve" else ReviewStatus.REJECTED
    )
    asset.review_reason = None if body.action == "approve" else body.reason
    asset.reviewed_by = uuid.UUID(identity.user_id)
    asset.reviewed_at = datetime.now(UTC)
    await record_audit(
        session,
        identity=identity,
        action=f"admin.review.{body.action}",
        object_type="asset",
        object_id=str(asset_id),
        reason=body.reason,
        before={"review_status": before},
        after={"review_status": asset.review_status.value},
        request=request,
    )
    await session.commit()
    from loomvec.core.constants import EVENT_REVIEW_APPROVED, EVENT_REVIEW_REJECTED
    from loomvec.core.events import publish_event

    await publish_event(
        redis,
        {
            "type": EVENT_REVIEW_APPROVED if body.action == "approve" else EVENT_REVIEW_REJECTED,
            "asset_id": str(asset_id),
            "space_id": str(asset.space_id),
            "by": "platform",
        },
    )
    return _asset_out(asset)


@router.post("/assets/{asset_id}/takedown")
async def takedown_asset(
    asset_id: uuid.UUID,
    body: TakedownRequest,
    request: Request,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """下架（连带清理）：rejected + 软删 + 向量删除（图谱清理随 P3 补齐）。"""
    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))
    before = {
        "review_status": asset.review_status.value if asset.review_status else None,
        "status": asset.status.value,
        "deleted": False,
    }
    asset.review_status = ReviewStatus.REJECTED
    asset.review_reason = body.reason
    asset.deleted_at = datetime.now(UTC)
    # 向量连带清理（Milvus 同步客户端，线程外调用会阻塞事件循环，量小可接受；
    # 大批量场景由重嵌入/重建任务处理）
    try:
        request.app.state.milvus.delete_asset_units(asset_id)
    except Exception as e:  # 向量清理失败不阻塞下架（记录告警，可由重建任务兜底）
        import structlog

        structlog.get_logger("loomvec.api.admin").warning(
            "takedown_vector_cleanup_failed", asset_id=str(asset_id), error=str(e)
        )
    await record_audit(
        session,
        identity=identity,
        action="admin.asset.takedown",
        object_type="asset",
        object_id=str(asset_id),
        reason=body.reason,
        before=before,
        after={"review_status": "rejected", "deleted": True},
        request=request,
    )
    await session.commit()
    return {"id": str(asset_id), "taken_down": True}


@router.get("/assets/{asset_id}/units")
async def list_asset_units(
    asset_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """chunk 复核：语义单元切片结果（chunk_method 标注 LLM 分片/结构化兜底）。"""
    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))
    units = (
        (
            await session.execute(
                select(SemanticUnit)
                .where(SemanticUnit.asset_id == asset_id)
                .order_by(SemanticUnit.order_index.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "asset": _asset_out(asset),
        "items": [
            {
                "id": str(u.id),
                "unit_type": u.unit_type.value,
                "title": u.title,
                "content": u.content[:2000],
                "chunk_method": u.chunk_method.value,
                "locator": u.locator,
                "char_count": u.char_count,
                "order_index": u.order_index,
                "embed_model_version": u.embed_model_version,
            }
            for u in units
        ],
    }


@router.post("/assets/batch-rerun", status_code=202)
async def batch_rerun(
    body: BatchRerunRequest,
    request: Request,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> dict[str, Any]:
    """批量重跑（任务化）：逐资产派发管线任务，进度经任务看板跟踪。"""
    if body.from_step not in ("parse", "chunk", "embed", "index"):
        raise ValidationError(reason="from_step 非法", allowed=["parse", "chunk", "embed", "index"])
    assets = (
        (
            await session.execute(
                select(Asset).where(Asset.id.in_(body.asset_ids), Asset.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    found = {a.id for a in assets}
    missing = [str(i) for i in body.asset_ids if i not in found]
    for a in assets:
        a.status = AssetStatus.PENDING
        celery.send_task(
            "pipeline.process_asset", args=[str(a.id), body.from_step], queue=QUEUE_PIPELINE
        )
    await record_audit(
        session,
        identity=identity,
        action="admin.asset.batch_rerun",
        object_type="asset",
        object_id=None,
        after={"asset_ids": [str(i) for i in body.asset_ids], "from_step": body.from_step},
        request=request,
    )
    await session.commit()
    return {"accepted": len(assets), "missing": missing, "from_step": body.from_step}
