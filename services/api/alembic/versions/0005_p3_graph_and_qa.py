"""P3-CORE-01 图谱与问答数据层：

- asset 扩展（graph_status 图谱抽取状态）；
- semantic_unit 扩展（time_start/time_end 音视频时间区间）；
- space 扩展（graph_merged_at/graph_entities_at_merge 合并簿记）；
- job_type 枚举扩展 graph/community/transcode；rendition_kind 扩展
  transcode/subtitles；
- 新表：entity（实体主表，事实源）/ entity_merge_log / community /
  chat_session / chat_message；
- AGE：确保扩展与图存在（compose init 已建，迁移对既有库兜底，幂等）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 loomvec.core.constants.GRAPH_NAME 保持一致（跨引用约定）
GRAPH_NAME = "loomvec_graph"


def _enum(name: str, *values: str):
    """列类型引用（不负责创建）；创建时机见各 create_table / 预建 DO block。

    注：须用 postgresql.ENUM——泛型 sa.Enum 在 SQLAlchemy 2.x 会忽略
    create_type=False 并在 create_table 时隐式 CREATE TYPE。
    """
    from sqlalchemy.dialects.postgresql import ENUM

    return ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    # ---- 枚举扩值（运行时才写入新值，PG12+ 允许事务内 ADD VALUE）----
    op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'graph' AFTER 'chunk'")
    op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'community' AFTER 'index'")
    op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'transcode' AFTER 'community'")
    op.execute("ALTER TYPE rendition_kind ADD VALUE IF NOT EXISTS 'transcode' AFTER 'preview'")
    op.execute("ALTER TYPE rendition_kind ADD VALUE IF NOT EXISTS 'subtitles' AFTER 'transcode'")

    # ---- asset / semantic_unit / space 扩展列 ----
    op.execute(
        "DO $$ BEGIN CREATE TYPE graph_status AS ENUM "
        "('pending', 'ready', 'pending_retry', 'failed', 'skipped'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.add_column(
        "asset",
        sa.Column(
            "graph_status",
            _enum("graph_status", *(("pending", "ready", "pending_retry", "failed", "skipped"))),
            nullable=True,
        ),
    )
    op.create_index("ix_asset_graph_status", "asset", ["graph_status"])
    op.add_column("semantic_unit", sa.Column("time_start", sa.Float(), nullable=True))
    op.add_column("semantic_unit", sa.Column("time_end", sa.Float(), nullable=True))
    op.add_column("space", sa.Column("graph_merged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("space", sa.Column("graph_entities_at_merge", sa.Integer(), nullable=True))

    # ---- community（先建：entity.community_id 引用它）----
    op.create_table(
        "community",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("space_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_model_version", sa.String(128), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # ---- entity 实体主表 ----
    op.create_table(
        "entity",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("space_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("entity_key", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("name_norm", sa.String(512), nullable=False),
        sa.Column("type", sa.String(64), nullable=False, server_default="other"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("aliases", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "community_id",
            UUID(as_uuid=True),
            sa.ForeignKey("community.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "merged_into",
            UUID(as_uuid=True),
            sa.ForeignKey("entity.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("space_id", "name_norm", "type", name="uq_entity_space_norm_type"),
    )
    op.create_index("ix_entity_merged_into", "entity", ["merged_into"])

    # ---- entity_merge_log ----
    op.execute(
        "DO $$ BEGIN CREATE TYPE merge_log_status AS ENUM ('applied', 'rolled_back'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.create_table(
        "entity_merge_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("space_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "winner_id",
            UUID(as_uuid=True),
            sa.ForeignKey("entity.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "loser_id",
            UUID(as_uuid=True),
            sa.ForeignKey("entity.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "status",
            _enum("merge_log_status", "applied", "rolled_back"),
            nullable=False,
            server_default="applied",
        ),
        sa.Column("reason", sa.String(64), nullable=False, server_default="manual"),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("snapshot", JSONB, nullable=False, server_default="{}"),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # ---- chat_session / chat_message ----
    op.execute(
        "DO $$ BEGIN CREATE TYPE chat_role AS ENUM ('user', 'assistant'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.create_table(
        "chat_session",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("space_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("title", sa.String(255), nullable=False, server_default="新会话"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_table(
        "chat_message",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_session.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role", _enum("chat_role", "user", "assistant"), nullable=False, server_default="user"
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", JSONB, nullable=False, server_default="[]"),
        sa.Column("graph_evidence", JSONB, nullable=False, server_default="[]"),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("meta", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # ---- AGE 扩展与图（compose init 01-age-init.sql 已建；对既有库幂等兜底）----
    # best-effort：AGE 是部署属性（compose 镜像内置；普通 PG 没有），
    # 缺失时跳过不阻断迁移——运行时图操作降级 graph_status=pending_retry
    bind = op.get_bind()
    age_installed = bind.exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'age')"
    ).scalar()
    if not age_installed:
        # SAVEPOINT 包裹：失败语句会把事务置 aborted，须回滚到保存点后才能继续
        try:
            with bind.begin_nested():
                bind.exec_driver_sql("CREATE EXTENSION age")
        except Exception:
            print("alembic[0005]: AGE 扩展不可用，跳过图谱初始化（运行时降级）")
            return
    graph_exists = bind.exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'loomvec_graph')"
    ).scalar()
    if not graph_exists:
        op.execute("LOAD 'age'")
        op.execute('SET search_path = ag_catalog, "$user", public')
        op.execute("SELECT create_graph('loomvec_graph')")


def downgrade() -> None:
    # 图数据随图名保留（AGE 无 schema 迁移；降级仅回滚关系层）
    op.drop_table("chat_message")
    op.drop_table("chat_session")
    op.execute("DROP TYPE IF EXISTS chat_role")
    op.drop_table("entity_merge_log")
    op.execute("DROP TYPE IF EXISTS merge_log_status")
    op.drop_table("entity")
    op.drop_table("community")
    op.drop_column("space", "graph_entities_at_merge")
    op.drop_column("space", "graph_merged_at")
    op.drop_column("semantic_unit", "time_end")
    op.drop_column("semantic_unit", "time_start")
    op.drop_index("ix_asset_graph_status", table_name="asset")
    op.drop_column("asset", "graph_status")
    op.execute("DROP TYPE IF EXISTS graph_status")
    # 枚举扩值不回退（迁移只增不删原则）
