"""chunk 管理：chunk_method 枚举追加 'manual'（前端手动新增的语义单元）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PG12+ 允许事务内 ADD VALUE（新值本迁移内不使用，运行时才写入）
    op.execute(
        "ALTER TYPE chunk_method ADD VALUE IF NOT EXISTS 'manual' AFTER 'structural_fallback'"
    )


def downgrade() -> None:
    # 枚举值不可安全移除（可能已被既有行引用）；回滚为无操作
    pass
