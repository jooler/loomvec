"""P5 开放服务 CORS 白名单测试（app.py 中间件挂载 × SecuritySettings 配置）。

语义基线（见 .env.example CORS 段）：
- 默认（显式白名单空 × 生产 dev_mode=False）：不挂中间件，任何响应无 CORS 头
  （回归现状）；dev_mode=True 时兜底放行 ^http://localhost:\\d+$；
- 显式白名单/正则命中放行，未命中拒绝（无 Access-Control-Allow-Origin）；
- OPTIONS 预检由 CORSMiddleware 短路：200 + Allow-Headers 含 X-API-Key
  （不进入限流与路由鉴权）；
- expose_headers：浏览器 JS 需可读 X-RateLimit-Limit / X-Request-Id；
- 凭据全在 header：不出现 Access-Control-Allow-Credentials。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loomvec.api.app import create_app
from loomvec.core.config import Settings

LOCAL_ORIGIN = "http://localhost:5173"  # 本地第三方 dev server（如 InkCop）
FOREIGN_ORIGIN = "https://evil.example.com"


def _make_client(dev_mode: bool = True, **security) -> TestClient:
    settings = Settings(_env_file=None, auth={"dev_mode": dev_mode}, security=dict(security))
    return TestClient(create_app(settings), raise_server_exceptions=False)


def test_dev_default_allows_localhost_regex():
    """dev 默认：localhost 任意端口放行（简单响应带 ACAO）。"""
    with _make_client() as client:
        resp = client.get("/healthz", headers={"Origin": LOCAL_ORIGIN})
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == LOCAL_ORIGIN


def test_dev_default_rejects_foreign_origin():
    with _make_client() as client:
        resp = client.get("/healthz", headers={"Origin": FOREIGN_ORIGIN})
        assert "access-control-allow-origin" not in resp.headers


def test_prod_unconfigured_has_no_cors_headers():
    """生产未配置 = 关闭（回归现状）：白名单命中与否都不出 CORS 头。"""
    with _make_client(dev_mode=False) as client:
        for origin in (LOCAL_ORIGIN, FOREIGN_ORIGIN):
            resp = client.get("/healthz", headers={"Origin": origin})
            assert resp.status_code == 200
            assert "access-control-allow-origin" not in resp.headers


def test_explicit_origins_allowlist():
    with _make_client(dev_mode=False, cors_allow_origins=[FOREIGN_ORIGIN]) as client:
        resp = client.get("/healthz", headers={"Origin": FOREIGN_ORIGIN})
        assert resp.headers["access-control-allow-origin"] == FOREIGN_ORIGIN
        resp = client.get("/healthz", headers={"Origin": LOCAL_ORIGIN})
        assert "access-control-allow-origin" not in resp.headers


def test_explicit_regex_allowlist():
    with _make_client(
        dev_mode=False,
        cors_allow_origins=[],
        cors_allow_origin_regex=r"^https://[a-z]+\.example\.com$",
    ) as client:
        resp = client.get("/healthz", headers={"Origin": "https://app.example.com"})
        assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
        resp = client.get("/healthz", headers={"Origin": "https://app.other.com"})
        assert "access-control-allow-origin" not in resp.headers


def test_preflight_short_circuit_with_custom_headers():
    """OPTIONS 预检短路：200、允许方法与自定义头（X-API-Key）。"""
    with _make_client() as client:
        resp = client.options(
            "/api/v1/search",
            headers={
                "Origin": LOCAL_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Api-Key, Content-Type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == LOCAL_ORIGIN
        allow = resp.headers["access-control-allow-headers"].lower()
        assert "x-api-key" in allow
        assert "authorization" in allow


def test_preflight_rejected_for_foreign_origin():
    with _make_client() as client:
        resp = client.options(
            "/api/v1/search",
            headers={
                "Origin": FOREIGN_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in resp.headers


def test_expose_headers_and_no_credentials():
    """expose_headers 生效 + 不带 credentials 头（凭据全在 header）。"""
    with _make_client() as client:
        resp = client.get("/healthz", headers={"Origin": LOCAL_ORIGIN})
        exposed = resp.headers["access-control-expose-headers"].lower()
        assert "x-ratelimit-limit" in exposed
        assert "x-request-id" in exposed
        assert "access-control-allow-credentials" not in resp.headers


def test_no_origin_header_passes_through_untouched():
    """非 CORS 请求（无 Origin）不受中间件影响。"""
    with _make_client() as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers


@pytest.mark.parametrize("origin", ["http://localhost:38080", "http://127.0.0.1:5173"])
def test_localhost_variants(origin):
    """127.0.0.1 不在默认正则内（显式区分 localhost）；localhost 任意端口命中。"""
    with _make_client() as client:
        resp = client.get("/healthz", headers={"Origin": origin})
        if origin == "http://localhost:38080":
            assert resp.headers["access-control-allow-origin"] == origin
        else:
            assert "access-control-allow-origin" not in resp.headers
