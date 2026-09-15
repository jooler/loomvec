"""用户分组唯一性改为部分唯一索引：软删行不占名（同名分组可重建）。

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 普通唯一约束连软删行一起约束：删除分组后同名重建撞唯一键（500）
    op.drop_constraint("uq_user_group_tenant_name", "user_group", type_="unique")
    op.create_index(
        "uq_user_group_tenant_name_live",
        "user_group",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where="deleted_at IS NULL",
    )


def downgrade() -> None:
    # 回滚要求软删行不与活行重名（存在重名时需先处理再降级）
    op.drop_index("uq_user_group_tenant_name_live", table_name="user_group")
    op.create_unique_constraint("uq_user_group_tenant_name", "user_group", ["tenant_id", "name"])
