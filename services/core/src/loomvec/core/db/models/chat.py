"""问答会话域：会话与消息（迁移 0005）。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from loomvec.core.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, UuidPkMixin
from loomvec.core.db.models._common import enum_values


class ChatSession(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """问答会话（P3-CORE-05）：作用域固定单空间（聚合问答后置）。"""

    __tablename__ = "chat_session"

    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新会话")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ChatMessage(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """问答消息（P3-CORE-05）：assistant 消息附 citations 与 graph_evidence。

    citations: [{index, unit_id, asset_id, asset_name, unit_type, title,
                 locator, text_snippet, locatable}]（index 与答案中 [n] 对应）；
    graph_evidence: [{head, relation, tail}]（实体→关系→实体路径）。
    """

    __tablename__ = "chat_message"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[ChatRole] = mapped_column(
        Enum(ChatRole, name="chat_role", values_callable=enum_values),
        nullable=False,
        default=ChatRole.USER,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    graph_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
