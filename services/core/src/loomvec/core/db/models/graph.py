"""图谱域：实体 / 合并日志 / 社区（迁移 0005）。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
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

# ---------------------------------------------------------------------------
# P3-CORE 图谱与问答：entity 实体主表（事实源）、entity_merge_log、community、
# chat_session / chat_message。迁移 0005。
# AGE（openCypher）存图结构（同库，事务级联见 core/graph/age.py），
# Milvus entities 集合存链接用向量（可由 entity 主表全量重建）。
# ---------------------------------------------------------------------------


class Entity(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """实体主表（03 文档 §3.4 事实源）：规范名/类型/描述按空间唯一。

    entity_key = sha256(space_id | normalized(name) | type) 的十六进制摘要——
    确定性 ID（P3-CORE-01），AGE 节点与 Milvus entities 主键共用，
    重跑/重建天然幂等。name_norm 存归一化规范名供精确链接。
    """

    __tablename__ = "entity"
    __table_args__ = (
        UniqueConstraint("space_id", "name_norm", "type", name="uq_entity_space_norm_type"),
    )

    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    entity_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)  # 规范名（首次出现形态）
    name_norm: Mapped[str] = mapped_column(String(512), nullable=False)  # 归一化规范名
    type: Mapped[str] = mapped_column(String(64), nullable=False, default="other")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 所属社区（P3-WRK-02 Leiden 检测结果；未检测为空）
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("community.id", ondelete="SET NULL"), nullable=True
    )
    # 阶段二合并：败者实体指向胜者（合并回滚经 merge_log 快照恢复）
    merged_into: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity.id", ondelete="SET NULL"), nullable=True
    )


class MergeLogStatus(enum.StrEnum):
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


class EntityMergeLog(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """实体合并日志（P3-CORE-03）：每次合并一行，可审阅、可回滚。

    snapshot 记录败者实体原值 + 受影响边（AGE 重写前后）+ 胜者原 description，
    回滚时据此逆向恢复（不依赖 AGE 历史）。
    """

    __tablename__ = "entity_merge_log"

    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    winner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entity.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 败者行被回滚恢复后 id 不变；硬删场景 FK 置空，id 保留在 snapshot
    loser_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entity.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[MergeLogStatus] = mapped_column(
        Enum(MergeLogStatus, name="merge_log_status", values_callable=enum_values),
        nullable=False,
        default=MergeLogStatus.APPLIED,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="manual")
    # 合并依据的相似度（向量余弦/编辑距离；人工为空）
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )  # 用户 id 或 "system"


class Community(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """社区（P3-WRK-02）：Leiden 检测结果 + LLM 摘要；摘要入 Milvus 供 L4 召回。"""

    __tablename__ = "community"

    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)  # 如 C0-3（算法批次-序号）
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending / ready / failed
