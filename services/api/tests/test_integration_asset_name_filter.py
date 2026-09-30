"""P2 资产名称过滤（GET /assets?q= → AssetRepo.list_in_spaces(name_like=…)）集成测试。

覆盖：过滤生效（大小写不敏感包含）；不传 q 与旧行为一致；q 与 folder_id=root
可组合；关键字含 % _ \\ 时按字面匹配（不放大范围）。
testcontainers PG；无 Docker 自动跳过（见 conftest）。
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


@pytest.fixture()
async def db(pg_url):
    """建表 + 种子租户/用户/空间（owner）——只铺名称过滤所需的最小数据面。"""
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
                    if t.key in {"tenant", "user", "space", "space_member", "asset_folder", "asset", "category"}
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
        user = User(tenant_id=tenant.id, username="alice", display_name="alice")
        session.add(user)
        await session.flush()
        ids["alice"] = user.id
        space = Space(tenant_id=tenant.id, slug="demo", name="Demo", owner_id=user.id)
        session.add(space)
        await session.flush()
        ids["space"] = space.id
        session.add(
            SpaceMember(
                space_id=space.id,
                user_id=user.id,
                role=SpaceRole.OWNER,
                tenant_id=tenant.id,
            )
        )
        await session.commit()
    yield maker, ids
    await engine.dispose()


def _asset(name: str, *, folder_id=None, tenant_id=None, space_id=None):
    from loomvec.core.db.models import Asset, AssetStatus

    return Asset(
        name=name,
        ext=".md",
        mime_type="text/markdown",
        status=AssetStatus.READY,
        tenant_id=tenant_id,
        space_id=space_id,
        folder_id=folder_id,
        created_by=None,
    )


async def _seed_assets(session, ids) -> dict[str, uuid.UUID]:
    from loomvec.core.db.models import AssetFolder

    folder = AssetFolder(
        name="f1", space_id=ids["space"], tenant_id=ids["tenant"], created_by=ids["alice"]
    )
    session.add(folder)
    await session.flush()

    names = [
        "InkCop 手册",
        "inkcop-release-notes",
        "设计文档.md",
        "100% 覆盖率",
        "snake_case_file",
        "snakeXcase",
        "back\\slash",
        "无关文件",
    ]
    created: dict[str, uuid.UUID] = {}
    for i, name in enumerate(names):
        a = _asset(
            name,
            folder_id=folder.id if i == 0 else None,
            tenant_id=ids["tenant"],
            space_id=ids["space"],
        )
        session.add(a)
        await session.flush()
        created[name] = a.id
    await session.commit()
    return created


async def _names(rows) -> list[str]:
    return [r.name for r in rows]


async def test_name_like_filters_case_insensitive(db):
    maker, ids = db
    from loomvec.core.db.repos import AssetRepo

    async with maker() as session:
        await _seed_assets(session, ids)
        rows = await AssetRepo(session).list_in_spaces(
            [ids["space"]], limit=50, name_like="inkcop"
        )
        assert sorted(await _names(rows)) == ["InkCop 手册", "inkcop-release-notes"]


async def test_name_like_absent_keeps_legacy_behavior(db):
    maker, ids = db
    from loomvec.core.db.repos import AssetRepo

    async with maker() as session:
        await _seed_assets(session, ids)
        rows = await AssetRepo(session).list_in_spaces([ids["space"]], limit=50)
        assert len(rows) == 8  # 不传 q：与旧版一致返回全部
        rows_by_space = await AssetRepo(session).list_by_space(ids["space"], limit=50)
        assert len(rows_by_space) == 8  # list_by_space 透传同一参数面


async def test_name_like_combines_with_folder_root(db):
    maker, ids = db
    from loomvec.core.db.repos import AssetRepo

    async with maker() as session:
        await _seed_assets(session, ids)
        rows = await AssetRepo(session).list_in_spaces(
            [ids["space"]], limit=50, folder_id="root", name_like="设计"
        )
        assert await _names(rows) == ["设计文档.md"]
        # 目录内名称过滤：folder uuid 限定目录，q 再滤名称。
        # 「InkCop 手册」在 f1 内不在根；根里命中的是 inkcop-release-notes
        folder_rows = await AssetRepo(session).list_in_spaces(
            [ids["space"]], limit=50, folder_id="root", name_like="InkCop"
        )
        assert await _names(folder_rows) == ["inkcop-release-notes"]


async def test_name_like_escapes_wildcards(db):
    maker, ids = db
    from loomvec.core.db.repos import AssetRepo

    async with maker() as session:
        await _seed_assets(session, ids)
        # % 只字面匹配「100% 覆盖率」，不作为任意串通配
        rows = await AssetRepo(session).list_in_spaces([ids["space"]], limit=50, name_like="0%")
        assert await _names(rows) == ["100% 覆盖率"]
        # _ 只字面匹配：若未转义为单字符通配，"snake_case" 会同时命中 snakeXcase
        rows = await AssetRepo(session).list_in_spaces(
            [ids["space"]], limit=50, name_like="snake_case"
        )
        assert await _names(rows) == ["snake_case_file"]
        # 反斜杠按字面匹配
        rows = await AssetRepo(session).list_in_spaces(
            [ids["space"]], limit=50, name_like="back\\slash"
        )
        assert await _names(rows) == ["back\\slash"]
