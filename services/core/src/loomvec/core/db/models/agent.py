"""智能体工作环境域：环境与会话元数据（迁移 0011，14 文档 §7）。

消息事实源在 env 目录树内的 dsh session.jsonl，本域只存 UI 元数据与治理
字段（列表/重命名/软删归档）；env_id 与账号解耦，交接 = owner 重绑。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from loomvec.core.db.base import Base, TenantMixin, TimestampMixin, UuidPkMixin
from loomvec.core.db.models._common import enum_values


class AgentEnvironmentStatus(enum.StrEnum):
    ACTIVE = "active"
    TRANSFERRED = "transferred"
    ARCHIVED = "archived"


class AgentEnvironment(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """用户服务端工作环境（env_id = 目录树主键）：dsh-home + workspace。

    owner_user_id 为「当前持有者」（交接即重绑，prev_owner/transferred_at 留
    审计）；用户账号硬删不连带环境（SET NULL），工作成果与账号生命周期解耦。
    一个用户默认一个 env，多 env（日常/项目）经本表扩展。
    """

    __tablename__ = "agent_environment"

    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="我的工作环境")
    status: Mapped[AgentEnvironmentStatus] = mapped_column(
        Enum(
            AgentEnvironmentStatus,
            name="agent_environment_status",
            values_callable=enum_values,
        ),
        nullable=False,
        default=AgentEnvironmentStatus.ACTIVE,
    )
    prev_owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    transferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentSession(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """智能体会话元数据：id 即 dsh session_id（网关生成 uuid 后传入）。

    dsh 无删除/重命名/列表 API（14 文档 §2.1），本表补齐这些 UI 语义；
    archived_at 兼作软删与交接归档标记（归档列表默认不展示、admin 可查）。
    """

    __tablename__ = "agent_session"

    env_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_environment.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 审计不变量：创建者不随交接变化（账号硬删置空，事实源 JSONL 仍在）
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新会话")
    # 会话绑定项目目录（workspace 相对路径，空 = workspace 根；P5.5a §6.1）：
    # 提问时经 orchestrator 注入工作目录前缀，交付物约定写入该目录。
    # server_default 与 init_db 的 ADD COLUMN DDL 对齐（裸 SQL 插入不缺省）
    project_path: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    # 会话检索范围：随提问签发进 agent token（严格遵循用户设置）；
    # 空列表 = 未设置 → MCP fail-closed，任何空间都不可检索
    scope_space_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    preview: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
