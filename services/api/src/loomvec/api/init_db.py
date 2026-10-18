"""数据库初始化（P5 起取代 alembic：极早期开发阶段固化最新结构，无迁移链）。

幂等可重复执行：
1. 扩展：pgcrypto（uuid 函数兜底，PG18 原生 uuidv7）；
2. `Base.metadata.create_all` 直接创建最新结构（PG 枚举类型由 SQLAlchemy 一并建）；
   既有库的补列 / 补索引 / 约束调整先查目录，已就绪即跳过（ALTER TABLE 哪怕是空操作也要
   ACCESS EXCLUSIVE 锁，会被线上长事务卡住并连带挡住该表的所有查询），需 DDL 时加 lock_timeout；
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
    # P2 acp-gateway：env ↔ 网关 workspace、会话所用 agent / 网关 session / MCP token 过期时间
    # （存量会话都是 dsh 时代创建，agent 默认值 dsh）
    ("agent_environment", "ADD COLUMN IF NOT EXISTS gateway_workspace_id VARCHAR(64)"),
    ("agent_session", "ADD COLUMN IF NOT EXISTS agent VARCHAR(16) NOT NULL DEFAULT 'dsh'"),
    ("agent_session", "ADD COLUMN IF NOT EXISTS gateway_session_id VARCHAR(64)"),
    ("agent_session", "ADD COLUMN IF NOT EXISTS mcp_token_exp TIMESTAMPTZ"),
    ("agent_session", "ADD COLUMN IF NOT EXISTS replay_pending BOOLEAN NOT NULL DEFAULT FALSE"),
    # API Key 绑定用户（PAT）：非空即按用户语义鉴权。新库由 create_all 建全
    # （含 FK/索引）；既有库补列 + 索引，FK 省略（列可空，历史行全空）
    ("api_key", "ADD COLUMN IF NOT EXISTS user_id UUID"),
    # 分片策略 / 图谱抽取空间级覆盖（constants.CHUNK_STRATEGIES / GRAPH_EXTRACTION_MODES）
    ("space", "ADD COLUMN IF NOT EXISTS chunk_strategy VARCHAR(32)"),
    ("space", "ADD COLUMN IF NOT EXISTS graph_extraction VARCHAR(32)"),
    # 抽取领域配置空间级选择（graph/profiles；NULL = 跟随全局 pipeline.extraction_profile）
    ("space", "ADD COLUMN IF NOT EXISTS extraction_profile VARCHAR(64)"),
    # 层级知识库：Tag 仍兼容旧标签，只增加父节点关系
    ("tag", "ADD COLUMN IF NOT EXISTS parent_id UUID"),
    ("tag", "ADD COLUMN IF NOT EXISTS space_id UUID"),
    # 知识库节点状态（TagStatus）：存量行由 DEFAULT 补为 active；管线自动新建的为 pending
    ("tag", "ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'active'"),
    # 合并目标（新库由 create_all 建 FK；既有库只补列，列可空、历史行全空）
    ("tag", "ADD COLUMN IF NOT EXISTS merged_into UUID"),
    ("asset_tag", "ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'manual'"),
    ("asset_tag", "ADD COLUMN IF NOT EXISTS confidence FLOAT"),
    # 本地账号密码哈希（user 是 PG 保留字，须加引号）
    ('"user"', "ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)"),
]

# 既有库的增量补索引（幂等；须在对应补列之后执行）
_ADDITIVE_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_api_key_user_id ON api_key (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_tag_parent_id ON tag (parent_id)",
    "CREATE INDEX IF NOT EXISTS ix_tag_space_id ON tag (space_id)",
    "CREATE INDEX IF NOT EXISTS ix_asset_checksum ON asset (checksum)",
]

# 新建知识库按空间隔离名称；存量库原约束是租户级名称唯一，需换成空间级。
_OLD_TAG_CONSTRAINT = "uq_tag_tenant_name"
_NEW_TAG_CONSTRAINT = "uq_tag_space_name"

# DDL 拿锁等待上限：ALTER TABLE 即便是 IF NOT EXISTS 空操作也要 ACCESS EXCLUSIVE 锁，
# 排队期间会挡住该表的所有后续查询；宁可快速失败也不拖垮线上
_DDL_LOCK_TIMEOUT = "10s"


async def _ensure_additive_columns(engine: AsyncEngine) -> None:
    """补列 / 补索引 / 调约束：先查目录，已就绪的一律跳过，稳态运行不拿表级锁。"""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"SET LOCAL lock_timeout = '{_DDL_LOCK_TIMEOUT}'")
        have_columns = {
            (t, c)
            for t, c in await conn.execute(
                sa.text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = ANY (current_schemas(false))"
                )
            )
        }
        for table, ddl in _ADDITIVE_COLUMNS:
            # ddl 形如 "ADD COLUMN IF NOT EXISTS <列> <类型> ..."
            column = ddl.split("IF NOT EXISTS", 1)[1].split()[0]
            if (table.strip('"'), column) not in have_columns:  # "user" 等保留字表名带引号
                await conn.exec_driver_sql(f"ALTER TABLE {table} {ddl}")

        have_indexes = set(
            await conn.scalars(
                sa.text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = ANY (current_schemas(false))"
                )
            )
        )
        for ddl in _ADDITIVE_INDEXES:
            # ddl 形如 "CREATE INDEX IF NOT EXISTS <索引> ON ..."
            index = ddl.split("IF NOT EXISTS", 1)[1].split()[0]
            if index not in have_indexes:
                await conn.exec_driver_sql(ddl)

        have_constraints = set(
            await conn.scalars(
                sa.text("SELECT conname FROM pg_constraint WHERE conrelid = 'tag'::regclass")
            )
        )
        if _OLD_TAG_CONSTRAINT in have_constraints:
            await conn.exec_driver_sql(
                f"ALTER TABLE tag DROP CONSTRAINT IF EXISTS {_OLD_TAG_CONSTRAINT}"
            )
        if _NEW_TAG_CONSTRAINT not in have_constraints:
            await conn.exec_driver_sql(
                f"ALTER TABLE tag ADD CONSTRAINT {_NEW_TAG_CONSTRAINT} "
                "UNIQUE (tenant_id, space_id, name)"
            )


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
                "INSERT INTO space (id, slug, name, description, space_type, "
                "review_required, quota_storage_bytes, quota_file_count) "
                "VALUES (:id, 'default', '默认空间', "
                "'P1 摄取与检索闭环使用的种子默认空间', 'shared', false, 0, 0) "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": uuid.UUID(DEFAULT_SPACE_ID)},
        )
        # ---- 演示租户 + 演示用户 ----
        await conn.execute(
            sa.text(
                "INSERT INTO tenant (id, name, plan, status, "
                "quota_storage_bytes, quota_file_count) "
                "VALUES (:id, 'LoomVec 演示租户', 'free', 'active', 0, 0) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": uuid.UUID(SEED_TENANT_ID)},
        )
        for username, uid in SEED_USERS.items():
            await conn.execute(
                sa.text(
                    'INSERT INTO "user" (id, tenant_id, username, display_name, '
                    "status, auth_source) "
                    "VALUES (:id, :tenant_id, :username, :display_name, "
                    "'active', 'local') "
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
