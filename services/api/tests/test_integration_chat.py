"""chat 用户级会话集成测试（testcontainers PG；无 Docker 自动跳过，见 conftest）。

覆盖会话用户级化（迁移 0006）后的核心契约：
- scope_space_ids 合法性：创建/更新携带非成员空间 → 422（请求参数不合法）；
- 会话按用户隔离：他人会话列表不可见，直连读取/更新/删除 → 422 会话不存在；
- PATCH：重命名、范围清空（空列表 = 我的全部空间）、空标题拒绝（保护首问自动命名）；
- `_resolve_chat_scope`：召回范围 × 当前可见空间取交集，成员被移出后该空间即时剔除。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


@pytest.fixture()
async def env(pg_url):
    """建表 + 返回 (TestClient 工厂, 独立会话工厂)。

    TestClient 上下文触发 lifespan；dev 用户在首个携带 token 的请求中自动落库。
    """
    from loomvec.api.app import create_app
    from loomvec.core.config import Settings
    from loomvec.core.constants import SEED_TENANT_ID
    from loomvec.core.db.base import Base
    from loomvec.core.db.models import Tenant, TenantStatus

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # dev 用户落库挂种子租户（identity.resolve_user_row），create_all 不含种子数据
    async with factory() as session, session.begin():
        session.add(
            Tenant(id=uuid.UUID(SEED_TENANT_ID), name="chat-it-tenant", status=TenantStatus.ACTIVE)
        )

    settings = Settings(
        _env_file=None,
        auth={"dev_mode": True},
        postgres={"url": pg_url},
        security={"rate_limit_enabled": False},
    )

    def make_client() -> TestClient:
        return TestClient(create_app(settings), raise_server_exceptions=False)

    yield make_client, factory
    await engine.dispose()


async def _user_id(factory, username: str) -> uuid.UUID:
    from loomvec.core.db.models import User

    async with factory() as session:
        return (
            (await session.execute(select(User.id).where(User.username == username)))
            .scalars()
            .one()
        )


async def _make_space(factory, prefix: str, members: dict[uuid.UUID, str]) -> uuid.UUID:
    from loomvec.core.db.models import Space, SpaceMember, SpaceRole

    async with factory() as session, session.begin():
        space = Space(slug=f"{prefix}-{uuid.uuid4().hex[:8]}", name=prefix)
        session.add(space)
        await session.flush()
        for user_id, role in members.items():
            session.add(SpaceMember(space_id=space.id, user_id=user_id, role=SpaceRole(role)))
        return space.id


def _auth(client: TestClient, username: str) -> dict[str, str]:
    resp = client.post("/api/v1/auth/dev/token", json={"username": username})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_create_session_scope_rejects_non_member_space(env):
    make_client, factory = env
    with make_client() as client:
        headers = _auth(client, "chat-scope-alice")
        assert client.get("/api/v1/chat/sessions", headers=headers).status_code == 200
        alice = await _user_id(factory, "chat-scope-alice")
        own = await _make_space(factory, "chat-scope-own", {alice: "owner"})
        foreign = await _make_space(factory, "chat-scope-foreign", {})

        resp = client.post(
            "/api/v1/chat/sessions",
            headers=headers,
            json={"scope_space_ids": [str(own)]},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["scope_space_ids"] == [str(own)]

        resp = client.post(
            "/api/v1/chat/sessions",
            headers=headers,
            json={"scope_space_ids": [str(foreign)]},
        )
        assert resp.status_code == 422
        assert "无权访问的空间" in resp.json()["message"]


async def test_session_isolated_per_user(env):
    make_client, _factory = env
    with make_client() as client:
        alice_headers = _auth(client, "chat-iso-alice")
        bob_headers = _auth(client, "chat-iso-bob")
        resp = client.post("/api/v1/chat/sessions", headers=alice_headers, json={})
        assert resp.status_code == 201, resp.text
        session_id = resp.json()["session_id"]

        # bob 的列表看不到 alice 的会话
        resp = client.get("/api/v1/chat/sessions", headers=bob_headers)
        assert resp.status_code == 200
        assert all(item["session_id"] != session_id for item in resp.json()["items"])

        # bob 直连读取/更新/删除 → 一律"会话不存在"（422，防探测）
        for method, path, kwargs in (
            ("get", f"/api/v1/chat/sessions/{session_id}/messages", {}),
            ("patch", f"/api/v1/chat/sessions/{session_id}", {"json": {"title": "越权"}}),
            ("delete", f"/api/v1/chat/sessions/{session_id}", {}),
        ):
            resp = getattr(client, method)(path, headers=bob_headers, **kwargs)
            assert resp.status_code == 422, (method, resp.text)
            assert resp.json()["code"] == "validation_error"


async def test_patch_session_title_and_scope(env):
    make_client, factory = env
    with make_client() as client:
        headers = _auth(client, "chat-patch-alice")
        assert client.get("/api/v1/chat/sessions", headers=headers).status_code == 200
        alice = await _user_id(factory, "chat-patch-alice")
        s1 = await _make_space(factory, "chat-patch-s1", {alice: "owner"})
        s2 = await _make_space(factory, "chat-patch-s2", {alice: "editor"})
        sid = client.post(
            "/api/v1/chat/sessions",
            headers=headers,
            json={"scope_space_ids": [str(s1), str(s2)]},
        ).json()["session_id"]

        # 重命名
        resp = client.patch(
            f"/api/v1/chat/sessions/{sid}", headers=headers, json={"title": "发布会准备"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "发布会准备"

        # 范围清空 = 我的全部空间（合法值，与 None=不改动区分）
        resp = client.patch(
            f"/api/v1/chat/sessions/{sid}", headers=headers, json={"scope_space_ids": []}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scope_space_ids"] == []

        # 范围更新同样校验成员资格
        foreign = await _make_space(factory, "chat-patch-foreign", {})
        resp = client.patch(
            f"/api/v1/chat/sessions/{sid}",
            headers=headers,
            json={"scope_space_ids": [str(foreign)]},
        )
        assert resp.status_code == 422

        # 空白标题拒绝（空标题会破坏首问自动命名）
        resp = client.patch(f"/api/v1/chat/sessions/{sid}", headers=headers, json={"title": "   "})
        assert resp.status_code == 422


async def test_resolve_scope_excludes_removed_membership(env):
    from loomvec.api.routes.chat import _resolve_chat_scope
    from loomvec.core.db.models import ChatSession, SpaceMember

    make_client, factory = env
    with make_client() as client:
        headers = _auth(client, "chat-leave-carol")
        assert client.get("/api/v1/chat/sessions", headers=headers).status_code == 200
        carol = await _user_id(factory, "chat-leave-carol")
        s1 = await _make_space(factory, "chat-leave-s1", {carol: "owner"})
        s2 = await _make_space(factory, "chat-leave-s2", {carol: "editor"})
        sid = client.post(
            "/api/v1/chat/sessions",
            headers=headers,
            json={"scope_space_ids": [str(s1), str(s2)]},
        ).json()["session_id"]

        # 成员在时：范围 = scope ∩ 可见 = 全集
        async with factory() as session:
            row = await session.get(ChatSession, uuid.UUID(sid))
            effective, _tenant_id, roles = await _resolve_chat_scope(session, carol, row)
            assert set(effective) == {s1, s2}
            assert set(roles) == {s1, s2}

        # 被移出 s2 后：有效范围即时剔除（无需改会话）
        async with factory() as session, session.begin():
            await session.execute(
                delete(SpaceMember).where(SpaceMember.space_id == s2, SpaceMember.user_id == carol)
            )
        async with factory() as session:
            row = await session.get(ChatSession, uuid.UUID(sid))
            effective, _tenant_id, roles = await _resolve_chat_scope(session, carol, row)
            assert effective == [s1]
            assert set(roles) == {s1}

        # 范围为空（全部我的空间）时跟随可见空间收缩
        async with factory() as session, session.begin():
            row = await session.get(ChatSession, uuid.UUID(sid))
            row.scope_space_ids = []
            await session.flush()
        async with factory() as session:
            row = await session.get(ChatSession, uuid.UUID(sid))
            effective, _tenant_id, _roles = await _resolve_chat_scope(session, carol, row)
            assert effective == [s1]
