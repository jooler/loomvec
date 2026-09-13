"""P4-API-03 治理接口（空间）：全量列表（含个人空间）、封禁/解封、owner 转移、
配额调整、内容清空（危险操作）。

封禁语义：banned_at 非空时成员访问被拒（require_space 闸门拦截，数据保留）。
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
from loomvec.api.deps import get_session, require_admin
from loomvec.core.db.models import Asset, Space, SpaceMember, SpaceUsage, User
from loomvec.core.errors import NotFoundError, ValidationError

router = APIRouter(tags=["admin-spaces"])


class SpaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    space_type: str
    tenant_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    owner_name: str | None = None
    member_count: int = 0
    asset_count: int = 0
    storage_bytes: int = 0
    file_count: int = 0
    review_required: bool
    embedding_model: str | None
    chunk_preset: str | None
    banned: bool
    created_at: datetime


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class OwnerTransferRequest(ReasonRequest):
    new_owner_id: uuid.UUID


class SpaceQuotaRequest(ReasonRequest):
    quota_storage_bytes: int = Field(ge=0)
    quota_file_count: int = Field(ge=0)


def _space_out(
    s: Space,
    member_count: int = 0,
    asset_count: int = 0,
    usage: SpaceUsage | None = None,
    owner_name: str | None = None,
) -> dict[str, Any]:
    return SpaceOut(
        id=s.id,
        slug=s.slug,
        name=s.name,
        space_type=s.space_type.value,
        tenant_id=s.tenant_id,
        owner_id=s.owner_id,
        owner_name=owner_name,
        member_count=member_count,
        asset_count=asset_count,
        storage_bytes=usage.storage_bytes if usage else 0,
        file_count=usage.file_count if usage else 0,
        review_required=s.review_required,
        embedding_model=s.embedding_model,
        chunk_preset=s.chunk_preset,
        banned=s.banned_at is not None,
        created_at=s.created_at,
    ).model_dump(mode="json")


async def _get_space(session: AsyncSession, space_id: uuid.UUID) -> Space:
    space = (
        await session.execute(select(Space).where(Space.id == space_id, Space.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if space is None:
        raise NotFoundError(resource="space", id=str(space_id))
    return space


@router.get("/spaces")
async def list_spaces(
    q: str | None = Query(default=None, max_length=255),
    tenant_id: uuid.UUID | None = Query(default=None),
    space_type: str | None = Query(default=None),
    banned: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Space).where(Space.deleted_at.is_(None))
    if q:
        stmt = stmt.where(Space.name.ilike(f"%{q}%") | Space.slug.ilike(f"%{q}%"))
    if tenant_id:
        stmt = stmt.where(Space.tenant_id == tenant_id)
    if space_type:
        stmt = stmt.where(Space.space_type == space_type)
    if banned is not None:
        stmt = stmt.where(Space.banned_at.is_not(None) if banned else Space.banned_at.is_(None))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    spaces = (
        (await session.execute(stmt.order_by(Space.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    ids = [s.id for s in spaces]
    member_counts = (
        dict(
            (
                await session.execute(
                    select(SpaceMember.space_id, func.count())
                    .where(SpaceMember.space_id.in_(ids))
                    .group_by(SpaceMember.space_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    asset_counts = (
        dict(
            (
                await session.execute(
                    select(Asset.space_id, func.count())
                    .where(Asset.space_id.in_(ids), Asset.deleted_at.is_(None))
                    .group_by(Asset.space_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    usages = (
        {
            u.space_id: u
            for u in (await session.execute(select(SpaceUsage).where(SpaceUsage.space_id.in_(ids))))
            .scalars()
            .all()
        }
        if ids
        else {}
    )
    owner_ids = [s.owner_id for s in spaces if s.owner_id]
    owner_names = (
        dict(
            (
                await session.execute(
                    select(User.id, User.display_name).where(User.id.in_(owner_ids))
                )
            ).all()
        )
        if owner_ids
        else {}
    )
    return {
        "items": [
            _space_out(
                s,
                member_counts.get(s.id, 0),
                asset_counts.get(s.id, 0),
                usages.get(s.id),
                owner_names.get(s.owner_id),
            )
            for s in spaces
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/spaces/{space_id}")
async def get_space(
    space_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    space = await _get_space(session, space_id)
    members = (
        (await session.execute(select(SpaceMember).where(SpaceMember.space_id == space_id)))
        .scalars()
        .all()
    )
    asset_count = (
        await session.execute(
            select(func.count())
            .select_from(Asset)
            .where(Asset.space_id == space_id, Asset.deleted_at.is_(None))
        )
    ).scalar_one()
    usage = (
        await session.execute(select(SpaceUsage).where(SpaceUsage.space_id == space_id))
    ).scalar_one_or_none()
    owner_name = None
    if space.owner_id:
        owner_name = await session.scalar(
            select(User.display_name).where(User.id == space.owner_id)
        )
    return {
        **_space_out(space, len(members), asset_count, usage, owner_name),
        "members": [
            {
                "user_id": str(m.user_id),
                "role": m.role.value,
                "invited_by": str(m.invited_by) if m.invited_by else None,
            }
            for m in members
        ],
    }


@router.post("/spaces/{space_id}/ban")
async def ban_space(
    space_id: uuid.UUID,
    body: ReasonRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    space = await _get_space(session, space_id)
    if space.banned_at is not None:
        raise ValidationError(reason="空间已处于封禁状态")
    space.banned_at = datetime.now(UTC)
    await record_audit(
        session,
        identity=identity,
        action="admin.space.ban",
        object_type="space",
        object_id=str(space_id),
        reason=body.reason,
        after={"banned": True},
    )
    await session.commit()
    return {"id": str(space_id), "banned": True}


@router.post("/spaces/{space_id}/unban")
async def unban_space(
    space_id: uuid.UUID,
    body: ReasonRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    space = await _get_space(session, space_id)
    space.banned_at = None
    await record_audit(
        session,
        identity=identity,
        action="admin.space.unban",
        object_type="space",
        object_id=str(space_id),
        reason=body.reason,
        after={"banned": False},
    )
    await session.commit()
    return {"id": str(space_id), "banned": False}


@router.post("/spaces/{space_id}/transfer-owner")
async def transfer_owner(
    space_id: uuid.UUID,
    body: OwnerTransferRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    space = await _get_space(session, space_id)
    new_owner = (
        await session.execute(
            select(User).where(User.id == body.new_owner_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if new_owner is None:
        raise NotFoundError(resource="user", id=str(body.new_owner_id))
    member = (
        await session.execute(
            select(SpaceMember).where(
                SpaceMember.space_id == space_id, SpaceMember.user_id == body.new_owner_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise ValidationError(reason="新 owner 必须是空间成员")
    old_owner = space.owner_id
    space.owner_id = body.new_owner_id
    # 成员角色同步：旧 owner → editor，新成员角色 → owner
    from loomvec.core.db.models import SpaceRole

    if old_owner:
        om = (
            await session.execute(
                select(SpaceMember).where(
                    SpaceMember.space_id == space_id, SpaceMember.user_id == old_owner
                )
            )
        ).scalar_one_or_none()
        if om is not None:
            om.role = SpaceRole.EDITOR
    member.role = SpaceRole.OWNER
    await record_audit(
        session,
        identity=identity,
        action="admin.space.transfer_owner",
        object_type="space",
        object_id=str(space_id),
        reason=body.reason,
        before={"owner_id": str(old_owner) if old_owner else None},
        after={"owner_id": str(body.new_owner_id)},
    )
    await session.commit()
    return {"id": str(space_id), "owner_id": str(body.new_owner_id)}


@router.put("/spaces/{space_id}/quota")
async def set_space_quota(
    space_id: uuid.UUID,
    body: SpaceQuotaRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    space = await _get_space(session, space_id)
    before = {
        "quota_storage_bytes": space.quota_storage_bytes,
        "quota_file_count": space.quota_file_count,
    }
    space.quota_storage_bytes = body.quota_storage_bytes
    space.quota_file_count = body.quota_file_count
    await record_audit(
        session,
        identity=identity,
        action="admin.space.quota_update",
        object_type="space",
        object_id=str(space_id),
        reason=body.reason,
        before=before,
        after={
            "quota_storage_bytes": body.quota_storage_bytes,
            "quota_file_count": body.quota_file_count,
        },
    )
    await session.commit()
    return before | {
        "quota_storage_bytes": body.quota_storage_bytes,
        "quota_file_count": body.quota_file_count,
    }


@router.post("/spaces/{space_id}/clear", status_code=202)
async def clear_space(
    space_id: uuid.UUID,
    body: ReasonRequest,
    request: Request,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """内容清空（危险操作）：软删全部资产 + 向量连带清理（派生物随空间删除清理）。

    必填理由入审计；向量清理逐资产同步执行（大批量由索引重建任务兜底）。
    """
    # 存在性校验（不存在即 404）
    await _get_space(session, space_id)
    assets = (
        (
            await session.execute(
                select(Asset).where(Asset.space_id == space_id, Asset.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    from datetime import datetime as _dt

    for a in assets:
        a.deleted_at = _dt.now(UTC)
        try:
            request.app.state.milvus.delete_asset_units(a.id)
        except Exception as e:
            import structlog

            structlog.get_logger("loomvec.api.admin").warning(
                "clear_space_vector_cleanup_failed", asset_id=str(a.id), error=str(e)
            )
    await record_audit(
        session,
        identity=identity,
        action="admin.space.clear",
        object_type="space",
        object_id=str(space_id),
        reason=body.reason,
        before={"asset_count": len(assets)},
        after={"asset_count": 0},
    )
    await session.commit()
    return {"id": str(space_id), "accepted": True, "asset_count": len(assets)}
