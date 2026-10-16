"""API 测试共享设施。

- 集成测试统一跳过逻辑：凡带 `integration` 标记的用例，在无 Docker 或
  LOOMVEC_SKIP_INTEGRATION=1 时自动跳过（此处统一声明，测试文件不再重复）；
- 线上库硬防护（pytest_configure）：测试将使用的 Postgres 指向线上库则在建立任何库连接
  之前 pytest.exit，见 `_live_db_guard`；
- `pg_url` fixture：testcontainers PG18+AGE（本地 compose 构建的镜像，
  离线环境禁用 ryuk 侧车）；`pg_url_module` 为模块级共享版（含建表，同模块用例共用一个容器）。
"""

from __future__ import annotations

import os

import pytest
from _live_db_guard import effective_postgres_url, exit_message, live_db_violation


def pytest_configure(config):
    """最早的检查点：任何用例/模块导入建立库连接之前，拒绝指向线上库的测试会话。"""
    reason = live_db_violation(effective_postgres_url())
    if reason is not None:
        pytest.exit(exit_message(reason), returncode=3)


def _docker_available() -> bool:
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info", "--format", "ok"], capture_output=True, text=True, timeout=15
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LOOMVEC_SKIP_INTEGRATION") == "1" or _docker_available():
        return
    skip = pytest.mark.skip(reason="需要 Docker（LOOMVEC_SKIP_INTEGRATION=1 或 docker 不可用）")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def _asyncpg_url(pg) -> str:
    # testcontainers 默认驱动为 psycopg2；转 asyncpg DSN
    return pg.get_connection_url().replace("psycopg2", "asyncpg").replace("+psycopg2", "+asyncpg")


@pytest.fixture()
async def pg_url(monkeypatch):
    from testcontainers.postgres import PostgresContainer

    # ryuk 侧车需要拉取 testcontainers/ryuk 镜像，离线环境禁用；
    # 使用本地 compose 构建的 postgres-age:18（CI 具备外网时可改 postgres:18）
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
    with PostgresContainer(
        "loomvec/postgres-age:18", username="loomvec", password="loomvec", dbname="loomvec"
    ) as pg:
        yield _asyncpg_url(pg)


@pytest.fixture(scope="module")
def pg_url_module():
    """模块级临时库（已建全部表 + AGE 图 + 种子数据）：同模块用例共用一个容器，省去逐用例启动。

    同步 fixture，建库在独立事件循环内完成并释放连接；用例自己的 app/TestClient 另建引擎。
    """
    import asyncio

    pytest.importorskip("testcontainers", reason="testcontainers 未安装")
    if not _docker_available():
        pytest.skip("需要 Docker（临时 PG）")

    from sqlalchemy.ext.asyncio import create_async_engine
    from testcontainers.postgres import PostgresContainer

    from loomvec.api.init_db import init_database
    from loomvec.core.constants import GRAPH_NAME

    async def _init(url: str) -> None:
        # 同 deploy/compose/postgres-age/init/01-age-init.sql：先建 AGE 图，
        # init_db 走与 compose 一致的路径
        bootstrap = create_async_engine(url)
        try:
            async with bootstrap.begin() as conn:
                await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS age")
                await conn.exec_driver_sql("LOAD 'age'")
                await conn.exec_driver_sql('SET search_path = ag_catalog, "$user", public')
                await conn.exec_driver_sql(f"SELECT create_graph('{GRAPH_NAME}')")
        finally:
            await bootstrap.dispose()
        engine = create_async_engine(url)
        try:
            await init_database(engine)
        finally:
            await engine.dispose()

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
        with PostgresContainer(
            "loomvec/postgres-age:18", username="loomvec", password="loomvec", dbname="loomvec"
        ) as pg:
            url = _asyncpg_url(pg)
            asyncio.run(_init(url))
            yield url
