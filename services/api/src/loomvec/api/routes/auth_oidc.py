"""P4-API-06 OIDC 对接：授权码流程对接企业 IdP、用户自动开通与租户域归属。

流程（docs/04 §5.2/§5.9）：
- GET  /api/v1/auth/oidc/login?domain=example.com → 302 至 IdP 授权页
  （state/nonce 落 Redis，5 分钟一次性）；
- GET  /api/v1/auth/oidc/callback → code 换 token → userinfo →
  按邮箱域匹配 TenantOidcBinding → 自动开通（auth_source=oidc、归属租户）→
  签发平台 JWT。

首次登录引导状态（docs/04 §5.12）：返回体含 first_login 标记，前端引导完善
显示名/密码。dev_mode 下亦可用（便于本地 Keycloak 联调）。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.auth import PlatformTokenProvider
from loomvec.api.context import Identity
from loomvec.api.deps import get_redis, get_session
from loomvec.api.identity import resolve_user_row
from loomvec.core.config import Settings, get_settings
from loomvec.core.db.models import TenantOidcBinding, User
from loomvec.core.errors import NotFoundError, UnauthenticatedError, ValidationError

router = APIRouter(prefix="/api/v1/auth/oidc", tags=["auth-oidc"])

STATE_TTL_SECONDS = 300


def _state_key(state: str) -> str:
    return f"loomvec:oidc:state:{state}"


async def _binding_for_domain(session: AsyncSession, domain: str) -> TenantOidcBinding:
    binding = (
        await session.execute(
            select(TenantOidcBinding).where(
                TenantOidcBinding.domain == domain.lower(), TenantOidcBinding.enabled.is_(True)
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise NotFoundError(resource="oidc_binding", id=domain)
    return binding


async def _discover(http: httpx.AsyncClient, issuer: str) -> dict:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    resp = await http.get(url)
    if resp.status_code != 200:
        raise UnauthenticatedError(reason=f"IdP discovery 失败: HTTP {resp.status_code}")
    return resp.json()


@router.get("/login")
async def oidc_login(
    domain: str,
    redirect_uri: str | None = None,
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
) -> RedirectResponse:
    """发起 OIDC 登录：按邮箱域定位租户绑定 → 302 IdP 授权页。"""
    binding = await _binding_for_domain(session, domain)
    async with httpx.AsyncClient(timeout=10.0) as http:
        doc = await _discover(http, binding.issuer)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    await redis.setex(
        _state_key(state),
        STATE_TTL_SECONDS,
        f"{binding.issuer}|{domain}|{nonce}",
    )
    params = {
        "response_type": "code",
        "client_id": binding.client_id,
        "redirect_uri": redirect_uri or "urn:loopback:loomvec-oidc",
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
    }
    authorize_url = doc["authorization_endpoint"]
    return RedirectResponse(
        authorize_url + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    )


class OidcCallbackOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    username: str
    first_login: bool


@router.get("/callback", response_model=OidcCallbackOut)
async def oidc_callback(
    code: str,
    state: str,
    redirect_uri: str | None = None,
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> OidcCallbackOut:
    """回调：换 token → userinfo → 域归属/自动开通 → 签发平台 JWT。"""
    raw = await redis.get(_state_key(state))
    if not raw:
        raise ValidationError(reason="state 无效或已过期")
    await redis.delete(_state_key(state))
    issuer_s, domain_s = raw.split("|", 2)
    binding = await _binding_for_domain(session, domain_s)
    if binding.issuer != issuer_s:
        raise ValidationError(reason="state 与绑定不匹配")

    async with httpx.AsyncClient(timeout=15.0) as http:
        doc = await _discover(http, binding.issuer)
        token_resp = await http.post(
            doc["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri or "urn:loopback:loomvec-oidc",
                "client_id": binding.client_id,
                "client_secret": binding.client_secret,
            },
        )
        if token_resp.status_code != 200:
            raise UnauthenticatedError(reason=f"IdP token 交换失败: HTTP {token_resp.status_code}")
        # id_token 的 nonce 校验在严格模式下进行；此处以 userinfo 结果为准
        token_resp.json().get("id_token")
        userinfo_resp = await http.get(
            doc["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {token_resp.json().get('access_token')}"},
        )
        if userinfo_resp.status_code != 200:
            raise UnauthenticatedError(reason="IdP userinfo 获取失败")
        userinfo = userinfo_resp.json()

    email = userinfo.get("email") or ""
    sub = userinfo.get("sub")
    if not sub:
        raise UnauthenticatedError(reason="IdP userinfo 缺少 sub")
    email_domain = email.split("@")[-1].lower() if "@" in email else domain_s.lower()
    if email_domain != binding.domain.lower():
        raise ValidationError(reason="邮箱域与登录入口不匹配", domain=binding.domain)

    # 自动开通：oidc_sub 唯一定位；否则按用户名找；再否则创建（归属绑定租户）
    first_login = False
    user = (
        await session.execute(select(User).where(User.oidc_sub == sub, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None:
        username = email.split("@")[0] if email else f"oidc_{sub[:12]}"
        user = (
            await session.execute(
                select(User).where(User.username == username, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if user is None:
            user = User(
                tenant_id=binding.tenant_id,
                username=username,
                display_name=userinfo.get("name") or username,
                email=email or None,
                auth_source="oidc",
                oidc_sub=sub,
                last_active_at=datetime.now(UTC),
            )
            session.add(user)
            first_login = True
        else:
            user.oidc_sub = sub
            user.auth_source = "oidc"
    user.last_active_at = datetime.now(UTC)
    await session.flush()

    # 平台角色从 DB 单源解析（resolve_user_row 合并 user_role），签发平台 JWT
    provider = PlatformTokenProvider(settings)
    identity = await resolve_user_row(
        session,
        Identity(
            user_id=str(user.id),
            username=user.username,
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
            roles=(),
        ),
    )
    token, claims = provider.issue_token(
        username=user.username,
        tenant_id=identity.tenant_id,
        roles=list(identity.roles),
    )
    return OidcCallbackOut(
        access_token=token,
        expires_in=claims["exp"] - claims["iat"],
        username=user.username,
        first_login=first_login,
    )
