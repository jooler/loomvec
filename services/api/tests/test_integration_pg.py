"""testcontainers 集成测试（需要本机 Docker；无 Docker 环境自动跳过，见 conftest）。

运行：uv run pytest -m integration
覆盖：PG18 建表迁移 + uuidv7 + 仓储基类（P0-QA-01 的 testcontainers 模式确立）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


async def test_base_tables_and_repository(pg_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.db.base import Base
    from loomvec.core.db.models import Tenant, User
    from loomvec.core.db.repository import Repository

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():

        class _UserRepo(Repository[User]):
            model = User

        repo = _UserRepo(session)
        tenant = Tenant(name="smoke-tenant")
        session.add(tenant)
        await session.flush()
        user = await repo.create(username="alice", tenant_id=tenant.id)
        assert user.id is not None
        # PG18 uuidv7：时间有序（版本位 = 7）
        assert user.id.version == 7
        fetched = await repo.get_or_404(user.id)
        assert fetched.username == "alice"
        await repo.soft_delete(fetched)
        assert await repo.get(user.id) is None  # 软删除后默认查询不可见
    await engine.dispose()
