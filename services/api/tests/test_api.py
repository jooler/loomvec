"""P0-QA-01 API 测试：健康检查 / 错误契约 / dev JWT / 契约快照。

契约测试模式（schema 快照）在此确立：
- tests/__snapshots__/openapi.json 为契约基线（由 scripts/export_openapi.py 更新）；
- 任何 OpenAPI 变更必须显式更新快照并同步 sdk-ts（CI 双重校验）。
"""

from __future__ import annotations

import pathlib
from datetime import UTC

import pytest
from fastapi.testclient import TestClient

from loomvec.api.app import create_app
from loomvec.core.config import Settings

SNAPSHOT = pathlib.Path(__file__).parent / "__snapshots__" / "openapi.json"


@pytest.fixture()
def client() -> TestClient:
    settings = Settings(_env_file=None, auth={"dev_mode": True})
    # with 上下文触发 lifespan（离线安全：全部依赖惰性连接），
    # 使依赖 get_session 的路由（如 /me）拿到 app.state 装配
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_error_contract_unauthorized(client):
    resp = client.get("/api/v1/me")
    assert resp.status_code == 401
    body = resp.json()
    assert set(body) == {"code", "message", "details"}
    assert body["code"] == "unauthenticated"


def test_request_id_header(client):
    resp = client.get("/healthz", headers={"X-Request-Id": "test-rid-1"})
    assert resp.headers["x-request-id"] == "test-rid-1"


def test_dev_token_issue_and_me(client):
    resp = client.post("/api/v1/auth/dev/token", json={"username": "alice", "roles": ["user"]})
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def test_dev_token_disabled_in_prod():
    settings = Settings(_env_file=None, auth={"dev_mode": False})
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    resp = client.post("/api/v1/auth/dev/token", json={"username": "alice"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_openapi_snapshot_matches():
    """契约测试：当前 app 的 OpenAPI 与快照一致（漂移即失败，需显式更新）。"""
    app = create_app()
    current = app.openapi()
    assert SNAPSHOT.exists(), "快照缺失：先运行 uv run python scripts/export_openapi.py"
    import json

    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert current == expected, (
        "OpenAPI 契约漂移：运行 uv run python scripts/export_openapi.py 更新快照并同步 sdk-ts"
    )


def test_cursor_pagination():
    import uuid as uuid_mod
    from datetime import datetime

    from loomvec.core.db.pagination import Cursor

    id_ = uuid_mod.UUID("0198f6a2-7c1a-7000-8000-000000000001")
    c = Cursor(datetime(2026, 9, 12, tzinfo=UTC), id_)
    d = Cursor.decode(c.encode())
    assert d.created_at == c.created_at
    assert d.id == c.id
