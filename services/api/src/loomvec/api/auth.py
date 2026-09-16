"""P0-API-02 认证骨架（平台 JWT 签发与校验单点）。

- 平台 JWT（HS256 + jwt_secret）：dev 模式经 `POST /api/v1/auth/dev/token`
  签发（生产禁用）；OIDC 登录（routes/auth_oidc.py）回调后签发同构 JWT；
- 依赖读取 Bearer Token，把 `Identity` 注入 `request.state.identity`
  （编排见 deps.get_identity：JWT vs API Key vs OAuth 令牌）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt
from fastapi import Depends, FastAPI
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

from loomvec.api.context import Identity
from loomvec.core.config import Settings, get_settings
from loomvec.core.errors import UnauthenticatedError

bearer_scheme = HTTPBearer(auto_error=False)

# 平台 JWT 的 iss（签发/校验单源）；agent token（iss=loomvec-agent）被白名单拒绝
PLATFORM_ISSUER = "loomvec-dev"


class PlatformTokenProvider:
    """平台 JWT 签发与校验：dev 路由与 OIDC 回调共用（同一 secret/算法）。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def issue_token(
        self,
        *,
        username: str,
        tenant_id: str | None = None,
        roles: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": str(uuid.uuid5(uuid.NAMESPACE_URL, f"dev:{username}")),
            "username": username,
            "roles": roles or ["user"],
            "iat": now,
            "exp": now + (ttl_seconds or self._settings.auth.access_token_ttl_seconds),
            "iss": PLATFORM_ISSUER,
        }
        if tenant_id:
            claims["tenant_id"] = tenant_id
        token = jwt.encode(
            claims, self._settings.auth.jwt_secret, algorithm=self._settings.auth.jwt_algorithm
        )
        return token, claims

    async def exchange(self, credentials: dict[str, Any]) -> Identity:
        token = credentials.get("token")
        if not token:
            raise UnauthenticatedError()
        try:
            claims = jwt.decode(
                token,
                self._settings.auth.jwt_secret,
                algorithms=[self._settings.auth.jwt_algorithm],
            )
        except jwt.PyJWTError as e:
            raise UnauthenticatedError(reason=str(e)) from e
        # iss 白名单：同密钥签发的 agent token（iss=loomvec-agent）不得充当平台
        # 登录态——agent token 在 dsh 子进程/MCP 链路流转，暴露面更大且 TTL 独立
        if claims.get("iss") != PLATFORM_ISSUER:
            raise UnauthenticatedError(reason="非平台 token")
        return Identity(
            user_id=claims["sub"],
            username=claims.get("username", "unknown"),
            tenant_id=claims.get("tenant_id"),
            roles=tuple(claims.get("roles", [])),
        )


def get_identity_provider(settings: Settings = Depends(get_settings)) -> PlatformTokenProvider:
    """平台 JWT 校验器（dev 与生产同一路径；dev_mode 仅门控签发路由）。"""
    return PlatformTokenProvider(settings)


class DevTokenRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    tenant_id: str | None = None
    roles: list[str] = Field(default_factory=lambda: ["user"])
    ttl_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 3600)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


def register_auth_routes(app: FastAPI, settings: Settings) -> None:
    dev_provider = PlatformTokenProvider(settings)

    @app.post("/api/v1/auth/dev/token", response_model=TokenResponse)
    async def issue_dev_token(body: DevTokenRequest) -> TokenResponse:
        """dev 模式专用：签发测试 JWT（生产环境 404/禁用）。"""
        if not settings.auth.dev_mode:
            from loomvec.core.errors import NotFoundError

            raise NotFoundError()
        token, claims = dev_provider.issue_token(
            username=body.username,
            tenant_id=body.tenant_id,
            roles=body.roles,
            ttl_seconds=body.ttl_seconds,
        )
        return TokenResponse(access_token=token, expires_in=claims["exp"] - claims["iat"])
