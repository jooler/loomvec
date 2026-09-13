"""P1-CORE-01 资产域：space / asset / asset_version / semantic_unit / processing_job

含 seed：默认空间（P1 单空间运行，slug=default，固定 UUID）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-12
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 固定默认空间 ID（幂等 seed，代码可引用）
DEFAULT_SPACE_ID = "0198bec0-0000-7000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "space",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "asset",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("space.id", ondelete="RESTRICT", name="fk_asset_space_id_space"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("ext", sa.String(16), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("storage_key", sa.String(1024), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "ready", "failed", name="asset_status"),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("asset_meta", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "asset_version",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("asset.id", ondelete="CASCADE", name="fk_asset_version_asset_id_asset"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("storage_key", sa.String(1024), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("inline_text", sa.Text(), nullable=True),
        sa.Column("chunk_cache_key", sa.String(255), nullable=True, index=True),
        sa.Column("version_meta", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("asset_id", "version", name="uq_asset_version_seq"),
    )

    op.create_table(
        "semantic_unit",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("asset.id", ondelete="CASCADE", name="fk_semantic_unit_asset_id_asset"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "version_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "asset_version.id",
                ondelete="CASCADE",
                name="fk_semantic_unit_version_id_asset_version",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("space_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column(
            "unit_type",
            sa.Enum("text", "table", name="unit_type"),
            nullable=False,
            server_default="text",
        ),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("keywords", JSONB(), nullable=False, server_default="[]"),
        sa.Column("locator", JSONB(), nullable=False, server_default="{}"),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "chunk_method",
            sa.Enum("llm_markers", "structural_fallback", name="chunk_method"),
            nullable=False,
        ),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embed_model_version", sa.String(128), nullable=True, index=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_semantic_unit_parent_id_semantic_unit",
        "semantic_unit",
        "semantic_unit",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "processing_job",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("asset.id", ondelete="CASCADE", name="fk_processing_job_asset_id_asset"),
            nullable=False,
            index=True,
        ),
        sa.Column("version_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "job_type",
            sa.Enum("parse", "chunk", "embed", "index", name="job_type"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "succeeded", "failed", name="job_status"),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_meta", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ---- seed 默认空间（P1 单空间运行）----
    op.execute(
        sa.text(
            "INSERT INTO space (id, slug, name, description) "
            "VALUES (:id, 'default', '默认空间', 'P1 摄取与检索闭环使用的种子默认空间') "
            "ON CONFLICT (slug) DO NOTHING"
        ).bindparams(id=uuid.UUID(DEFAULT_SPACE_ID))
    )


def downgrade() -> None:
    op.drop_table("processing_job")
    op.drop_table("semantic_unit")
    op.drop_table("asset_version")
    op.drop_table("asset")
    sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="chunk_method").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="unit_type").drop(op.get_bind(), checkfirst=True)
    op.drop_table("space")
    sa.Enum(name="asset_status").drop(op.get_bind(), checkfirst=True)
