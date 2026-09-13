"""P4-API-07 OAuth 2.0 授权码流程（面向第三方应用）：

- GET  /api/v1/oauth/authorize  → 同意页信息（client 展示 + 申请 scopes）；
- POST /api/v1/oauth/authorize  → 用户同意后签发一次性授权码（10 分钟）；
- POST /api/v1/oauth/token      → 授权码换访问令牌（client_secret 校验）。

权限收敛：可授 scopes 仅 read/write（管理域不对第三方开放），且令牌生效时
校验用户状态——禁用用户令牌即失效（token 校验查 user 行）。
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_identity, get_session
from loomvec.api.identity import hash_api_key
from loomvec.core.config import Settings, get_settings
from loomvec.core.db.models import OauthAccessToken, OauthAuthorizationCode, OauthClient, User
from loomvec.core.errors import NotFoundError, UnauthenticatedError, ValidationError

router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])

AUTH_CODE_TTL_SECONDS = 600
ACCESS_TOKEN_TTL_SECONDS = 30 * 24 * 3600


async def _active_client(session: AsyncSession, client_id: str) -> OauthClient:
    from loomvec.core.db.models import OauthClientStatus

    c = (
        await session.execute(
            select(OauthClient).where(
                OauthClient.client_id == client_id, OauthClient.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if c is None or c.status != OauthClientStatus.ACTIVE:
        raise NotFoundError(resource="oauth_client", id=client_id)
    return c


def _validate_redirect(client: OauthClient, redirect_uri: str) -> None:
    if redirect_uri not in (client.redirect_uris or []):
        raise ValidationError(reason="redirect_uri 未登记", redirect_uri=redirect_uri)


class ConsentOut(BaseModel):
    client_id: str
    client_name: str
    homepage_url: str | None
    scopes: list[str]
    state: str | None = None


class AuthorizeRequest(BaseModel):
    client_id: str
    redirect_uri: str
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    state: str | None = None


class AuthorizeOut(BaseModel):
    redirect_to: str
    expires_in: int


class TokenRequest(BaseModel):
    grant_type: str = Field(default="authorization_code")
    code: str
    client_id: str
    client_secret: str
    redirect_uri: str | None = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


@router.get("/authorize", response_model=ConsentOut)
async def consent_info(
    client_id: str,
    redirect_uri: str,
    scopes: str = "read",
    state: str | None = None,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> ConsentOut:
    """同意页信息（要求登录态；同意页由运维端/用户端前端渲染）。"""
    client = await _active_client(session, client_id)
    _validate_redirect(client, redirect_uri)
    requested = [s for s in scopes.split() if s]
    return ConsentOut(
        client_id=client.client_id,
        client_name=client.name,
        homepage_url=client.homepage_url,
        scopes=[s for s in requested if s in (client.scopes or [])],
        state=state,
    )


@router.post("/authorize", response_model=AuthorizeOut, status_code=201)
async def authorize(
    body: AuthorizeRequest,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> AuthorizeOut:
    """用户同意：签发一次性授权码并拼接回调。scopes ∩ client 登记范围。"""
    client = await _active_client(session, body.client_id)
    _validate_redirect(client, body.redirect_uri)
    granted = [s for s in body.scopes if s in (client.scopes or [])]
    if not granted:
        raise ValidationError(reason="无可授予的 scope")
    if identity.user_id.startswith("apikey:"):
        raise UnauthenticatedError(reason="API Key 不能代表用户授权")
    user_id = uuid.UUID(identity.user_id)
    user = (
        await session.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None or user.status.value != "active":
        raise UnauthenticatedError(reason="用户不可用")

    code_raw = f"olc_{secrets.token_urlsafe(32)}"
    session.add(
        OauthAuthorizationCode(
            code_hash=hash_api_key(code_raw),
            client_id=client.client_id,
            user_id=user_id,
            redirect_uri=body.redirect_uri,
            scopes=granted,
            expires_at=datetime.now(UTC) + timedelta(seconds=AUTH_CODE_TTL_SECONDS),
        )
    )
    await session.commit()
    sep = "&" if "?" in body.redirect_uri else "?"
    qs = f"code={code_raw}"
    if body.state:
        qs += f"&state={body.state}"
    return AuthorizeOut(
        redirect_to=f"{body.redirect_uri}{sep}{qs}", expires_in=AUTH_CODE_TTL_SECONDS
    )


@router.post("/token", response_model=TokenOut)
async def token(
    body: TokenRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenOut:
    """授权码换令牌：client_secret 校验、一次性 code、短时效。"""
    if body.grant_type != "authorization_code":
        raise ValidationError(reason="仅支持 authorization_code", got=body.grant_type)
    client = await _active_client(session, body.client_id)
    if client.client_secret_hash != hash_api_key(body.client_secret):
        raise UnauthenticatedError(reason="client_secret 校验失败")
    row = (
        await session.execute(
            select(OauthAuthorizationCode).where(
                OauthAuthorizationCode.code_hash == hash_api_key(body.code)
            )
        )
    ).scalar_one_or_none()
    if row is None or row.client_id != body.client_id:
        raise UnauthenticatedError(reason="授权码无效")
    if row.used_at is not None or row.expires_at < datetime.now(UTC):
        raise UnauthenticatedError(reason="授权码已使用或过期")
    if body.redirect_uri and body.redirect_uri != row.redirect_uri:
        raise ValidationError(reason="redirect_uri 不匹配")
    row.used_at = datetime.now(UTC)
    token_raw = f"olv_{secrets.token_urlsafe(32)}"
    expires_at = datetime.now(UTC) + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)
    session.add(
        OauthAccessToken(
            token_hash=hash_api_key(token_raw),
            client_id=body.client_id,
            user_id=row.user_id,
            scopes=row.scopes or [],
            expires_at=expires_at,
        )
    )
    await session.commit()
    return TokenOut(
        access_token=token_raw,
        expires_in=ACCESS_TOKEN_TTL_SECONDS,
        scope=" ".join(row.scopes or []),
    )
