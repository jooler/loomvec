"""P5 单测：agent token 签发/校验/JTI 黑名单（14 文档 §6.2）。"""

from __future__ import annotations

import time

import jwt
import pytest

from loomvec.api.services.agent_tokens import (
    decode_agent_token,
    is_token_revoked,
    issue_agent_token,
)


class _StubRedis:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def exists(self, key: str) -> int:
        return 1 if key in self.keys else 0


def _settings():
    from loomvec.core.config import Settings

    return Settings(auth={"jwt_secret": "test-secret"})


def test_agent_token_roundtrip():
    s = _settings()
    token = issue_agent_token(
        s,
        user_id="11111111-1111-1111-1111-111111111111",
        tenant_id="22222222-2222-2222-2222-222222222222",
        env_id="env-1",
        session_id="sess-1",
        scope_space_ids=["aaaaaaaa-0000-0000-0000-000000000000"],
        ttl_seconds=60,
    )
    claims = decode_agent_token(s, token)
    assert claims.user_id == "11111111-1111-1111-1111-111111111111"
    assert claims.env_id == "env-1"
    assert claims.session_id == "sess-1"
    assert claims.scope_list == ["aaaaaaaa-0000-0000-0000-000000000000"]
    assert claims.jti


def test_agent_token_expired_rejected():
    s = _settings()
    token = issue_agent_token(s, user_id="u", env_id="e", session_id="s", ttl_seconds=-1)
    from loomvec.core.errors import UnauthenticatedError

    with pytest.raises(UnauthenticatedError):
        decode_agent_token(s, token)


def test_platform_jwt_rejected_as_agent_token():
    s = _settings()
    now = int(time.time())
    platform = jwt.encode(
        {"sub": "u", "iat": now, "exp": now + 60, "iss": "loomvec-dev"},
        s.auth.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(Exception, match="未认证"):
        decode_agent_token(s, platform)


@pytest.mark.asyncio
async def test_agent_token_rejected_as_platform_credential():
    """反向重放：agent token 不得经平台 exchange 充当登录态（P1 修复回归）。"""
    from loomvec.api.auth import PlatformTokenProvider

    s = _settings()
    token = issue_agent_token(
        s, user_id="11111111-1111-1111-1111-111111111111", env_id="e", session_id="s"
    )
    provider = PlatformTokenProvider(s)
    from loomvec.core.errors import UnauthenticatedError

    with pytest.raises(UnauthenticatedError):
        await provider.exchange({"token": token})


@pytest.mark.asyncio
async def test_agent_token_without_authorization_header_rejected_by_mcp_asgi():
    """MCP ASGI 包裹层：无 Authorization 头 → 401 JSON（不抛 TypeError）。"""

    from loomvec.api.routes.mcp import _AgentAuthASGI

    async def _downstream(scope, receive, send):  # pragma: no cover — 不应到达
        raise AssertionError("请求不应透传到下游")

    app = _AgentAuthASGI(_downstream, ctx=None)
    sent: list = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/mcp",
        "headers": [(b"content-type", b"application/json")],
    }
    await app(scope, _receive, _send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
async def test_jti_blacklist():
    s = _settings()
    token = issue_agent_token(s, user_id="u", env_id="e", session_id="s")
    claims = decode_agent_token(s, token)
    redis = _StubRedis()
    assert not await is_token_revoked(redis, claims)
    redis.keys[f"agent:jti:{claims.jti}"] = "1"
    assert await is_token_revoked(redis, claims)
