"""Finder 文件夹域 DB 级集成测试（testcontainers PG；无 Docker 自动跳过）。

覆盖：迁移 0009 后的目录树不变量——同父重名校验、移动防环、级联删除
（资产软删 + 配额扣减）、资产复制（共享原始对象 + 计量）、文件夹递归复制
（同名并入）、列表 folder 过滤（root 哨兵 / 精确文件夹）。
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


class _MilvusStub:
    """delete_folder_cascade 的向量清理桩（不依赖真实 Milvus）。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def async_delete_asset_units(self, asset_id) -> None:
        self.deleted.append(str(asset_id))


@pytest.fixture()
async def db(pg_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.db.base import Base
    from loomvec.core.db.models import Space, SpaceMember, SpaceRole, Tenant, User

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[
                    t
                    for t in Base.metadata.sorted_tables
                    if t.key
                    in {
                        "tenant",
                        "user",
                        "space",
                        "space_member",
                        "asset",
                        "asset_folder",
                        "asset_version",
                        "asset_rendition",
                        "semantic_unit",
                        "space_usage",
                        "tag",
                        "asset_tag",
                        "category",
                    }
                ],
            )
        )

    maker = async_sessionmaker(engine, expire_on_commit=False)
    ids: dict[str, uuid.UUID] = {}
    async with maker() as session:
        tenant = Tenant(name="t")
        session.add(tenant)
        await session.flush()
        ids["tenant"] = tenant.id
        u = User(tenant_id=tenant.id, username="alice", display_name="alice")
        session.add(u)
        await session.flush()
        ids["alice"] = u.id
        space = Space(tenant_id=tenant.id, slug="demo", name="Demo", owner_id=u.id)
        session.add(space)
        await session.flush()
        ids["space"] = space.id
        session.add(
            SpaceMember(space_id=space.id, user_id=u.id, role=SpaceRole.OWNER, tenant_id=tenant.id)
        )
        await session.commit()
    yield maker, ids
    await engine.dispose()


def _identity(user_id: uuid.UUID, tenant_id: uuid.UUID):
    from loomvec.api.context import Identity

    return Identity(
        user_id=str(user_id),
        username="alice",
        tenant_id=str(tenant_id),
        scopes=("read", "write"),
        roles=(),
    )


async def _add_asset(session, ids, *, name="a.pdf", size=100, folder_id=None) -> uuid.UUID:
    from datetime import UTC, datetime

    from loomvec.core.db.models import Asset, AssetStatus, AssetVersion

    asset = Asset(
        tenant_id=ids["tenant"],
        space_id=ids["space"],
        folder_id=folder_id,
        name=name,
        mime_type="application/pdf",
        ext=".pdf",
        size_bytes=size,
        storage_key=f"raw/{name}",
        status=AssetStatus.READY,
        created_by=ids["alice"],
    )
    session.add(asset)
    await session.flush()
    session.add(
        AssetVersion(
            asset_id=asset.id,
            tenant_id=ids["tenant"],
            version=1,
            storage_key=asset.storage_key,
            mime_type=asset.mime_type,
            size_bytes=size,
        )
    )
    # 建表时间列由 PG 默认值填充；显式回读避免 None
    asset.created_at = asset.created_at or datetime.now(UTC)
    await session.commit()
    return asset.id


async def test_folder_tree_invariants(db):
    """同父重名拒绝、跨父同名允许、移动防环。"""
    from loomvec.api.services import folders as svc
    from loomvec.core.db.models import Space
    from loomvec.core.errors import ValidationError

    maker, ids = db
    async with maker() as session:
        space = await session.get(Space, ids["space"])
        ident = _identity(ids["alice"], ids["tenant"])
        root_a = await svc.create_folder(
            session, identity=ident, space=space, name="A", parent_id=None
        )
        child = await svc.create_folder(
            session, identity=ident, space=space, name="B", parent_id=root_a.id
        )
        with pytest.raises(ValidationError):
            await svc.create_folder(session, identity=ident, space=space, name="A", parent_id=None)
        # 跨父同名允许
        other = await svc.create_folder(
            session, identity=ident, space=space, name="C", parent_id=None
        )
        await svc.create_folder(session, identity=ident, space=space, name="A", parent_id=other.id)
        # 防环：A 移到自己的后代 B/C 内拒绝
        with pytest.raises(ValidationError):
            await svc.move_folder(session, root_a, child.id)
        # 正常移动：B 移到根
        moved = await svc.move_folder(session, child, None)
        assert moved.parent_id is None


async def test_delete_folder_cascade_soft_deletes_assets_and_quota(db):
    """删除文件夹：后代文件夹 + 资产级联软删，配额按资产扣减。"""
    from loomvec.api.services import folders as svc
    from loomvec.core.db.models import Asset, Space
    from loomvec.core.db.repos import AssetFolderRepo, AssetRepo
    from loomvec.core.quota import apply_asset_added

    maker, ids = db
    async with maker() as session:
        space = await session.get(Space, ids["space"])
        ident = _identity(ids["alice"], ids["tenant"])
        parent = await svc.create_folder(
            session, identity=ident, space=space, name="P", parent_id=None
        )
        child = await svc.create_folder(
            session, identity=ident, space=space, name="Q", parent_id=parent.id
        )
        a1 = await _add_asset(session, ids, name="1.pdf", folder_id=parent.id)
        a2 = await _add_asset(session, ids, name="2.pdf", folder_id=child.id)
        a3 = await _add_asset(session, ids, name="3.pdf", folder_id=None)
        await apply_asset_added(
            session, space_id=ids["space"], tenant_id=ids["tenant"], size_bytes=300
        )

        milvus = _MilvusStub()
        deleted = await svc.delete_folder_cascade(session, milvus=milvus, folder=parent)
        assert deleted == 2
        assert {str(a1), str(a2)} <= set(milvus.deleted)
        assert (await session.get(Asset, a1)).deleted_at is not None
        assert (await session.get(Asset, a2)).deleted_at is not None
        assert (await session.get(Asset, a3)).deleted_at is None
        assert await AssetFolderRepo(session).get_live(parent.id) is None
        assert await AssetFolderRepo(session).get_live(child.id) is None
        assert await AssetRepo(session).list_in_folders([parent.id, child.id]) == []


async def test_copy_asset_shares_object_and_counts_quota(db):
    """复制资产：新行共享 storage_key、状态 pending、配额 +1 文件 / +size 字节。"""
    from sqlalchemy import select

    from loomvec.api.services import folders as svc
    from loomvec.core.db.models import Space, SpaceUsage
    from loomvec.core.db.repos import AssetRepo
    from loomvec.core.quota import apply_asset_added

    maker, ids = db
    async with maker() as session:
        space = await session.get(Space, ids["space"])
        ident = _identity(ids["alice"], ids["tenant"])
        src_id = await _add_asset(session, ids, name="doc.pdf", size=500)
        # 源资产按登记路径计量（500B / 1 文件）
        await apply_asset_added(
            session, space_id=ids["space"], tenant_id=ids["tenant"], size_bytes=500
        )
        src = await AssetRepo(session).get_live(src_id)

        copy = await svc.copy_asset(
            session, identity=ident, source=src, target_space=space, target_folder_id=None
        )
        assert copy.id != src.id
        assert copy.storage_key == src.storage_key
        assert copy.status.value == "pending"
        usage = (
            await session.execute(select(SpaceUsage).where(SpaceUsage.space_id == space.id))
        ).scalar_one()
        assert usage.file_count == 2
        assert usage.storage_bytes == 1000


async def test_copy_folder_recursive_merges_same_name(db):
    """递归复制：目标父下同名文件夹并入；同级复制建「副本」避让源自身。"""
    from loomvec.api.services import folders as svc
    from loomvec.core.db.models import Space
    from loomvec.core.db.repos import AssetFolderRepo, AssetRepo

    maker, ids = db
    async with maker() as session:
        space = await session.get(Space, ids["space"])
        ident = _identity(ids["alice"], ids["tenant"])
        src = await svc.create_folder(
            session, identity=ident, space=space, name="Docs", parent_id=None
        )
        sub = await svc.create_folder(
            session, identity=ident, space=space, name="2026", parent_id=src.id
        )
        await _add_asset(session, ids, name="x.pdf", folder_id=src.id)
        await _add_asset(session, ids, name="y.pdf", folder_id=sub.id)
        other = await svc.create_folder(
            session, identity=ident, space=space, name="Other", parent_id=None
        )

        enqueued: list[str] = []

        # 复制到 Other 下：新建 Docs
        dest = await svc.copy_folder_recursive(
            session, identity=ident, source=src, target_parent_id=other.id, enqueue=enqueued.append
        )
        assert dest.id != src.id and dest.name == "Docs" and dest.parent_id == other.id
        assert len(enqueued) == 2
        repo = AssetFolderRepo(session)
        dest_sub = await repo.find_sibling_name(ids["space"], dest.id, "2026")
        assert dest_sub is not None

        # 再次复制到 Other 下：并入既有 Docs（同 id，资产累加）
        dest2 = await svc.copy_folder_recursive(
            session, identity=ident, source=src, target_parent_id=other.id, enqueue=enqueued.append
        )
        assert dest2.id == dest.id
        assert len(await AssetRepo(session).list_in_folders([dest.id])) == 2

        # 同级复制（根 → 根）：不能并入源自身，建「Docs 副本」
        dest3 = await svc.copy_folder_recursive(
            session, identity=ident, source=src, target_parent_id=None, enqueue=enqueued.append
        )
        assert dest3.id != src.id
        assert dest3.name == "Docs 副本"
        assert dest3.parent_id is None


async def test_asset_move_and_list_folder_filter(db):
    """资产移动（挂靠/移回根）+ 列表 folder 过滤（root 哨兵 / 精确 id / 缺省不过滤）。"""
    from loomvec.api.services import folders as svc
    from loomvec.core.db.models import Space
    from loomvec.core.db.repos import AssetRepo
    from loomvec.core.errors import ValidationError

    maker, ids = db
    async with maker() as session:
        space = await session.get(Space, ids["space"])
        ident = _identity(ids["alice"], ids["tenant"])
        folder = await svc.create_folder(
            session, identity=ident, space=space, name="F", parent_id=None
        )
        a1 = await _add_asset(session, ids, name="in.pdf")
        a2 = await _add_asset(session, ids, name="out.pdf")

        repo = AssetRepo(session)
        asset = await repo.get_live(a1)
        asset.folder_id = await svc.validate_folder_in_space(session, ids["space"], folder.id)
        await session.commit()

        root_items = await repo.list_by_space(ids["space"], limit=10, folder_id="root")
        assert [a.id for a in root_items] == [a2]
        in_items = await repo.list_by_space(ids["space"], limit=10, folder_id=str(folder.id))
        assert [a.id for a in in_items] == [a1]
        all_items = await repo.list_by_space(ids["space"], limit=10)
        assert len(all_items) == 2

        # 跨空间文件夹引用拒绝
        with pytest.raises(ValidationError):
            await svc.validate_folder_in_space(session, uuid.uuid4(), folder.id)
