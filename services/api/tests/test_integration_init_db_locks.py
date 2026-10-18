"""init_db 稳态运行不拿表级锁的集成测试（testcontainers PG）。

线上事故：补列的 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 即便列已存在也要 ACCESS EXCLUSIVE 锁，
遇到长事务便一直排队并挡住该表的所有后续查询。现要求：结构已就绪时跳过 DDL；缺列时补上；
真要拿锁而拿不到时在 lock_timeout 内报错而不是无限等待。
"""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


@pytest.fixture()
async def engine(pg_url):
    from sqlalchemy.ext.asyncio import create_async_engine

    from loomvec.api.init_db import init_database
    from loomvec.core.constants import GRAPH_NAME

    # 同 compose 的 01-age-init.sql 预建 AGE 图（独立连接），避免池内连接 search_path 被改走
    bootstrap = create_async_engine(pg_url)
    async with bootstrap.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS age")
        await conn.exec_driver_sql("LOAD 'age'")
        await conn.exec_driver_sql('SET search_path = ag_catalog, "$user", public')
        await conn.exec_driver_sql(f"SELECT create_graph('{GRAPH_NAME}')")
    await bootstrap.dispose()
    engine = create_async_engine(pg_url)
    await init_database(engine)
    yield engine
    await engine.dispose()


async def test_ready_schema_skips_ddl_while_table_is_locked(engine, pg_url):
    """另一会话持有 space/tag/agent_session 的行级读事务，已就绪的库再跑 init_db 不应被阻塞。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from loomvec.api.init_db import init_database

    holder_engine = create_async_engine(pg_url)
    async with holder_engine.connect() as holder:
        for table in ("space", "tag", "agent_session", "agent_environment", "asset", "api_key"):
            await holder.exec_driver_sql(f"SELECT count(*) FROM {table}")  # 事务内持 ACCESS SHARE
        summary = await asyncio.wait_for(init_database(engine), timeout=8)
    await holder_engine.dispose()
    assert summary["tables"] > 0


async def test_missing_column_is_added(engine):
    from loomvec.api.init_db import init_database

    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE space DROP COLUMN chunk_strategy")
        await conn.exec_driver_sql("DROP INDEX ix_tag_parent_id")
    await init_database(engine)
    async with engine.connect() as conn:
        col = await conn.scalar(
            sa.text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='space' AND column_name='chunk_strategy'"
            )
        )
        idx = await conn.scalar(
            sa.text("SELECT count(*) FROM pg_indexes WHERE indexname='ix_tag_parent_id'")
        )
    assert col == 1 and idx == 1


async def test_lock_conflict_fails_fast(engine, pg_url, monkeypatch):
    """缺列又被长事务占着表：在 lock_timeout 内报错，而不是无限等待。"""
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    from loomvec.api import init_db

    monkeypatch.setattr(init_db, "_DDL_LOCK_TIMEOUT", "1s")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE space DROP COLUMN chunk_strategy")
    holder_engine = create_async_engine(pg_url)
    async with holder_engine.connect() as holder:
        await holder.exec_driver_sql("SELECT count(*) FROM space")
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(init_db.init_database(engine), timeout=15)
    await holder_engine.dispose()


async def test_tag_constraint_migrates_and_is_idempotent(engine):
    """存量库只有租户级旧约束：init_db 换成空间级新约束，重复执行无副作用。"""
    from loomvec.api.init_db import init_database

    async def names() -> set[str]:
        async with engine.connect() as conn:
            return set(
                await conn.scalars(
                    sa.text("SELECT conname FROM pg_constraint WHERE conrelid = 'tag'::regclass")
                )
            )

    async with engine.begin() as conn:
        await conn.exec_driver_sql("ALTER TABLE tag DROP CONSTRAINT uq_tag_space_name")
        await conn.exec_driver_sql(
            "ALTER TABLE tag ADD CONSTRAINT uq_tag_tenant_name UNIQUE (tenant_id, name)"
        )
    for _ in range(2):
        await init_database(engine)
        have = await names()
        assert "uq_tag_space_name" in have and "uq_tag_tenant_name" not in have


async def test_quoted_table_name_column_is_skipped_when_present(engine, pg_url):
    """补列表里的 "user" 带引号：列已在时不应再执行 ALTER（否则每次都白拿 user 表的锁）。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    from loomvec.api.init_db import init_database

    holder_engine = create_async_engine(pg_url)
    async with holder_engine.connect() as holder:
        await holder.exec_driver_sql('SELECT count(*) FROM "user"')  # 事务内持 ACCESS SHARE
        await asyncio.wait_for(init_database(engine), timeout=8)
    await holder_engine.dispose()
