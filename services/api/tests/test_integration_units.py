"""chunk 管理 DB 级集成测试（testcontainers PG；无 Docker 自动跳过，见 conftest）。

覆盖 services/units.py：列表过滤 / 手动新增（即时向量化 + 缓存合并 + Milvus 行）/
编辑重嵌 / 仅改关键词不重嵌 / 批删（行 + 缓存条目 + 向量清理）/ 图片资产拒绝增改。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


class FakeAI:
    """记录调用次数的确定性嵌入桩（返回固定维度递增向量）。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(self.calls)), 1.0, 0.0] for _ in texts]


class FakeStorage:
    """派生 bucket 内存桩：仅 vectors.json 读写所需接口。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def get_object(self, bucket: str, key: str) -> bytes:
        return self.objects[key]

    async def put_object(self, bucket: str, key: str, data: bytes, content_type=None) -> None:
        self.objects[key] = data


class FakeMilvus:
    text_dim = 3
    clip_dim = 3

    def __init__(self) -> None:
        self.upserted: list[dict] = []
        self.deleted_ids: list[str] = []

    async def async_upsert_units(self, rows: list[dict]) -> None:
        self.upserted.extend(rows)

    async def async_delete_unit_ids(self, ids: list) -> None:
        self.deleted_ids.extend(str(i) for i in ids)


@pytest.fixture()
async def db(pg_url):
    """建表（与迁移等价 schema）+ 种子：租户/用户/空间/文档资产/v1/两个单元。"""
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.db.base import Base
    from loomvec.core.db.models import (
        Asset,
        AssetStatus,
        AssetVersion,
        ChunkMethod,
        SemanticUnit,
        Space,
        SpaceMember,
        SpaceRole,
        Tenant,
        UnitType,
        User,
    )

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
                        "semantic_unit",
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
        for name in ("alice", "bob", "carol"):
            u = User(tenant_id=tenant.id, username=name, display_name=name)
            session.add(u)
            await session.flush()
            ids[name] = u.id
        space = Space(tenant_id=tenant.id, slug="demo", name="Demo", owner_id=ids["alice"])
        session.add(space)
        await session.flush()
        ids["space"] = space.id
        for name, role in (("alice", "owner"), ("bob", "editor"), ("carol", "viewer")):
            session.add(
                SpaceMember(
                    space_id=space.id, user_id=ids[name], role=SpaceRole(role), tenant_id=tenant.id
                )
            )
        asset = Asset(
            space_id=space.id,
            tenant_id=tenant.id,
            name="demo.md",
            mime_type="text/markdown",
            ext=".md",
            size_bytes=100,
            status=AssetStatus.READY,
            created_by=ids["alice"],
        )
        session.add(asset)
        await session.flush()
        ids["asset"] = asset.id
        version = AssetVersion(
            asset_id=asset.id,
            version=1,
            inline_text="第一段\n第二段",
            version_meta={"parse": {"parser": "plain_text", "page_count": 1}},
        )
        session.add(version)
        await session.flush()
        ids["version"] = version.id
        for order, content in enumerate(("alpha 内容", "beta 内容")):
            session.add(
                SemanticUnit(
                    asset_id=asset.id,
                    version_id=version.id,
                    space_id=space.id,
                    tenant_id=tenant.id,
                    unit_type=UnitType.TEXT,
                    content=content,
                    chunk_method=ChunkMethod.LLM_MARKERS,
                    order_index=order,
                    char_count=len(content),
                )
            )
        await session.commit()
    yield maker, ids
    await engine.dispose()


@pytest.fixture()
def fakes():
    from loomvec.core.config import Settings

    return {
        "ai": FakeAI(),
        "storage": FakeStorage(),
        "milvus": FakeMilvus(),
        "settings": Settings(_env_file=None, ai={"mock": True}),
    }


async def _cache_units(storage: FakeStorage, key: str) -> dict:
    raw = storage.objects.get(key)
    if raw is None:
        return {}
    cache = json.loads(raw.decode())
    return cache.get("units", cache)  # 媒体缓存为嵌套布局，文档为扁平映射


async def test_create_unit_sync_embed(db, fakes):
    """手动新增：manual 方法 + 追加序号 + 同步嵌入（缓存/索引同步更新）。"""
    from loomvec.api.services import units as unit_service
    from loomvec.core.db.repos import AssetRepo
    from loomvec.core.storage_keys import embed_vectors_key

    maker, ids = db
    async with maker() as session:
        asset = await AssetRepo(session).get(ids["asset"])
        unit = await unit_service.create_unit(
            session,
            ai=fakes["ai"],
            milvus=fakes["milvus"],
            storage=fakes["storage"],
            settings=fakes["settings"],
            asset=asset,
            content="人工补充的说明",
            title="补充",
            keywords=["kw"],
            unit_type="text",
        )
        assert unit.chunk_method.value == "manual"
        assert unit.order_index == 2  # 追加在两个既有单元之后
        assert unit.embed_model_version == "mock"
        assert len(fakes["ai"].calls) == 1

        key = embed_vectors_key(asset.id, 1)
        cache = await _cache_units(fakes["storage"], key)
        assert set(cache) == {str(unit.id)}  # 新单元写入缓存（初始无缓存条目）

        rows = {r["id"] for r in fakes["milvus"].upserted}
        assert str(unit.id) in rows
        new_row = next(r for r in fakes["milvus"].upserted if r["id"] == str(unit.id))
        assert new_row["text_dense"] == [1.0, 1.0, 0.0]
        assert new_row["unit_type"] == "text"


async def test_update_unit_reembeds_and_updates_index(db, fakes):
    """编辑内容：重新嵌入 + Milvus 行 text 更新；仅改关键词不触发嵌入。"""
    from loomvec.api.services import units as unit_service
    from loomvec.core.db.repos import AssetRepo, SemanticUnitRepo

    maker, ids = db
    async with maker() as session:
        asset = await AssetRepo(session).get(ids["asset"])
        first = (await SemanticUnitRepo(session).for_version(ids["version"]))[0]
        await unit_service.update_unit(
            session,
            ai=fakes["ai"],
            milvus=fakes["milvus"],
            storage=fakes["storage"],
            settings=fakes["settings"],
            asset=asset,
            unit=first,
            title=None,
            content="alpha 内容（已修订）",
            keywords=None,
        )
        assert len(fakes["ai"].calls) == 1
        row = next(r for r in fakes["milvus"].upserted if r["id"] == str(first.id))
        assert row["text"] == "alpha 内容（已修订）"

        # 仅改关键词：元数据变更，不重新向量化
        calls_before = len(fakes["ai"].calls)
        await unit_service.update_unit(
            session,
            ai=fakes["ai"],
            milvus=fakes["milvus"],
            storage=fakes["storage"],
            settings=fakes["settings"],
            asset=asset,
            unit=first,
            title=None,
            content=None,
            keywords=["只改关键词"],
        )
        assert len(fakes["ai"].calls) == calls_before


async def test_delete_units_cascades_cache_and_vectors(db, fakes):
    """批删：PG 行 + 缓存条目 + Milvus 按 id 清理；缺失 id 报 NotFound。"""
    import uuid

    from loomvec.api.services import units as unit_service
    from loomvec.core.db.repos import AssetRepo, SemanticUnitRepo
    from loomvec.core.errors import NotFoundError
    from loomvec.core.storage_keys import embed_vectors_key

    maker, ids = db
    async with maker() as session:
        asset = await AssetRepo(session).get(ids["asset"])
        units = await SemanticUnitRepo(session).for_version(ids["version"])
        removed = await unit_service.delete_units(
            session,
            milvus=fakes["milvus"],
            storage=fakes["storage"],
            settings=fakes["settings"],
            asset=asset,
            unit_ids=[u.id for u in units],
        )
        assert removed == 2
        assert {str(u.id) for u in units} == set(fakes["milvus"].deleted_ids)
        key = embed_vectors_key(asset.id, 1)
        # 无缓存时删除是空转（缓存缺失视为空），不落文件
        assert key not in fakes["storage"].objects
        assert (await SemanticUnitRepo(session).for_version(ids["version"])) == []

        with pytest.raises(NotFoundError):
            await unit_service.delete_units(
                session,
                milvus=fakes["milvus"],
                storage=fakes["storage"],
                settings=fakes["settings"],
                asset=asset,
                unit_ids=[uuid.uuid4()],
            )


async def test_image_asset_rejects_manual_units(db, fakes):
    """图片资产的单单元由图片流维护：手动增改拒绝。"""

    from loomvec.api.services import units as unit_service
    from loomvec.core.db.models import Asset, AssetStatus
    from loomvec.core.errors import ValidationError

    maker, ids = db
    async with maker() as session:
        image = Asset(
            space_id=ids["space"],
            tenant_id=ids["tenant"],
            name="pic.png",
            mime_type="image/png",
            ext=".png",
            size_bytes=10,
            status=AssetStatus.READY,
            created_by=ids["alice"],
        )
        session.add(image)
        await session.flush()
        with pytest.raises(ValidationError):
            await unit_service.create_unit(
                session,
                ai=fakes["ai"],
                milvus=fakes["milvus"],
                storage=fakes["storage"],
                settings=fakes["settings"],
                asset=image,
                content="x",
                title=None,
                keywords=None,
                unit_type="text",
            )
