"""chat_session 改用户级多空间作用域：space_id → scope_space_ids (JSONB)。

revision: 0006
revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_session",
        sa.Column("scope_space_ids", JSONB, nullable=False, server_default="[]"),
    )
    # 既有单空间会话：召回范围回填为原空间
    op.execute("UPDATE chat_session SET scope_space_ids = to_jsonb(ARRAY[space_id::text])")
    op.drop_index("ix_chat_session_space_id")
    op.drop_column("chat_session", "space_id")


def downgrade() -> None:
    op.add_column(
        "chat_session",
        sa.Column("space_id", UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
    )
    # 不可逆精确还原：取范围内第一个空间，空范围落到占位 id
    op.execute(
        "UPDATE chat_session SET space_id = "
        "COALESCE((scope_space_ids->>0)::uuid, gen_random_uuid())"
    )
    op.create_index("ix_chat_session_space_id", "chat_session", ["space_id"])
    op.drop_column("chat_session", "scope_space_ids")
