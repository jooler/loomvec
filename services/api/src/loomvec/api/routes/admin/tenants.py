"""P4-API-03 治理接口：租户 CRUD/停用（冻结语义）/OIDC 域绑定/配额调整。

冻结语义：停用租户后其用户登录与 API 访问被阻断（identity 层强制），数据保留；
清理（硬删）为独立危险操作，不在本阶段开放 REST 入口（运维脚本处理 + 审计）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import audit_snapshot, record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_admin
from loomvec.core.db.models import Space, SpaceUsage, Tenant, TenantOidcBinding, TenantStatus, User
from loomvec.core.errors import ConflictError, NotFoundError

router = APIRouter(tags=["admin-tenants"])

AUDIT_FIELDS = ("name", "plan", "status", "quota_storage_bytes", "quota_file_count")


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    plan: str
    status: str
    quota_storage_bytes: int
    quota_file_count: int
    created_at: datetime
    space_count: int = 0
    user_count: int = 0
    used_storage_bytes: int = 0
    used_file_count: int = 0


class TenantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    plan: str = "free"
    quota_storage_bytes: int = Field(default=0, ge=0)
    quota_file_count: int = Field(default=0, ge=0)


class TenantPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    plan: str | None = None


class TenantQuotaRequest(BaseModel):
    quota_storage_bytes: int = Field(ge=0)
    quota_file_count: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class SuspendRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class OidcBindingRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=255, description="邮箱域，如 example.com")
    issuer: str = Field(min_length=8, max_length=512)
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str = Field(default="", max_length=512)
    enabled: bool = True


def _out(
    t: Tenant, space_count: int = 0, user_count: int = 0, usage: SpaceUsage | None = None
) -> TenantOut:
    return TenantOut(
        id=t.id,
        name=t.name,
        plan=t.plan,
        status=t.status.value,
        quota_storage_bytes=t.quota_storage_bytes,
        quota_file_count=t.quota_file_count,
        created_at=t.created_at,
        space_count=space_count,
        user_count=user_count,
        used_storage_bytes=usage.storage_bytes if usage else 0,
        used_file_count=usage.file_count if usage else 0,
    )


async def _get_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise NotFoundError(resource="tenant", id=str(tenant_id))
    return tenant


@router.get("/tenants")
async def list_tenants(
    q: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Tenant).where(Tenant.deleted_at.is_(None))
    if q:
        stmt = stmt.where(Tenant.name.ilike(f"%{q}%"))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    tenants = (
        (await session.execute(stmt.order_by(Tenant.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    space_counts = dict(
        (
            await session.execute(select(Space.tenant_id, func.count()).group_by(Space.tenant_id))
        ).all()
    )
    user_counts = dict(
        (await session.execute(select(User.tenant_id, func.count()).group_by(User.tenant_id))).all()
    )
    # 同租户多空间用量需求和（SpaceUsage 按空间一行）
    usage_rows = (
        await session.execute(
            select(
                SpaceUsage.tenant_id,
                func.coalesce(func.sum(SpaceUsage.storage_bytes), 0),
                func.coalesce(func.sum(SpaceUsage.file_count), 0),
            ).group_by(SpaceUsage.tenant_id)
        )
    ).all()
    usage_agg = {tid: (used_s, used_f) for tid, used_s, used_f in usage_rows}
    out = []
    for t in tenants:
        used_s, used_f = usage_agg.get(t.id, (0, 0))
        out.append(
            _out(t, space_counts.get(t.id, 0), user_counts.get(t.id, 0))
            .model_copy(update={"used_storage_bytes": int(used_s), "used_file_count": int(used_f)})
            .model_dump()
        )
    return {"items": out, "total": total, "limit": limit, "offset": offset}


@router.post("/tenants", status_code=201)
async def create_tenant(
    body: TenantCreateRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    tenant = Tenant(
        name=body.name,
        plan=body.plan,
        quota_storage_bytes=body.quota_storage_bytes,
        quota_file_count=body.quota_file_count,
    )
    session.add(tenant)
    await record_audit(
        session,
        identity=identity,
        action="admin.tenant.create",
        object_type="tenant",
        object_id=str(tenant.id),
        after=audit_snapshot(tenant, AUDIT_FIELDS),
    )
    await session.commit()
    return _out(tenant)


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    tenant = await _get_tenant(session, tenant_id)
    space_count = (
        await session.execute(
            select(func.count())
            .select_from(Space)
            .where(Space.tenant_id == tenant_id, Space.deleted_at.is_(None))
        )
    ).scalar_one()
    user_count = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id, User.deleted_at.is_(None))
        )
    ).scalar_one()
    return _out(tenant, space_count, user_count)


@router.patch("/tenants/{tenant_id}")
async def patch_tenant(
    tenant_id: uuid.UUID,
    body: TenantPatchRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    tenant = await _get_tenant(session, tenant_id)
    before = audit_snapshot(tenant, ("name", "plan"))
    if body.name is not None:
        tenant.name = body.name
    if body.plan is not None:
        tenant.plan = body.plan
    await record_audit(
        session,
        identity=identity,
        action="admin.tenant.update",
        object_type="tenant",
        object_id=str(tenant_id),
        before=before,
        after=audit_snapshot(tenant, ("name", "plan")),
    )
    await session.commit()
    return _out(tenant)


@router.post("/tenants/{tenant_id}/suspend")
async def suspend_tenant(
    tenant_id: uuid.UUID,
    body: SuspendRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """停用（冻结语义）：阻断登录与 API 访问，数据保留；必填理由入审计。"""
    tenant = await _get_tenant(session, tenant_id)
    before = audit_snapshot(tenant, AUDIT_FIELDS)
    tenant.status = TenantStatus.SUSPENDED
    await record_audit(
        session,
        identity=identity,
        action="admin.tenant.suspend",
        object_type="tenant",
        object_id=str(tenant_id),
        reason=body.reason,
        before=before,
        after=audit_snapshot(tenant, AUDIT_FIELDS),
    )
    await session.commit()
    return {"id": str(tenant_id), "status": tenant.status.value}


@router.post("/tenants/{tenant_id}/activate")
async def activate_tenant(
    tenant_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    tenant = await _get_tenant(session, tenant_id)
    before = audit_snapshot(tenant, AUDIT_FIELDS)
    tenant.status = TenantStatus.ACTIVE
    await record_audit(
        session,
        identity=identity,
        action="admin.tenant.activate",
        object_type="tenant",
        object_id=str(tenant_id),
        before=before,
        after=audit_snapshot(tenant, AUDIT_FIELDS),
    )
    await session.commit()
    return {"id": str(tenant_id), "status": tenant.status.value}


@router.put("/tenants/{tenant_id}/quota")
async def set_tenant_quota(
    tenant_id: uuid.UUID,
    body: TenantQuotaRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> TenantOut:
    tenant = await _get_tenant(session, tenant_id)
    before = audit_snapshot(tenant, ("quota_storage_bytes", "quota_file_count"))
    tenant.quota_storage_bytes = body.quota_storage_bytes
    tenant.quota_file_count = body.quota_file_count
    await record_audit(
        session,
        identity=identity,
        action="admin.tenant.quota_update",
        object_type="tenant",
        object_id=str(tenant_id),
        reason=body.reason,
        before=before,
        after=audit_snapshot(tenant, ("quota_storage_bytes", "quota_file_count")),
    )
    await session.commit()
    return _out(tenant)


# ---------------------------------------------------------------------------
# OIDC 域绑定（P4-API-03/06 交叉）：该域登录用户自动归属租户
# ---------------------------------------------------------------------------


def _binding_out(b: TenantOidcBinding) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "tenant_id": str(b.tenant_id),
        "domain": b.domain,
        "issuer": b.issuer,
        "client_id": b.client_id,
        "has_secret": bool(b.client_secret),
        "enabled": b.enabled,
        "created_at": b.created_at.isoformat(),
    }


@router.get("/tenants/{tenant_id}/oidc-binding")
async def get_oidc_binding(
    tenant_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any] | None:
    await _get_tenant(session, tenant_id)
    b = (
        await session.execute(
            select(TenantOidcBinding).where(TenantOidcBinding.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return _binding_out(b) if b else None


@router.put("/tenants/{tenant_id}/oidc-binding")
async def put_oidc_binding(
    tenant_id: uuid.UUID,
    body: OidcBindingRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_tenant(session, tenant_id)
    existing = (
        await session.execute(
            select(TenantOidcBinding).where(TenantOidcBinding.domain == body.domain)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.tenant_id != tenant_id:
        raise ConflictError(reason="该邮箱域已绑定其他租户", domain=body.domain)
    if existing is None:
        existing = TenantOidcBinding(tenant_id=tenant_id, domain=body.domain)
        session.add(existing)
    existing.issuer = body.issuer
    existing.client_id = body.client_id
    if body.client_secret:  # 空值保留原 secret
        existing.client_secret = body.client_secret
    existing.enabled = body.enabled
    await record_audit(
        session,
        identity=identity,
        action="admin.tenant.oidc_binding_update",
        object_type="tenant_oidc_binding",
        object_id=str(existing.id),
        after={"domain": body.domain, "issuer": body.issuer, "enabled": body.enabled},
    )
    await session.commit()
    return _binding_out(existing)
