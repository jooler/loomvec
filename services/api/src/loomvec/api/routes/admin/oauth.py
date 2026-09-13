"""P4-API-07 OAuth 应用管理（运营端）：client CRUD、secret 签发/重置、启停。"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_admin
from loomvec.api.identity import hash_api_key
from loomvec.core.constants import SCOPE_READ, SCOPE_WRITE
from loomvec.core.db.models import OauthClient, OauthClientStatus
from loomvec.core.errors import NotFoundError, ValidationError

router = APIRouter(tags=["admin-oauth"])

# OAuth 可授 scopes：仅用户级读写（管理域 admin:* 不对第三方开放）
OAUTH_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE)


class OauthClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    redirect_uris: list[str] = Field(min_length=1, max_length=20)
    scopes: list[str] = Field(default_factory=lambda: [SCOPE_READ])
    description: str | None = None
    homepage_url: str | None = None


class OauthClientPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    redirect_uris: list[str] | None = None
    description: str | None = None
    homepage_url: str | None = None
    status: Literal["active", "suspended"] | None = None


def _out(c: OauthClient) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "name": c.name,
        "client_id": c.client_id,
        "redirect_uris": c.redirect_uris or [],
        "scopes": c.scopes or [],
        "description": c.description,
        "homepage_url": c.homepage_url,
        "status": c.status.value,
        "created_at": c.created_at.isoformat(),
    }


async def _get_client(session: AsyncSession, client_db_id: uuid.UUID) -> OauthClient:
    c = (
        await session.execute(
            select(OauthClient).where(
                OauthClient.id == client_db_id, OauthClient.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if c is None:
        raise NotFoundError(resource="oauth_client", id=str(client_db_id))
    return c


def _validate_scopes(scopes: list[str]) -> list[str]:
    bad = [s for s in scopes if s not in OAUTH_SCOPES]
    if bad:
        raise ValidationError(reason=f"OAuth 不可授予 scope: {bad}", allowed=list(OAUTH_SCOPES))
    return list(dict.fromkeys(scopes))


@router.post("/oauth/clients", status_code=201)
async def create_client(
    body: OauthClientCreateRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    scopes = _validate_scopes(body.scopes)
    secret_raw = f"olv_{secrets.token_urlsafe(32)}"
    client = OauthClient(
        name=body.name,
        client_id=f"lc_{secrets.token_urlsafe(16)}",
        client_secret_hash=hash_api_key(secret_raw),
        redirect_uris=body.redirect_uris,
        scopes=scopes,
        description=body.description,
        homepage_url=body.homepage_url,
        created_by=uuid.UUID(identity.user_id),
    )
    session.add(client)
    await record_audit(
        session,
        identity=identity,
        action="admin.oauth.client_create",
        object_type="oauth_client",
        object_id=str(client.id),
        after={"name": body.name, "redirect_uris": body.redirect_uris, "scopes": scopes},
    )
    await session.commit()
    out = _out(client)
    out["client_secret"] = secret_raw  # 明文仅此一次
    return out


@router.post("/oauth/clients/{client_db_id}/reset-secret")
async def reset_client_secret(
    client_db_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    client = await _get_client(session, client_db_id)
    secret_raw = f"olv_{secrets.token_urlsafe(32)}"
    client.client_secret_hash = hash_api_key(secret_raw)
    await record_audit(
        session,
        identity=identity,
        action="admin.oauth.client_reset_secret",
        object_type="oauth_client",
        object_id=str(client_db_id),
    )
    await session.commit()
    return {"client_id": client.client_id, "client_secret": secret_raw}


@router.get("/oauth/clients")
async def list_clients(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(OauthClient).where(OauthClient.deleted_at.is_(None))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    clients = (
        (
            await session.execute(
                stmt.order_by(OauthClient.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_out(c) for c in clients], "total": total, "limit": limit, "offset": offset}


@router.get("/oauth/clients/{client_db_id}")
async def get_client(
    client_db_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return _out(await _get_client(session, client_db_id))


@router.patch("/oauth/clients/{client_db_id}")
async def patch_client(
    client_db_id: uuid.UUID,
    body: OauthClientPatchRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    client = await _get_client(session, client_db_id)
    before = _out(client)
    if body.name is not None:
        client.name = body.name
    if body.redirect_uris is not None:
        client.redirect_uris = body.redirect_uris
    if body.description is not None:
        client.description = body.description
    if body.homepage_url is not None:
        client.homepage_url = body.homepage_url
    if body.status is not None:
        client.status = (
            OauthClientStatus.ACTIVE if body.status == "active" else OauthClientStatus.SUSPENDED
        )
    await record_audit(
        session,
        identity=identity,
        action="admin.oauth.client_update",
        object_type="oauth_client",
        object_id=str(client_db_id),
        before={"status": before["status"], "redirect_uris": before["redirect_uris"]},
        after={"status": client.status.value, "redirect_uris": client.redirect_uris},
    )
    await session.commit()
    return _out(client)


@router.delete("/oauth/clients/{client_db_id}", status_code=204)
async def delete_client(
    client_db_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    from datetime import UTC

    client = await _get_client(session, client_db_id)
    client.deleted_at = datetime.now(UTC)
    await record_audit(
        session,
        identity=identity,
        action="admin.oauth.client_delete",
        object_type="oauth_client",
        object_id=str(client_db_id),
    )
    await session.commit()
