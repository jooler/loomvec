"""全局限流中间件的分桶键：API Key（X-API-Key 或 Bearer lv_...）按 key 哈希，其余按来源 IP。

中间件在鉴权之前执行，identity 恒为空；Bearer 里的 PAT 若按 IP 计数，反向代理后的所有
MCP / REST 客户端会共用一个桶。
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from loomvec.api.ratelimit import RATE_LIMIT_KEY_PREFIX, RateLimitMiddleware


class _Redis:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def incr(self, key: str) -> int:
        self.keys.append(key)
        return 1

    async def expire(self, key: str, ttl: int) -> None:
        return None


def _app(redis: _Redis) -> Starlette:
    settings = SimpleNamespace(
        security=SimpleNamespace(rate_limit_enabled=True, rate_limit_per_min=300)
    )
    app = Starlette(routes=[Route("/", lambda request: PlainTextResponse("ok"))])
    app.state.redis = redis
    app.add_middleware(RateLimitMiddleware, settings=settings)
    return app


async def _subjects(headers: dict[str, str]) -> list[str]:
    redis = _Redis()
    transport = httpx.ASGITransport(app=_app(redis), client=("10.1.2.3", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/", headers=headers)).status_code == 200
    prefix = f"{RATE_LIMIT_KEY_PREFIX}:"
    return [k[len(prefix) :].rsplit(":", 1)[0] for k in redis.keys]


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def test_x_api_key_buckets_by_key_hash():
    assert await _subjects({"x-api-key": "lv_abc"}) == [_digest("lv_abc")]


async def test_bearer_api_key_buckets_by_the_same_hash():
    assert await _subjects({"authorization": "Bearer lv_abc"}) == [_digest("lv_abc")]


async def test_bearer_jwt_and_anonymous_fall_back_to_client_ip():
    assert await _subjects({"authorization": "Bearer eyJhbGciOi.payload.sig"}) == ["10.1.2.3"]
    assert await _subjects({}) == ["10.1.2.3"]


async def test_plaintext_key_never_reaches_redis_key():
    redis = _Redis()
    transport = httpx.ASGITransport(app=_app(redis), client=("10.1.2.3", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get("/", headers={"authorization": "Bearer lv_secretvalue"})
    assert not any("secretvalue" in k for k in redis.keys)
