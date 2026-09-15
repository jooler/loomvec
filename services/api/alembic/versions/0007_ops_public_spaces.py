"""运营端与公共空间：用户分组 / 空间分组可见性 / 空间链接 + space_type 扩展 public。

- space_type 枚举追加 'public'（公共知识库空间，运营端创建维护）；
- 新表：user_group / user_group_member / space_group_visibility / space_link；
- seed：演示分组「公共访问」（alice/bob/carol），未挂任何空间。

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-14
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 loomvec.core.constants / 0003 迁移保持一致（seed 值跨引用约定）
SEED_TENANT_ID = "0198bec0-0000-7000-8000-0000000000a0"
SEED_USERS = {
    "alice": "0198bec0-0000-7000-8000-000000000101",
    "bob": "0198bec0-0000-7000-8000-000000000102",
    "carol": "0198bec0-0000-7000-8000-000000000103",
}
SEED_GROUP_NAME = "公共访问"


def upgrade() -> None:
    # 0005 为 AGE 图初始化设置过会话级 search_path（ag_catalog 优先），
    # 非限定 CREATE TABLE 会落入 ag_catalog schema——本迁移建表前先复位。
    op.execute('SET search_path = "$user", public')
    # PG12+ 允许事务内 ADD VALUE（新值本迁移内不使用，运行时才写入）
    op.execute("ALTER TYPE space_type ADD VALUE IF NOT EXISTS 'public' AFTER 'personal'")

    # ---- user_group ----
    op.create_table(
        "user_group",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
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
        sa.UniqueConstraint("tenant_id", "name", name="uq_user_group_tenant_name"),
    )

    # ---- user_group_member ----
    op.create_table(
        "user_group_member",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "user_group.id", ondelete="CASCADE", name="fk_group_member_group_id_group"
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE", name="fk_group_member_user_id_user"),
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
        sa.UniqueConstraint("group_id", "user_id", name="uq_user_group_member_pair"),
    )

    # ---- space_group_visibility ----
    op.create_table(
        "space_group_visibility",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "space.id", ondelete="CASCADE", name="fk_space_group_visibility_space_id_space"
            ),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "user_group.id", ondelete="CASCADE", name="fk_space_group_visibility_group_id_group"
            ),
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
        sa.UniqueConstraint("space_id", "group_id", name="uq_space_group_visibility_pair"),
    )

    # ---- space_link ----
    op.create_table(
        "space_link",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("uuidv7()"), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE", name="fk_space_link_user_id_user"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "space_id",
            UUID(as_uuid=True),
            sa.ForeignKey("space.id", ondelete="CASCADE", name="fk_space_link_space_id_space"),
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
        sa.UniqueConstraint("user_id", "space_id", name="uq_space_link_pair"),
    )

    # ---- seed：演示分组「公共访问」（alice/bob/carol）----
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "INSERT INTO user_group (tenant_id, name, description) "
            "VALUES (:tenant_id, :name, :description) "
            "ON CONFLICT (tenant_id, name) DO NOTHING RETURNING id"
        ).bindparams(
            tenant_id=uuid.UUID(SEED_TENANT_ID),
            name=SEED_GROUP_NAME,
            description="演示分组：默认包含全部种子用户",
        )
    )
    group_id = result.scalar_one_or_none()
    if group_id is None:
        group_id = bind.execute(
            sa.text("SELECT id FROM user_group WHERE tenant_id = :tenant_id AND name = :name")
        ).scalar_one()
    for uid in SEED_USERS.values():
        bind.execute(
            sa.text(
                "INSERT INTO user_group_member (group_id, user_id) "
                "VALUES (:group_id, :user_id) "
                "ON CONFLICT (group_id, user_id) DO NOTHING"
            ).bindparams(group_id=group_id, user_id=uuid.UUID(uid))
        )


def downgrade() -> None:
    # 枚举值 'public' 保留（PG 移除枚举值需重建类型，保留无副作用）
    op.execute('SET search_path = "$user", public')
    op.drop_table("space_link")
    op.drop_table("space_group_visibility")
    op.drop_table("user_group_member")
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM user_group WHERE name = :name").bindparams(name=SEED_GROUP_NAME)
    )
    op.drop_table("user_group")
