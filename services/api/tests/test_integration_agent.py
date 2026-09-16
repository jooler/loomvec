"""P5 集成测试：agent_environment / agent_session 建表与仓储语义（迁移 0011）。

覆盖：uuidv7 主键、默认 env 的 get_or_create、会话列表归档过滤、
交接重绑语义（owner 重绑 + prev_owner/transferred_at 审计、audit 字段不变）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]


async def test_agent_env_and_session_domain(pg_url):
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.db.base import Base
    from loomvec.core.db.models import AgentEnvironmentStatus as EnvStatus
    from loomvec.core.db.models import Tenant, User
    from loomvec.core.db.repos import AgentEnvironmentRepo, AgentSessionRepo

    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        tenant = Tenant(name="agent-tenant")
        session.add(tenant)
        await session.flush()
        user = User(username="alice", tenant_id=tenant.id)
        session.add(user)
        await session.flush()

        env_repo = AgentEnvironmentRepo(session)
        env = await env_repo.get_or_create_default(user.id, tenant.id, title="张三的工作环境")
        assert env.id.version == 7  # PG18 uuidv7 主键
        assert env.status == EnvStatus.ACTIVE
        # 二次调用复用同一默认 env（不重复创建）
        again = await env_repo.get_or_create_default(user.id, tenant.id, title="x")
        assert again.id == env.id

        sess_repo = AgentSessionRepo(session)
        s1 = await sess_repo.create(
            id=uuid.uuid4(),  # 网关生成 uuid 后传入（dsh session_id）
            env_id=env.id,
            created_by_user_id=user.id,
            title="新会话",
            scope_space_ids=[str(uuid.uuid4())],
        )
        await sess_repo.create(
            id=uuid.uuid4(),
            env_id=env.id,
            created_by_user_id=user.id,
            title="已归档",
            last_message_at=datetime.now(UTC),
            message_count=2,
            preview="摘要",
            archived_at=datetime.now(UTC),
        )
        live = await sess_repo.list_for_env(env.id)
        assert [s.id for s in live] == [s1.id]  # 归档默认不可见
        all_sessions = await sess_repo.list_for_env(env.id, include_archived=True)
        assert len(all_sessions) == 2
        assert await sess_repo.count_for_env(env.id) == 1
        assert await sess_repo.get_in_env(s1.id, env.id) is not None
        assert await sess_repo.get_in_env(s1.id, uuid.uuid4()) is None  # 跨 env 不可见

        # 交接：owner 重绑，审计字段不动（14 文档 §4.2）
        bob = User(username="bob", tenant_id=tenant.id)
        session.add(bob)
        await session.flush()
        await env_repo.update(
            env,
            owner_user_id=bob.id,
            prev_owner_user_id=user.id,
            transferred_at=datetime.now(UTC),
            status=EnvStatus.TRANSFERRED,
        )
        s1_after = await sess_repo.get_in_env(s1.id, env.id)
        assert s1_after.created_by_user_id == user.id  # 创建者审计不变量
        # 交接后状态为 transferred：不再是任何人的「活跃默认 env」
        assert await env_repo.get_default_for_user(bob.id) is None
        assert await env_repo.get_default_for_user(user.id) is None
        bob_envs = await env_repo.list_for_user(bob.id)
        assert [e.id for e in bob_envs] == [env.id]

    await engine.dispose()
