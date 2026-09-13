"""P2-CORE-01/03/04 空间协作与用户端：

- space 扩展（space_type/owner/review_required/embedding_model/chunk_preset/配额）；
- 新表：space_member / tag / asset_tag / category / metadata_field /
  asset_rendition / notification / space_usage；
- asset 扩展（审核状态 / 分类 / 元数据 JSONB）；
- seed：默认租户 + 演示用户（alice/bob/carol）+ 默认空间迁入成员模型
  （P1 默认空间归属种子租户，alice=owner、bob=editor、carol=viewer）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-12
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 loomvec.core.constants 保持一致（seed 值跨引用约定）
SEED_TENANT_ID = "0198bec0-0000-7000-8000-0000000000a0"
SEED_USERS = {
    "alice": "0198bec0-0000-7000-8000-000000000101",
    "bob": "0198bec0-0000-7000-8000-000000000102",
    "carol": "0198bec0-0000-7000-8000-000000000103",
}
DEFAULT_SPACE_ID = "0198bec0-0000-7000-8000-000000000001"


def _enum(name: str, *values: str) -> sa.Enum:
    """列类型引用（不负责创建）；创建时机见 upgrade() 内注释。"""
    return sa.Enum(*values, name=name, create_type=False)


def _create_enum_sql(name: str, values: list[str]) -> str:
    """幂等创建枚举（DO block 守卫；add_column 不会隐式创建类型）。"""
    labels = ", ".join(f"'{v}'" for v in values)
    return (
        f"DO $$ BEGIN CREATE TYPE {name} AS ENUM ({labels}); "
        f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )


def upgrade() -> None:
    # P2 图片管线：unit_type 枚举扩展 image（PG12+ 允许事务内 ADD VALUE，
    # 新值在本迁移内不使用，运行时才写入）
    op.execute("ALTER TYPE unit_type ADD VALUE IF NOT EXISTS 'image' AFTER 'table'")

    # 枚举创建时机：add_column 不会隐式建类型 → 空间/审核枚举需幂等预创建；
    # create_table 会隐式建类型 → space_role/metadata_field_type/rendition_kind
    # 交给对应 create_table，不在此预创建（否则重复 CREATE TYPE 报错）。
    op.execute(_create_enum_sql("space_type", ["shared", "personal"]))
    op.execute(_create_enum_sql("review_status", ["pending_review", "approved", "rejected"]))

    # ---- space 扩展列 ----
    op.add_column(
        "space",
        sa.Column(
            "space_type",
            _enum("space_type", "shared", "personal"),
            nullable=False,
            server_default="shared",
        ),
    )
    op.add_column("space", sa.Column("owner_id", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "space", sa.Column("review_required", sa.Boolean(), nullable=False, server_default="false")
    )
    op.add_column("space", sa.Column("embedding_model", sa.String(128), nullable=True))
    op.add_column("space", sa.Column("chunk_preset", sa.String(32), nullable=True))
    op.add_column(
        "space",
        sa.Column("quota_storage_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "space", sa.Column("quota_file_count", sa.Integer(), nullable=False, server_default="0")
    )

    # ---- asset 扩展列 ----
    op.add_column(
        "asset",
        sa.Column(
            "review_status",
            _enum("review_status", "pending_review", "approved", "rejected"),
            nullable=True,
        ),
    )
    op.add_column("asset", sa.Column("review_reason", sa.Text(), nullable=True))
    op.add_column("asset", sa.Column("reviewed_by", UUID(as_uuid=True), nullable=True))
    op.add_column("asset", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("asset", sa.Column("user_meta", JSONB(), nullable=False, server_default="{}"))
    op.add_column("asset", sa.Column("category_id", UUID(as_uuid=True), nullable=True))

    # ---- space_member ----
    op.create_table(
        "space_member",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("space.id", ondelete="CASCADE", name="fk_space_member_space_id_space"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE", name="fk_space_member_user_id_user"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role",
            _enum("space_role", "owner", "editor", "viewer"),
            nullable=False,
            server_default="viewer",
        ),
        sa.Column("invited_by", UUID(as_uuid=True), nullable=True),
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
        sa.UniqueConstraint("space_id", "user_id", name="uq_space_member_pair"),
    )

    # ---- tag / asset_tag ----
    op.create_table(
        "tag",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
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
        sa.UniqueConstraint("tenant_id", "name", name="uq_tag_tenant_name"),
    )
    op.create_table(
        "asset_tag",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("asset.id", ondelete="CASCADE", name="fk_asset_tag_asset_id_asset"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "tag_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tag.id", ondelete="CASCADE", name="fk_asset_tag_tag_id_tag"),
            nullable=False,
            index=True,
        ),
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
        sa.UniqueConstraint("asset_id", "tag_id", name="uq_asset_tag_pair"),
    )

    # ---- category ----
    op.create_table(
        "category",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("space.id", ondelete="CASCADE", name="fk_category_space_id_space"),
            nullable=False,
            index=True,
        ),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
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
    op.create_foreign_key(
        "fk_category_parent_id_category",
        "category",
        "category",
        ["parent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_asset_category_id_category",
        "asset",
        "category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---- metadata_field ----
    op.create_table(
        "metadata_field",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("space.id", ondelete="CASCADE", name="fk_metadata_field_space_id_space"),
            nullable=False,
            index=True,
        ),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "field_type",
            _enum("metadata_field_type", "text", "number", "date", "select"),
            nullable=False,
            server_default="text",
        ),
        sa.Column("required", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("options", JSONB(), nullable=False, server_default="[]"),
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
        sa.UniqueConstraint("space_id", "key", name="uq_metadata_field_space_key"),
    )

    # ---- asset_rendition ----
    op.create_table(
        "asset_rendition",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "asset_id",
            UUID(as_uuid=True),
            sa.ForeignKey("asset.id", ondelete="CASCADE", name="fk_asset_rendition_asset_id_asset"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "version_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "asset_version.id",
                ondelete="CASCADE",
                name="fk_asset_rendition_version_id_asset_version",
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "kind",
            _enum("rendition_kind", "thumbnail", "preview"),
            nullable=False,
            server_default="thumbnail",
        ),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False, server_default=""),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
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
        sa.UniqueConstraint("asset_id", "version_id", "kind", name="uq_rendition_kind"),
    )

    # ---- notification ----
    op.create_table(
        "notification",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE", name="fk_notification_user_id_user"),
            nullable=False,
            index=True,
        ),
        sa.Column("type", sa.String(64), nullable=False, index=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
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

    # ---- space_usage ----
    op.create_table(
        "space_usage",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("space.id", ondelete="CASCADE", name="fk_space_usage_space_id_space"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
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

    # ---- seed：默认租户 + 演示用户 + 默认空间迁入成员模型 ----
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO tenant (id, name, plan) "
            "VALUES (:id, 'LoomVec 演示租户', 'free') ON CONFLICT (id) DO NOTHING"
        ).bindparams(id=uuid.UUID(SEED_TENANT_ID))
    )
    for username, uid in SEED_USERS.items():
        bind.execute(
            sa.text(
                'INSERT INTO "user" (id, tenant_id, username, display_name, auth_source) '
                "VALUES (:id, :tenant_id, :username, :display_name, 'local') "
                "ON CONFLICT (username) DO NOTHING"
            ).bindparams(
                id=uuid.UUID(uid),
                tenant_id=uuid.UUID(SEED_TENANT_ID),
                username=username,
                display_name=username.capitalize(),
            )
        )
    # P1 默认空间迁入 P2 模型：归属种子租户，alice 持有
    bind.execute(
        sa.text(
            "UPDATE space SET tenant_id = :tenant_id, owner_id = :alice, space_type = 'shared' "
            "WHERE id = :space_id"
        ).bindparams(
            tenant_id=uuid.UUID(SEED_TENANT_ID),
            alice=uuid.UUID(SEED_USERS["alice"]),
            space_id=uuid.UUID(DEFAULT_SPACE_ID),
        )
    )
    for username, role in (("alice", "owner"), ("bob", "editor"), ("carol", "viewer")):
        bind.execute(
            sa.text(
                "INSERT INTO space_member (tenant_id, space_id, user_id, role) "
                'SELECT :tenant_id, :space_id, u.id, CAST(:role AS space_role) FROM "user" u '
                "WHERE u.username = :username "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM space_member m "
                "  WHERE m.space_id = :space_id AND m.user_id = u.id)"
            ).bindparams(
                tenant_id=uuid.UUID(SEED_TENANT_ID),
                space_id=uuid.UUID(DEFAULT_SPACE_ID),
                username=username,
                role=role,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    # 移除成员 seed（默认空间回退为无归属）
    bind.execute(
        sa.text("DELETE FROM space_member WHERE space_id = :space_id").bindparams(
            space_id=uuid.UUID(DEFAULT_SPACE_ID)
        )
    )
    op.drop_table("space_usage")
    op.drop_table("notification")
    op.drop_table("asset_rendition")
    op.drop_table("metadata_field")
    op.drop_constraint("fk_asset_category_id_category", "asset", type_="foreignkey")
    op.drop_table("category")
    op.drop_table("asset_tag")
    op.drop_table("tag")
    op.drop_table("space_member")
    op.drop_column("asset", "category_id")
    op.drop_column("asset", "user_meta")
    op.drop_column("asset", "reviewed_at")
    op.drop_column("asset", "reviewed_by")
    op.drop_column("asset", "review_reason")
    op.drop_column("asset", "review_status")
    op.drop_column("space", "quota_file_count")
    op.drop_column("space", "quota_storage_bytes")
    op.drop_column("space", "chunk_preset")
    op.drop_column("space", "embedding_model")
    op.drop_column("space", "review_required")
    op.drop_column("space", "owner_id")
    op.drop_column("space", "space_type")
    sa.Enum(name="rendition_kind").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="metadata_field_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="review_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="space_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="space_type").drop(op.get_bind(), checkfirst=True)
