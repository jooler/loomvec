"""知识库可见性回归测试（testcontainers PG；无 Docker 自动跳过，见 conftest）。

2026-10-04 提交 94e4dd0 曾把"空间权限"和"知识库 tag_member 权限"实现成交集：
viewer 必须对文献挂载的每个知识库节点单独有 tag_member 授权才能看到对应文献，
但上线时没有任何机制给存量用户批量授予这张新权限表，导致所有空间（含公共空间）
的所有 viewer 看不到任何内容。本文件固定修复后的预期行为：

- 真实空间成员 viewer、公共空间虚拟 viewer，不需要 tag_member 就能看到空间内
  全部文献、知识库树、`/spaces/{id}/tags`；
- 写权限路径（PATCH/DELETE 文献、新建知识库节点）仍然只对 editor/owner 开放；
- 知识库成员名单接口（谁能看到某个知识库节点的管理者花名册）仍然要求 viewer
  自己对该节点有 tag_role，这是有意保留的、和文献可见性无关的更严格闸门；
- review_required 空间里，viewer 依然看不到未审核文献（确认这次放开没有连带
  松掉审核过滤）。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
            Tenant(id=uuid.UUID(SEED_TENANT_ID), name="kb-it-tenant", status=TenantStatus.ACTIVE)
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


async def test_space_viewer_sees_assets_and_knowledge_bases_without_tag_member(env):
    make_client, factory = env
    with make_client() as client:
        owner = _auth(client, "kb-it-owner")
        viewer = _auth(client, "kb-it-viewer")
        assert client.get("/api/v1/me", headers=owner).status_code == 200
        assert client.get("/api/v1/me", headers=viewer).status_code == 200
        owner_id = await _user_id(factory, "kb-it-owner")
        viewer_id = await _user_id(factory, "kb-it-viewer")

        from loomvec.core.constants import SEED_TENANT_ID
        from loomvec.core.db.models import Asset, AssetTag, Space, SpaceMember, SpaceRole, Tag

        tenant_id = uuid.UUID(SEED_TENANT_ID)
        async with factory() as session, session.begin():
            space = Space(
                tenant_id=tenant_id,
                slug="kb-it-space",
                name="KB IT",
                review_required=False,
                owner_id=owner_id,
            )
            session.add(space)
            await session.flush()
            session.add_all(
                [
                    SpaceMember(
                        space_id=space.id,
                        user_id=owner_id,
                        role=SpaceRole.OWNER,
                        tenant_id=tenant_id,
                    ),
                    SpaceMember(
                        space_id=space.id,
                        user_id=viewer_id,
                        role=SpaceRole.VIEWER,
                        tenant_id=tenant_id,
                    ),
                ]
            )
            tag = Tag(tenant_id=tenant_id, space_id=space.id, name="CRISPR")
            session.add(tag)
            await session.flush()
            asset = Asset(
                tenant_id=tenant_id,
                space_id=space.id,
                name="a.pdf",
                mime_type="application/pdf",
                ext=".pdf",
                size_bytes=1,
                status="ready",
                created_by=owner_id,
            )
            session.add(asset)
            await session.flush()
            session.add(
                AssetTag(tenant_id=tenant_id, asset_id=asset.id, tag_id=tag.id, source="manual")
            )
            space_id, tag_id, asset_id = space.id, tag.id, asset.id

        # ---- viewer 没有任何 tag_member，仍能看到空间内全部内容 ----
        resp = client.get(f"/api/v1/assets?space_id={space_id}", headers=viewer)
        assert resp.status_code == 200, resp.text
        assert [i["id"] for i in resp.json()["items"]] == [str(asset_id)]

        resp = client.get("/api/v1/assets", headers=viewer)  # 聚合列表
        assert resp.status_code == 200, resp.text
        assert str(asset_id) in [i["id"] for i in resp.json()["items"]]

        resp = client.get(f"/api/v1/assets/{asset_id}", headers=viewer)
        assert resp.status_code == 200, resp.text
        assert [t["id"] for t in resp.json()["tags"]] == [str(tag_id)]

        resp = client.get(f"/api/v1/spaces/{space_id}/knowledge-bases", headers=viewer)
        assert resp.status_code == 200, resp.text
        items = {i["id"]: i for i in resp.json()["items"]}
        assert tag_id and str(tag_id) in items
        assert items[str(tag_id)]["role"] is None  # 可见但没有管理角色

        resp = client.get(f"/api/v1/spaces/{space_id}/tags", headers=viewer)
        assert resp.status_code == 200, resp.text
        assert str(tag_id) in [t["id"] for t in resp.json()]

        # ---- 回归护栏：写权限路径不受影响，viewer 依旧被拒 ----
        assert (
            client.patch(
                f"/api/v1/assets/{asset_id}", headers=viewer, json={"name": "renamed.pdf"}
            ).status_code
            == 403
        )
        assert client.delete(f"/api/v1/assets/{asset_id}", headers=viewer).status_code == 403
        assert (
            client.post(
                f"/api/v1/spaces/{space_id}/knowledge-bases",
                headers=viewer,
                json={"name": "新节点"},
            ).status_code
            == 403
        )

        # ---- 知识库成员名单接口：这个闸门有意保留，不随文献可见性放开 ----
        assert (
            client.get(
                f"/api/v1/spaces/{space_id}/knowledge-bases/{tag_id}/members", headers=viewer
            ).status_code
            == 403
        )
        resp = client.put(
            f"/api/v1/spaces/{space_id}/knowledge-bases/{tag_id}/members",
            headers=owner,
            json={"username": "kb-it-viewer", "role": "viewer"},
        )
        assert resp.status_code == 200, resp.text
        resp = client.get(
            f"/api/v1/spaces/{space_id}/knowledge-bases/{tag_id}/members", headers=viewer
        )
        assert resp.status_code == 200, resp.text
        assert [m["username"] for m in resp.json()["items"]] == ["kb-it-viewer"]


async def test_space_viewer_still_blocked_from_unreviewed_assets(env):
    """OR 语义放开的是"要不要 tag_member"，不是审核过滤：review_required 空间里
    viewer 依旧看不到未审核文献。"""
    make_client, factory = env
    with make_client() as client:
        owner = _auth(client, "kb-it-rv-owner")
        viewer = _auth(client, "kb-it-rv-viewer")
        assert client.get("/api/v1/me", headers=owner).status_code == 200
        assert client.get("/api/v1/me", headers=viewer).status_code == 200
        owner_id = await _user_id(factory, "kb-it-rv-owner")
        viewer_id = await _user_id(factory, "kb-it-rv-viewer")

        from loomvec.core.constants import SEED_TENANT_ID
        from loomvec.core.db.models import Asset, ReviewStatus, Space, SpaceMember, SpaceRole

        tenant_id = uuid.UUID(SEED_TENANT_ID)
        async with factory() as session, session.begin():
            space = Space(
                tenant_id=tenant_id,
                slug="kb-it-review-space",
                name="KB IT Review",
                review_required=True,
                owner_id=owner_id,
            )
            session.add(space)
            await session.flush()
            session.add_all(
                [
                    SpaceMember(
                        space_id=space.id,
                        user_id=owner_id,
                        role=SpaceRole.OWNER,
                        tenant_id=tenant_id,
                    ),
                    SpaceMember(
                        space_id=space.id,
                        user_id=viewer_id,
                        role=SpaceRole.VIEWER,
                        tenant_id=tenant_id,
                    ),
                ]
            )
            asset = Asset(
                tenant_id=tenant_id,
                space_id=space.id,
                name="pending.pdf",
                mime_type="application/pdf",
                ext=".pdf",
                size_bytes=1,
                status="ready",
                created_by=owner_id,
                review_status=ReviewStatus.PENDING_REVIEW,
            )
            session.add(asset)
            await session.flush()
            space_id, asset_id = space.id, asset.id

        resp = client.get(f"/api/v1/assets?space_id={space_id}", headers=viewer)
        assert resp.status_code == 200, resp.text
        assert resp.json()["items"] == []
        assert client.get(f"/api/v1/assets/{asset_id}", headers=viewer).status_code == 404

        # owner 本来就能看全部审核状态
        resp = client.get(f"/api/v1/assets?space_id={space_id}", headers=owner)
        assert [i["id"] for i in resp.json()["items"]] == [str(asset_id)]


async def test_public_space_virtual_viewer_sees_assets_and_knowledge_bases(env):
    """复现截图场景：公共空间分组可见的虚拟 viewer（无 space_member 行），
    不需要 tag_member 也能看到空间内全部文献和知识库树。"""
    make_client, factory = env
    with make_client() as client:
        operator = _auth(client, "kb-it-pub-op", roles=["operator"])
        alice = _auth(client, "kb-it-pub-alice")
        assert client.get("/api/v1/me", headers=operator).status_code == 200
        assert client.get("/api/v1/me", headers=alice).status_code == 200

        resp = client.post(
            "/api/v1/ops/spaces", headers=operator, json={"name": "最新文章", "description": "公共"}
        )
        assert resp.status_code == 201, resp.text
        space_id = uuid.UUID(resp.json()["id"])

        resp = client.post("/api/v1/ops/groups", headers=operator, json={"name": "kb-it-pub-group"})
        assert resp.status_code == 201, resp.text
        group_id = resp.json()["id"]
        resp = client.post(
            f"/api/v1/ops/groups/{group_id}/members",
            headers=operator,
            json={"username": "kb-it-pub-alice"},
        )
        assert resp.status_code == 201, resp.text
        resp = client.put(
            f"/api/v1/ops/spaces/{space_id}/visibility",
            headers=operator,
            json={"group_ids": [group_id]},
        )
        assert resp.status_code == 200, resp.text

        owner_id = await _user_id(factory, "kb-it-pub-op")
        from loomvec.core.constants import SEED_TENANT_ID
        from loomvec.core.db.models import Asset, AssetTag, Tag

        tenant_id = uuid.UUID(SEED_TENANT_ID)
        async with factory() as session, session.begin():
            tag = Tag(tenant_id=tenant_id, space_id=space_id, name="最新文章-分类")
            session.add(tag)
            await session.flush()
            asset = Asset(
                tenant_id=tenant_id,
                space_id=space_id,
                name="article.pdf",
                mime_type="application/pdf",
                ext=".pdf",
                size_bytes=1,
                status="ready",
                created_by=owner_id,
            )
            session.add(asset)
            await session.flush()
            session.add(
                AssetTag(tenant_id=tenant_id, asset_id=asset.id, tag_id=tag.id, source="manual")
            )
            tag_id, asset_id = tag.id, asset.id

        # alice 只是分组可见，没有 space_member 行，也没有任何 tag_member
        resp = client.get(f"/api/v1/assets?space_id={space_id}", headers=alice)
        assert resp.status_code == 200, resp.text
        assert [i["id"] for i in resp.json()["items"]] == [str(asset_id)]

        resp = client.get(f"/api/v1/spaces/{space_id}/knowledge-bases", headers=alice)
        assert resp.status_code == 200, resp.text
        assert str(tag_id) in {i["id"] for i in resp.json()["items"]}

        resp = client.get(f"/api/v1/spaces/{space_id}/tags", headers=alice)
        assert resp.status_code == 200, resp.text
        assert str(tag_id) in [t["id"] for t in resp.json()]

        # 写权限依旧 403（虚拟 viewer 只读）
        assert (
            client.patch(
                f"/api/v1/assets/{asset_id}", headers=alice, json={"name": "x.pdf"}
            ).status_code
            == 403
        )
