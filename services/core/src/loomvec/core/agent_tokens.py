"""P5 agent token：短时 JWT（14 文档 §6.2；签发在 agent 网关，校验在 api MCP）。

- 复用平台 HS256 密钥与算法签发；claims = 平台 JWT（sub/tenant_id/roles）
  + agent_env_id / agent_session_id / scope_space_ids + jti；
- 只在网关 → dsh 子进程 → MCP endpoint 这条内部链路上流转，前端不可见；
- 交接/离职时把未过期 token 的 jti 写入 Redis 黑名单（agent:jti:{jti}），
  MCP 校验侧即时失效。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt

from loomvec.core.config import Settings

JTI_BLACKLIST_PREFIX = "agent:jti:"
CITATIONS_PREFIX = "agent:citations:"


@dataclass(frozen=True)
class AgentTokenClaims:
    """MCP endpoint 上的智能体身份（identity 机制零改动，仅新增 claims）。"""

    user_id: str
    username: str = "agent"
    tenant_id: str | None = None
    roles: tuple[str, ...] = ()
    env_id: str = ""
    session_id: str = ""
    scope_space_ids: tuple[str, ...] = ()
    jti: str = ""
    exp: int = 0

    @property
    def scope_list(self) -> list[str]:
        return list(self.scope_space_ids)


def issue_agent_token(
    settings: Settings,
    *,
    user_id: str,
    username: str = "agent",
    tenant_id: str | None = None,
    roles: tuple[str, ...] | list[str] = (),
    env_id: str,
    session_id: str,
    scope_space_ids: list[str] | tuple[str, ...] = (),
    ttl_seconds: int | None = None,
) -> str:
    """签发 agent token：平台 JWT 同构 + agent 专属 claims（14 文档 §6.2）。"""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "roles": list(roles) or ["user"],
        "iat": now,
        "exp": now + (ttl_seconds or settings.agent.mcp.token_ttl_s),
        "iss": "loomvec-agent",
        "jti": uuid.uuid4().hex,
        "agent_env_id": env_id,
        "agent_session_id": session_id,
        "scope_space_ids": list(scope_space_ids),
    }
    if tenant_id:
        claims["tenant_id"] = tenant_id
    return jwt.encode(claims, settings.auth.jwt_secret, algorithm=settings.auth.jwt_algorithm)


def decode_agent_token(settings: Settings, token: str) -> AgentTokenClaims:
    """验签并解析 claims；非法/过期抛 UnauthenticatedError。"""
    from loomvec.core.errors import UnauthenticatedError

    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.auth.jwt_secret,
            algorithms=[settings.auth.jwt_algorithm],
        )
    except jwt.PyJWTError as e:
        raise UnauthenticatedError(reason=str(e)) from e
    if claims.get("iss") != "loomvec-agent":
        raise UnauthenticatedError(reason="非 agent token")
    return AgentTokenClaims(
        user_id=claims["sub"],
        username=claims.get("username", "agent"),
        tenant_id=claims.get("tenant_id"),
        roles=tuple(claims.get("roles", [])),
        env_id=claims.get("agent_env_id", ""),
        session_id=claims.get("agent_session_id", ""),
        scope_space_ids=tuple(claims.get("scope_space_ids", [])),
        jti=claims.get("jti", ""),
        exp=int(claims.get("exp", 0)),
    )


async def is_token_revoked(redis, claims: AgentTokenClaims) -> bool:
    """JTI 黑名单查询（交接/离职即时失效）。"""
    if not claims.jti:
        return False
    return bool(await redis.exists(f"{JTI_BLACKLIST_PREFIX}{claims.jti}"))


async def revoke_agent_tokens(redis, tokens: list[tuple[str, int]]) -> None:
    """批量拉黑：tokens 为 (jti, 剩余有效期秒) 列表，TTL 与剩余有效期对齐。"""
    if not tokens:
        return
    pipe = redis.pipeline()
    for jti, remaining in tokens:
        if jti and remaining > 0:
            pipe.set(f"{JTI_BLACKLIST_PREFIX}{jti}", "1", ex=remaining)
    await pipe.execute()
