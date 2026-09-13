"""P1-API-04 API Key 基础版：签发/列表/吊销（scopes 仅 read/write 两类）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_scope
from loomvec.api.identity import generate_api_key
from loomvec.core.constants import ALL_SCOPES
from loomvec.core.db.models import ApiKey
from loomvec.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1/api-keys", tags=["api-keys"])

# 与 core.constants.ALL_SCOPES 保持同步（Literal 以保留 OpenAPI 枚举表达）
Scope = Literal["read", "write"]


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: list[Scope] = Field(default_factory=lambda: [ALL_SCOPES[0]])

    @field_validator("scopes")
    @classmethod
    def _check_scopes(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in ALL_SCOPES]
        if bad:
            raise ValueError(f"非法 scope: {bad}")
        return v

    rate_limit_per_min: int = Field(default=600, ge=1, le=10000)
    expires_in_seconds: int | None = Field(default=None, gt=0, le=365 * 24 * 3600)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    rate_limit_per_min: int
    expires_at: datetime | None
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    key: str  # 明文仅此一次返回


def _out(k: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        name=k.name,
        scopes=k.scopes or [],
        rate_limit_per_min=k.rate_limit_per_min,
        expires_at=k.expires_at,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
    )


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    body: ApiKeyCreateRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreatedOut:
    raw, key_hash = generate_api_key()
    tenant_uuid = uuid.UUID(identity.tenant_id) if identity.tenant_id else None
    key = ApiKey(
        tenant_id=tenant_uuid,
        name=body.name,
        key_hash=key_hash,
        scopes=list(dict.fromkeys(body.scopes)),
        rate_limit_per_min=body.rate_limit_per_min,
        expires_at=(
            datetime.now(UTC) + timedelta(seconds=body.expires_in_seconds)
            if body.expires_in_seconds
            else None
        ),
    )
    session.add(key)
    await session.commit()
    return ApiKeyCreatedOut(**_out(key).model_dump(), key=raw)


@router.get("", response_model=list[ApiKeyOut])
async def list_api_keys(
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyOut]:
    keys = (
        (await session.execute(select(ApiKey).where(ApiKey.deleted_at.is_(None)))).scalars().all()
    )
    return [_out(k) for k in keys]


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: uuid.UUID,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    from datetime import datetime as _dt

    key = (
        await session.execute(
            select(ApiKey).where(ApiKey.id == key_id, ApiKey.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if key is None:
        raise NotFoundError(resource="api_key", id=str(key_id))
    key.deleted_at = _dt.now(UTC)
    await session.commit()
