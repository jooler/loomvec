"""本地账号密码登录集成测试（testcontainers PG；无 Docker 自动跳过，见 conftest）。

覆盖：
- 登录成功签发平台 JWT（/me 可用）；平台角色单源 DB（登录 token 不带角色）；
- 失败统一 401「用户名或密码错误」：密码错 / 账号不存在 / 未设密码 / 已停用；
- 失败计数锁定：达上限后 429（含正确密码），成功清零；
- dev 免密签发：已设密码的账号 403（含自带 super_admin 角色），未设密码的账号不受影响；
  查库失败时 fail-closed，不免密签发；dev_mode 关闭时 404；
- 登录成功 / 失败 / 锁定均写审计，且不记录失败请求里的用户名原文（账号不存在时）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from loomvec.api.routes.auth_password import LOGIN_MAX_FAILURES
from loomvec.core.db.models import User, UserStatus
from loomvec.core.passwords import hash_password

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]

PASSWORD = "s3cret-pass-phrase"


class FakeRedis:
    """登录锁用到的最小 Redis 子集。"""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def ttl(self, key):
        return self.ttls.get(key, -1)

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture()
async def client(pg_url):
    from loomvec.api.app import create_app
    from loomvec.core.config import Settings
    from loomvec.core.db.base import Base

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add_all(
            [
                User(username="pw-user", password_hash=hash_password(PASSWORD)),
                User(username="no-pw-user"),
                User(
                    username="disabled-user",
                    password_hash=hash_password(PASSWORD),
                    status=UserStatus.DISABLED,
                ),
            ]
        )
    settings = Settings(
        _env_file=None,
        auth={"dev_mode": True},
        postgres={"url": pg_url},
        security={"rate_limit_enabled": False},
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        c.app.state.redis = FakeRedis()
        yield c
    await engine.dispose()


def _login(client, username, password):
    return client.post("/api/v1/auth/login", json={"username": username, "password": password})


def test_login_success_issues_usable_token(client):
    resp = _login(client, "pw-user", PASSWORD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] > 0
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["username"] == "pw-user"
    assert "super_admin" not in me.json()["roles"]


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("pw-user", "wrong-password"),
        ("ghost", PASSWORD),
        ("no-pw-user", PASSWORD),
        ("disabled-user", PASSWORD),
    ],
)
def test_login_failures_are_indistinguishable(client, username, password):
    resp = _login(client, username, password)
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthenticated"
    assert resp.json()["message"] == "用户名或密码错误"


def test_login_locks_after_repeated_failures(client):
    for _ in range(LOGIN_MAX_FAILURES):
        assert _login(client, "pw-user", "wrong-password").status_code == 401
    # 锁定期内连正确密码也拒绝
    assert _login(client, "pw-user", PASSWORD).status_code == 429
    # 锁按用户名隔离
    assert _login(client, "ghost", "x").status_code == 401


def test_login_success_resets_failure_count(client):
    for _ in range(LOGIN_MAX_FAILURES - 1):
        _login(client, "pw-user", "wrong-password")
    assert _login(client, "pw-user", PASSWORD).status_code == 200
    for _ in range(LOGIN_MAX_FAILURES - 1):
        assert _login(client, "pw-user", "wrong-password").status_code == 401
    assert _login(client, "pw-user", PASSWORD).status_code == 200


def test_dev_token_rejected_for_password_accounts(client):
    resp = client.post("/api/v1/auth/dev/token", json={"username": "pw-user"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "permission_denied"


def test_dev_token_still_works_for_passwordless_accounts(client):
    assert client.post("/api/v1/auth/dev/token", json={"username": "no-pw-user"}).status_code == 200
    assert client.post("/api/v1/auth/dev/token", json={"username": "brand-new"}).status_code == 200


def test_dev_token_rejects_super_admin_roles_for_password_accounts(client):
    resp = client.post(
        "/api/v1/auth/dev/token", json={"username": "pw-user", "roles": ["super_admin"]}
    )
    assert resp.status_code == 403


def test_dev_token_fails_closed_when_user_lookup_fails(client, monkeypatch):
    """查库失败不能放行：否则库一故障就能对已设密码的账号免密签发。"""
    from loomvec.core.db.repos import UserRepo

    async def boom(self, username):
        raise RuntimeError("db down")

    monkeypatch.setattr(UserRepo, "get_by_username", boom)
    resp = client.post(
        "/api/v1/auth/dev/token", json={"username": "pw-user", "roles": ["super_admin"]}
    )
    assert resp.status_code >= 500
    assert "access_token" not in resp.json()


def test_dev_token_is_404_when_dev_mode_off(client, pg_url):
    from loomvec.api.app import create_app
    from loomvec.core.config import Settings

    off = Settings(
        _env_file=None,
        auth={"dev_mode": False},
        postgres={"url": pg_url},
        security={"rate_limit_enabled": False},
    )
    with TestClient(create_app(off), raise_server_exceptions=False) as c:
        assert c.post("/api/v1/auth/dev/token", json={"username": "pw-user"}).status_code == 404


async def _audit_rows(pg_url):
    from sqlalchemy import text

    engine = create_async_engine(pg_url)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT action, after_value FROM audit_log ORDER BY created_at")
            )
        ).all()
    await engine.dispose()
    return rows


async def test_login_events_are_audited(client, pg_url):
    _login(client, "pw-user", "wrong-password")
    _login(client, "ghost", "oops-this-is-a-password")
    assert _login(client, "pw-user", PASSWORD).status_code == 200
    rows = await _audit_rows(pg_url)
    actions = [r[0] for r in rows]
    assert actions == ["auth.login_failed", "auth.login_failed", "auth.login_success"]
    # 账号不存在时不落用户名原文（可能是误填进用户名框的密码）
    assert rows[1][1] is None
    assert rows[0][1] == {"username": "pw-user"}
    assert "oops-this-is-a-password" not in str(rows)


async def test_lock_is_audited_once_and_counter_always_has_ttl(client, pg_url):
    for _ in range(LOGIN_MAX_FAILURES + 3):
        _login(client, "pw-user", "wrong-password")
    actions = [r[0] for r in await _audit_rows(pg_url)]
    assert actions.count("auth.login_failed") == LOGIN_MAX_FAILURES
    assert actions.count("auth.login_locked") == 1  # 之后被拒的请求不再逐条落库
    redis = client.app.state.redis
    assert redis.ttls  # 计数键必须带 TTL，否则账号会被永久锁死
