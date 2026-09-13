"""API 测试共享设施。

- 集成测试统一跳过逻辑：凡带 `integration` 标记的用例，在无 Docker 或
  LOOMVEC_SKIP_INTEGRATION=1 时自动跳过（此处统一声明，测试文件不再重复）；
- `pg_url` fixture：testcontainers PG18+AGE（本地 compose 构建的镜像，
  离线环境禁用 ryuk 侧车）。
"""

from __future__ import annotations

import os

import pytest


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


@pytest.fixture()
async def pg_url(monkeypatch):
    from testcontainers.postgres import PostgresContainer

    # ryuk 侧车需要拉取 testcontainers/ryuk 镜像，离线环境禁用；
    # 使用本地 compose 构建的 postgres-age:18（CI 具备外网时可改 postgres:18）
    monkeypatch.setenv("TESTCONTAINERS_RYUK_DISABLED", "true")
    with PostgresContainer(
        "loomvec/postgres-age:18", username="loomvec", password="loomvec", dbname="loomvec"
    ) as pg:
        # testcontainers 默认驱动为 psycopg2；转 asyncpg DSN
        url = (
            pg.get_connection_url().replace("psycopg2", "asyncpg").replace("+psycopg2", "+asyncpg")
        )
        yield url
