"""Finder 式资产管理：asset_folder 表（空间内树形目录）+ asset.folder_id 挂靠列。

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset_folder",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuidv7()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("space.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["asset_folder.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_asset_folder_space_id", "asset_folder", ["space_id"])
    op.create_index("ix_asset_folder_parent_id", "asset_folder", ["parent_id"])
    op.create_index("ix_asset_folder_tenant_id", "asset_folder", ["tenant_id"])

    op.add_column("asset", sa.Column("folder_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_asset_folder_id_asset_folder",
        "asset",
        "asset_folder",
        ["folder_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_asset_folder_id", "asset", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_folder_id", table_name="asset")
    op.drop_constraint("fk_asset_folder_id_asset_folder", "asset", type_="foreignkey")
    op.drop_column("asset", "folder_id")
    op.drop_index("ix_asset_folder_tenant_id", table_name="asset_folder")
    op.drop_index("ix_asset_folder_parent_id", table_name="asset_folder")
    op.drop_index("ix_asset_folder_space_id", table_name="asset_folder")
    op.drop_table("asset_folder")
