"""管线域：处理任务 / 重嵌入任务（迁移 0002/0004）。"""

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
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from loomvec.core.db.base import Base, TenantMixin, TimestampMixin, UuidPkMixin
from loomvec.core.db.models._common import enum_values


class JobType(enum.StrEnum):
    PARSE = "parse"
    CHUNK = "chunk"
    GRAPH = "graph"  # P3-WRK-01 图谱写入（PG 主表→AGE→Milvus entities）
    EMBED = "embed"
    INDEX = "index"
    COMMUNITY = "community"  # P3-WRK-02 社区检测+摘要（空间级任务记录挂在任一资产上无意义，
    # 实际由 celery 任务直跑；此枚举值供空间级 job 记录扩展使用）
    TRANSCODE = "transcode"  # P3-WRK-05 懒转码


class JobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ProcessingJob(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """管线任务记录：每步骤一行，失败原因可见、支持单步重跑（退出标准）。"""

    __tablename__ = "processing_job"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    job_type: Mapped[JobType] = mapped_column(
        Enum(JobType, name="job_type", values_callable=enum_values), nullable=False, index=True
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=enum_values),
        nullable=False,
        default=JobStatus.PENDING,
        index=True,
    )
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)  # 0~1
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ReembedTaskStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReembedTask(UuidPkMixin, TimestampMixin, Base):
    """重嵌入任务（P4-ADM-05）：按空间重置 semantic_unit.embed_model_version 后
    逐资产重跑 embed/index；完成后原子切换 space.embedding_model（过渡期检索不受影响）。"""

    __tablename__ = "reembed_task"

    space_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    target_model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ReembedTaskStatus] = mapped_column(
        Enum(ReembedTaskStatus, name="reembed_task_status", values_callable=enum_values),
        nullable=False,
        default=ReembedTaskStatus.PENDING,
        index=True,
    )
    total_assets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done_assets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_assets: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
