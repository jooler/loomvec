"""P4-API-03 治理接口（用户）：禁用/启用、平台角色分配（仅 super_admin 可分配）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_admin, require_platform_role
from loomvec.core.constants import PLATFORM_ROLE_SUPER_ADMIN, PLATFORM_ROLES
from loomvec.core.db.models import Role, Tenant, User, UserRole
from loomvec.core.errors import NotFoundError, ValidationError

router = APIRouter(tags=["admin-users"])


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str | None
    email: str | None
    status: str
    auth_source: str
    tenant_id: uuid.UUID | None
    tenant_name: str | None = None
    last_active_at: datetime | None
    platform_roles: list[str] = []


class DisableRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class PlatformRoleRequest(BaseModel):
    role: str
    reason: str = Field(min_length=1, max_length=500)


async def _roles_for_users(
    session: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(UserRole.user_id, Role.code)
            .join(Role, Role.id == UserRole.role_id)
            .where(UserRole.user_id.in_(user_ids), Role.is_platform_role.is_(True))
        )
    ).all()
    out: dict[uuid.UUID, list[str]] = {}
    for uid, code in rows:
        out.setdefault(uid, []).append(code)
    return out


def _user_out(u: User, tenant_name: str | None, roles: list[str]) -> dict[str, Any]:
    return UserOut(
        id=u.id,
        username=u.username,
        display_name=u.display_name,
        email=u.email,
        status=u.status.value,
        auth_source=u.auth_source.value,
        tenant_id=u.tenant_id,
        tenant_name=tenant_name,
        last_active_at=u.last_active_at,
        platform_roles=roles,
    ).model_dump(mode="json")


@router.get("/users")
async def list_users(
    q: str | None = Query(default=None, max_length=128),
    tenant_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(User).where(User.deleted_at.is_(None))
    if q:
        stmt = stmt.where(User.username.ilike(f"%{q}%") | User.email.ilike(f"%{q}%"))
    if tenant_id:
        stmt = stmt.where(User.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(User.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    users = (
        (await session.execute(stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    tenant_names = (
        dict(
            (
                await session.execute(
                    select(Tenant.id, Tenant.name).where(
                        Tenant.id.in_([u.tenant_id for u in users if u.tenant_id])
                    )
                )
            ).all()
        )
        if any(u.tenant_id for u in users)
        else {}
    )
    role_map = await _roles_for_users(session, [u.id for u in users])
    return {
        "items": [
            _user_out(u, tenant_names.get(u.tenant_id), role_map.get(u.id, [])) for u in users
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def _get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(resource="user", id=str(user_id))
    return user


@router.post("/users/{user_id}/disable")
async def disable_user(
    user_id: uuid.UUID,
    body: DisableRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    from loomvec.core.db.models import UserStatus

    user = await _get_user(session, user_id)
    before = user.status.value
    user.status = UserStatus.DISABLED
    await record_audit(
        session,
        identity=identity,
        action="admin.user.disable",
        object_type="user",
        object_id=str(user_id),
        reason=body.reason,
        before={"status": before},
        after={"status": "disabled"},
    )
    await session.commit()
    return {"id": str(user_id), "status": "disabled"}


@router.post("/users/{user_id}/enable")
async def enable_user(
    user_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    from loomvec.core.db.models import UserStatus

    user = await _get_user(session, user_id)
    user.status = UserStatus.ACTIVE
    await record_audit(
        session,
        identity=identity,
        action="admin.user.enable",
        object_type="user",
        object_id=str(user_id),
        before={"status": user.status.value},
        after={"status": "active"},
    )
    await session.commit()
    return {"id": str(user_id), "status": "active"}


@router.put("/users/{user_id}/platform-role")
async def set_platform_role(
    user_id: uuid.UUID,
    body: PlatformRoleRequest,
    identity: Identity = Depends(require_platform_role(PLATFORM_ROLE_SUPER_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """平台角色分配：仅 super_admin。先清后授（单平台角色模型）。"""
    if body.role not in PLATFORM_ROLES:
        raise ValidationError(reason=f"非法平台角色: {body.role}", allowed=list(PLATFORM_ROLES))
    # 存在性校验（不存在即 404）
    await _get_user(session, user_id)
    before = await _roles_for_users(session, [user_id])
    # 清除旧平台角色
    old = (
        (
            await session.execute(
                select(UserRole).where(
                    UserRole.user_id == user_id,
                    UserRole.role_id.in_(select(Role.id).where(Role.is_platform_role.is_(True))),
                )
            )
        )
        .scalars()
        .all()
    )
    for ur in old:
        await session.delete(ur)
    role = (
        await session.execute(
            select(Role).where(Role.code == body.role, Role.is_platform_role.is_(True))
        )
    ).scalar_one_or_none()
    if role is None:
        raise NotFoundError(resource="role", id=body.role)
    session.add(UserRole(user_id=user_id, role_id=role.id))
    await record_audit(
        session,
        identity=identity,
        action="admin.user.platform_role_set",
        object_type="user",
        object_id=str(user_id),
        reason=body.reason,
        before={"roles": before.get(user_id, [])},
        after={"roles": [body.role]},
    )
    await session.commit()
    return {"id": str(user_id), "platform_roles": [body.role]}
