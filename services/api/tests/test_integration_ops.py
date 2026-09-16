"""运营端与公共空间集成测试（testcontainers PG；无 Docker 自动跳过，见 conftest）。

覆盖迁移 0007（用户分组/空间分组可见性/空间链接/space_type=public）后的核心契约：
- 运营域闸门：普通用户访问 /api/v1/ops/* → 403；operator 可用；
- 公共空间仅运营端可创建：用户端 POST /spaces 携带 space_type=public → 422；
- 用户分组：创建/按用户名加人/重复加入 409；
- 分组可见性：PUT 全量替换勾选；未勾选分组用户不可链接（403）；
- 用户端链接开关：可见用户可链接/断开；链接后该空间可入问答召回范围
  （scope_space_ids 合法），断开后即拒绝；
- 内容边界：非成员用户不可进入公共空间浏览（GET /spaces/{id} → 403）；
- 运营者成员兜底：非创建者的 operator 打开空间详情后自动成为 owner 成员。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from loomvec.core.errors import ValidationError

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


@pytest.fixture()
async def env(pg_url):
    """建表 + 返回 (TestClient 工厂, 独立会话工厂)。"""
    from loomvec.api.app import create_app
    from loomvec.core.config import Settings
    from loomvec.core.constants import SEED_TENANT_ID
    from loomvec.core.db.base import Base
    from loomvec.core.db.models import Tenant, TenantStatus

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add(
            Tenant(id=uuid.UUID(SEED_TENANT_ID), name="ops-it-tenant", status=TenantStatus.ACTIVE)
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


def _auth(client: TestClient, username: str, roles: list[str] | None = None) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/dev/token", json={"username": username, "roles": roles or ["user"]}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _user_id(factory, username: str) -> uuid.UUID:
    from loomvec.core.db.models import User

    async with factory() as session:
        return (
            (await session.execute(select(User.id).where(User.username == username)))
            .scalars()
            .one()
        )


async def _member_role(factory, space_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
    from loomvec.core.db.models import SpaceMember

    async with factory() as session:
        row = (
            await session.execute(
                select(SpaceMember.role).where(
                    SpaceMember.space_id == space_id, SpaceMember.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        return row.value if row is not None else None


async def test_ops_guard_and_public_space_flow(env):
    make_client, factory = env
    with make_client() as client:
        operator = _auth(client, "ops-it-op", roles=["operator"])
        # dev 用户落库（后续按用户名加分组需要 user 行存在）
        assert client.get("/api/v1/me", headers=operator).status_code == 200
        alice = _auth(client, "ops-it-alice")
        bob = _auth(client, "ops-it-bob")
        assert client.get("/api/v1/me", headers=alice).status_code == 200
        assert client.get("/api/v1/me", headers=bob).status_code == 200

        # ---- 闸门：普通用户不可进运营域 ----
        assert client.get("/api/v1/ops/spaces", headers=alice).status_code == 403
        assert client.get("/api/v1/ops/groups", headers=bob).status_code == 403

        # ---- 公共空间：仅运营端可创建；用户端 space_type=public 拒绝 ----
        resp = client.post(
            "/api/v1/ops/spaces",
            headers=operator,
            json={"name": "公司公共知识库", "description": "运营维护"},
        )
        assert resp.status_code == 201, resp.text
        space_id = resp.json()["id"]
        from loomvec.core.db.models import Space

        async with factory() as session:
            row = (
                await session.execute(
                    select(Space.space_type).where(Space.id == uuid.UUID(space_id))
                )
            ).scalar_one()
        assert row == "public"

        resp = client.post(
            "/api/v1/spaces", headers=alice, json={"name": "越权公共空间", "space_type": "public"}
        )
        assert resp.status_code == 422, resp.text

        # ---- 用户分组：创建 + 加人（重复 409）----
        resp = client.post("/api/v1/ops/groups", headers=operator, json={"name": "全员"})
        assert resp.status_code == 201, resp.text
        group_id = resp.json()["id"]
        resp = client.post("/api/v1/ops/groups", headers=operator, json={"name": "全员"})
        assert resp.status_code == 409, resp.text

        for username in ("ops-it-alice", "ops-it-bob"):
            resp = client.post(
                f"/api/v1/ops/groups/{group_id}/members",
                headers=operator,
                json={"username": username},
            )
            assert resp.status_code == 201, resp.text
        resp = client.post(
            f"/api/v1/ops/groups/{group_id}/members",
            headers=operator,
            json={"username": "ops-it-alice"},
        )
        assert resp.status_code == 409, resp.text

        # ---- 可见性：勾选分组前，用户不可见不可链接 ----
        resp = client.get("/api/v1/public-spaces", headers=alice)
        assert resp.status_code == 200 and resp.json()["items"] == []
        resp = client.put(
            f"/api/v1/public-spaces/{space_id}/link", headers=alice, json={"linked": True}
        )
        assert resp.status_code == 403, resp.text

        resp = client.put(
            f"/api/v1/ops/spaces/{space_id}/visibility",
            headers=operator,
            json={"group_ids": [group_id]},
        )
        assert resp.status_code == 200, resp.text
        vis = resp.json()["items"]
        checked = next(i for i in vis if i["group_id"] == group_id)
        assert checked["visible"] is True

        # ---- 用户端：可见列表 + 链接开关 + 内容边界 ----
        resp = client.get("/api/v1/public-spaces", headers=alice)
        items = resp.json()["items"]
        assert len(items) == 1 and items[0]["id"] == space_id and items[0]["linked"] is False

        resp = client.put(
            f"/api/v1/public-spaces/{space_id}/link", headers=alice, json={"linked": True}
        )
        assert resp.status_code == 200 and resp.json()["linked"] is True

        # 非成员不可进入公共空间浏览内容
        assert client.get(f"/api/v1/spaces/{space_id}", headers=alice).status_code == 403

        # 链接后：该公共空间可入智能体会话召回范围（直接调 agent 服务域的
        # scope 校验语义；facade 转发 e2e 走真机验收，测试库与 agent 服务
        # 进程不共库，跨进程转发不在本测试范围）
        from loomvec.agent.routes import _assert_member_spaces as _agent_scope_check

        alice_id = await _user_id(factory, "ops-it-alice")
        async with factory() as session:
            await _agent_scope_check(session, alice_id, [uuid.UUID(space_id)])  # 不抛 = 可入范围

        # 断开后：即时失效
        resp = client.put(
            f"/api/v1/public-spaces/{space_id}/link", headers=alice, json={"linked": False}
        )
        assert resp.status_code == 200
        async with factory() as session:
            with pytest.raises(ValidationError):
                await _agent_scope_check(session, alice_id, [uuid.UUID(space_id)])

        # ---- 未加入分组的用户不可见（carol 为对照组）----
        carol = _auth(client, "ops-it-carol")
        assert client.get("/api/v1/me", headers=carol).status_code == 200
        resp = client.get("/api/v1/public-spaces", headers=carol)
        assert resp.status_code == 200 and resp.json()["items"] == []

        # ---- 运营者成员兜底：第二运营者打开详情即成为 owner 成员 ----
        operator2 = _auth(client, "ops-it-op2", roles=["operator"])
        assert client.get("/api/v1/me", headers=operator2).status_code == 200
        op1_id = await _user_id(factory, "ops-it-op")
        assert await _member_role(factory, uuid.UUID(space_id), op1_id) == "owner"
        resp = client.get(f"/api/v1/ops/spaces/{space_id}", headers=operator2)
        assert resp.status_code == 200, resp.text
        op2_id = await _user_id(factory, "ops-it-op2")
        assert await _member_role(factory, uuid.UUID(space_id), op2_id) == "owner"

        # ---- 分组成员移除 → 可见性与链接即时失效（惰性求交，不留死链接）----
        alice_id = await _user_id(factory, "ops-it-alice")
        resp = client.put(
            f"/api/v1/public-spaces/{space_id}/link", headers=alice, json={"linked": True}
        )
        assert resp.status_code == 200
        resp = client.delete(f"/api/v1/ops/groups/{group_id}/members/{alice_id}", headers=operator)
        assert resp.status_code == 204
        resp = client.get("/api/v1/public-spaces", headers=alice)
        assert resp.status_code == 200 and resp.json()["items"] == []
        # 分组移除 → 可见性丧失 → 链接失效（scope 校验不再通过）
        async with factory() as session:
            with pytest.raises(ValidationError):
                await _agent_scope_check(session, alice_id, [uuid.UUID(space_id)])
