"""数据库初始化（P5 起取代 alembic：极早期开发阶段固化最新结构，无迁移链）。

幂等可重复执行：
1. 扩展：pgcrypto（uuid 函数兜底，PG18 原生 uuidv7）；
2. `Base.metadata.create_all` 直接创建最新结构（PG 枚举类型由 SQLAlchemy 一并建）；
3. AGE 图（compose 镜像内置；普通 PG 无则 best-effort 跳过，图运行时降级）；
4. 种子数据（原迁移 0002/0003/0004 的 seed，ON CONFLICT 幂等）：
   默认空间、演示租户与用户（alice/bob/carol）、平台角色。

用法：`uv run python -m loomvec.api.init_db`（Makefile init-db / dev.sh / k8s 钩子）。
"""

from __future__ import annotations

import asyncio
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from loomvec.core.config import get_settings
from loomvec.core.constants import GRAPH_NAME
from loomvec.core.db import models  # noqa: F401 — 注册全部表到 Base.metadata
from loomvec.core.db.base import Base, create_engine_and_sessionmaker
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.api.init_db")

# 种子常量（原迁移 0002/0003/0004；固定 id 保证幂等与既有环境一致）
DEFAULT_SPACE_ID = "0198bec0-0000-7000-8000-000000000001"
SEED_TENANT_ID = "0198bec0-0000-7000-8000-0000000000a0"
SEED_USERS = {
    "alice": "0198bec0-0000-7000-8000-000000000101",
    "bob": "0198bec0-0000-7000-8000-000000000102",
    "carol": "0198bec0-0000-7000-8000-000000000103",
}
PLATFORM_ROLE_IDS = {
    "super_admin": "0198bec0-0000-7000-8000-000000000201",
    "operator": "0198bec0-0000-7000-8000-000000000202",
    "auditor": "0198bec0-0000-7000-8000-000000000203",
}


async def _ensure_extensions(engine: AsyncEngine) -> None:
    """pgcrypto 必需（uuid 函数兜底；PG18 已原生内置 uuidv7）。"""
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')


# 既有库的增量补列（create_all 只建新表不改旧表；幂等，PG ≥ 9.6 支持 IF NOT EXISTS）
_ADDITIVE_COLUMNS: list[tuple[str, str]] = [
    # P5.5a：会话绑定项目目录（docs/Research/01 §6.1）
    ("agent_session", "ADD COLUMN IF NOT EXISTS project_path VARCHAR(512) NOT NULL DEFAULT ''"),
    # API Key 绑定用户（PAT）：非空即按用户语义鉴权。新库由 create_all 建全
    # （含 FK/索引）；既有库补列 + 索引，FK 省略（列可空，历史行全空）
    ("api_key", "ADD COLUMN IF NOT EXISTS user_id UUID"),
]

# 既有库的增量补索引（幂等；须在对应补列之后执行）
_ADDITIVE_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_api_key_user_id ON api_key (user_id)",
]


async def _ensure_additive_columns(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table, ddl in _ADDITIVE_COLUMNS:
            await conn.exec_driver_sql(f"ALTER TABLE {table} {ddl}")
        for ddl in _ADDITIVE_INDEXES:
            await conn.exec_driver_sql(ddl)


async def _ensure_age_graph(engine: AsyncEngine) -> bool:
    """确保 AGE 扩展与 loomvec_graph 图存在；不可用时返回 False（运行时降级）。"""
    async with engine.begin() as conn:
        installed = await conn.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'age')")
        )
        if not installed:
            # SAVEPOINT 包裹：失败语句会把事务置 aborted，须回滚到保存点才能继续
            try:
                async with conn.begin_nested():
                    await conn.exec_driver_sql("CREATE EXTENSION age")
            except Exception:
                logger.warning("init_db_age_unavailable", note="AGE 扩展不可用，图谱运行时降级")
                return False
        graph_exists = await conn.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = :name)"),
            {"name": GRAPH_NAME},
        )
        if not graph_exists:
            await conn.exec_driver_sql("LOAD 'age'")
            await conn.exec_driver_sql('SET search_path = ag_catalog, "$user", public')
            await conn.exec_driver_sql(f"SELECT create_graph('{GRAPH_NAME}')")
        return True


async def _seed(engine: AsyncEngine) -> None:
    """种子数据（全部 ON CONFLICT / NOT EXISTS 幂等）。"""
    async with engine.begin() as conn:
        # ---- 默认空间（P1 单空间运行；P3 起归属种子租户）----
        await conn.execute(
            sa.text(
                "INSERT INTO space (id, slug, name, description) "
                "VALUES (:id, 'default', '默认空间', 'P1 摄取与检索闭环使用的种子默认空间') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": uuid.UUID(DEFAULT_SPACE_ID)},
        )
        # ---- 演示租户 + 演示用户 ----
        await conn.execute(
            sa.text(
                "INSERT INTO tenant (id, name, plan) "
                "VALUES (:id, 'LoomVec 演示租户', 'free') ON CONFLICT (id) DO NOTHING"
            ),
            {"id": uuid.UUID(SEED_TENANT_ID)},
        )
        for username, uid in SEED_USERS.items():
            await conn.execute(
                sa.text(
                    'INSERT INTO "user" (id, tenant_id, username, display_name, auth_source) '
                    "VALUES (:id, :tenant_id, :username, :display_name, 'local') "
                    "ON CONFLICT (username) DO NOTHING"
                ),
                {
                    "id": uuid.UUID(uid),
                    "tenant_id": uuid.UUID(SEED_TENANT_ID),
                    "username": username,
                    "display_name": username.capitalize(),
                },
            )
        # ---- 默认空间迁入租户模型：归属种子租户，alice 持有 ----
        await conn.execute(
            sa.text(
                "UPDATE space SET tenant_id = :tenant_id, owner_id = :alice, space_type = 'shared' "
                "WHERE id = :space_id"
            ),
            {
                "tenant_id": uuid.UUID(SEED_TENANT_ID),
                "alice": uuid.UUID(SEED_USERS["alice"]),
                "space_id": uuid.UUID(DEFAULT_SPACE_ID),
            },
        )
        for username, role in (("alice", "owner"), ("bob", "editor"), ("carol", "viewer")):
            await conn.execute(
                sa.text(
                    "INSERT INTO space_member (tenant_id, space_id, user_id, role) "
                    'SELECT :tenant_id, :space_id, u.id, CAST(:role AS space_role) FROM "user" u '
                    "WHERE u.username = :username "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM space_member m "
                    "  WHERE m.space_id = :space_id AND m.user_id = u.id)"
                ),
                {
                    "tenant_id": uuid.UUID(SEED_TENANT_ID),
                    "space_id": uuid.UUID(DEFAULT_SPACE_ID),
                    "username": username,
                    "role": role,
                },
            )
        # ---- 平台角色（全局行）----
        role_labels = {
            "super_admin": ("超级管理员", "全部权限，含系统配置与平台角色分配"),
            "operator": ("运营人员", "日常运营（租户/空间/审核/管线），无系统配置"),
            "auditor": ("审计员", "全局只读 + 审计导出"),
        }
        for code, role_id in PLATFORM_ROLE_IDS.items():
            name, description = role_labels[code]
            await conn.execute(
                sa.text(
                    "INSERT INTO role (id, code, name, description, is_platform_role) "
                    "VALUES (:id, :code, :name, :description, true) "
                    "ON CONFLICT (code) DO NOTHING"
                ),
                {"id": uuid.UUID(role_id), "code": code, "name": name, "description": description},
            )


async def init_database(engine: AsyncEngine) -> dict:
    """初始化到最新结构（幂等）；返回摘要（表数 / AGE 就绪状态）。"""
    await _ensure_extensions(engine)
    age_ready = await _ensure_age_graph(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_additive_columns(engine)
    await _seed(engine)
    return {"tables": len(Base.metadata.tables), "age_ready": age_ready}


async def main() -> None:
    settings = get_settings()
    engine, _ = create_engine_and_sessionmaker(settings.postgres)
    try:
        summary = await init_database(engine)
        logger.info("init_db_done", **summary)
        state = "就绪" if summary["age_ready"] else "不可用（图谱运行时降级）"
        print(f"数据库初始化完成：{summary['tables']} 张表；AGE {state}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
