"""run_space_communities 的集成测试（testcontainers PG18+AGE）。

回归：community.updated_at 曾被写入带时区的 datetime.now(UTC)，而基类 TimestampMixin 的列是不带
时区的 timestamp → asyncpg 报「can't subtract offset-naive and offset-aware datetimes」，
社区任务（L4 全局层）因此长期写不成。
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = pytest.mark.integration


async def test_run_space_communities_persists_communities(pg_url):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.config import Settings
    from loomvec.core.db.base import Base
    from loomvec.core.db.models import Community, Entity, Tenant
    from loomvec.core.graph.age import AgeStore, GraphEdge, GraphNode
    from loomvec.core.graph.communities import run_space_communities

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    age = AgeStore()
    space_id = uuid.uuid4()
    keys = [f"k{i}" for i in range(6)]
    try:
        async with factory() as session, session.begin():
            tenant = Tenant(name="community-test")
            session.add(tenant)
            await session.flush()
            tenant_id = tenant.id
            await age.ensure_graph(session)
            for k in keys:
                session.add(
                    Entity(
                        tenant_id=tenant_id,
                        space_id=space_id,
                        entity_key=k,
                        name=k,
                        name_norm=k,
                        type="concept",
                    )
                )
            await age.upsert_nodes(
                session,
                [
                    GraphNode(key=k, space_id=str(space_id), tenant_id=None, name=k, type="concept")
                    for k in keys
                ],
            )
            # 两个三角形：k0-k1-k2、k3-k4-k5
            tri = [
                ("k0", "k1"),
                ("k1", "k2"),
                ("k0", "k2"),
                ("k3", "k4"),
                ("k4", "k5"),
                ("k3", "k5"),
            ]
            await age.write_edges(
                session, [GraphEdge(h, t, "USES", chunk_ids=["c"]) for h, t in tri]
            )

        async with factory() as session, session.begin():
            result = await run_space_communities(
                session, Settings(_env_file=None), space_id, tenant_id, summarize=None
            )
        assert result.get("communities", 0) >= 2, result

        async with factory() as session:
            rows = (
                (await session.execute(select(Community).where(Community.space_id == space_id)))
                .scalars()
                .all()
            )
            assert len(rows) >= 2
            assert all(c.updated_at is not None for c in rows)
            linked = (
                (await session.execute(select(Entity).where(Entity.space_id == space_id)))
                .scalars()
                .all()
            )
            assert all(e.community_id is not None for e in linked)
    finally:
        await engine.dispose()
