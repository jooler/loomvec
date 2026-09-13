"""空间协作域：空间 / 成员 / 通知 / 配额计量（迁移 0002/0003）。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from loomvec.core.db.base import Base, SoftDeleteMixin, TenantMixin, TimestampMixin, UuidPkMixin
from loomvec.core.db.models._common import enum_values


class SpaceType(enum.StrEnum):
    SHARED = "shared"  # 协作空间（默认）
    PERSONAL = "personal"  # 个人空间


class Space(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """空间：多租户协作的基本单元（P2 起承载成员/审核/配额/检索语义）。"""

    __tablename__ = "space"

    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # P2-CORE-01 空间域字段
    space_type: Mapped[SpaceType] = mapped_column(
        Enum(SpaceType, name="space_type", values_callable=enum_values),
        nullable=False,
        default=SpaceType.SHARED,
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 白名单内的模型/预设选择（constants.EMBEDDING_MODELS / CHUNK_PRESETS）
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chunk_preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # P2-CORE-03 空间级配额（0 = 不限；租户级配额在 tenant 表）
    quota_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quota_file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # P4-API-03 平台治理封禁：封禁后成员访问被拒（数据保留）；解封置回 NULL
    banned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P3-CORE-03 空间级实体合并簿记：上次合并完成时间与当时的实体总数
    # （增量 ≥ graph.merge_trigger_delta 时周期任务触发合并）
    graph_merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    graph_entities_at_merge: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SpaceRole(enum.StrEnum):
    """空间角色：owner > editor > viewer（序数语义见 constants.SPACE_ROLES）。"""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class SpaceMember(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """空间成员：授权与可见性的唯一事实源（被移除即失权）。"""

    __tablename__ = "space_member"
    __table_args__ = (UniqueConstraint("space_id", "user_id", name="uq_space_member_pair"),)

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("space.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[SpaceRole] = mapped_column(
        Enum(SpaceRole, name="space_role", values_callable=enum_values),
        nullable=False,
        default=SpaceRole.VIEWER,
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Notification(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """用户通知（P2-API-06）：成员/审核/管线失败等事件落用户收件箱。"""

    __tablename__ = "notification"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SpaceUsage(UuidPkMixin, TimestampMixin, Base):
    """空间用量计量（P2-CORE-03）：登记/删除时事务内更新；租户用量为各空间求和。"""

    __tablename__ = "space_usage"

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("space.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
