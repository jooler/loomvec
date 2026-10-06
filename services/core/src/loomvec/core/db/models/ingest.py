"""目录监听入库域：已扫描文件台账（worker/ingest_watch.py 读写）。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from loomvec.core.db.base import Base, TenantMixin, TimestampMixin, UuidPkMixin


class IngestFileStatus(enum.StrEnum):
    """台账状态：pending_stable → uploaded → done / duplicate；登记前失败 → error。"""

    PENDING_STABLE = "pending_stable"  # 已发现，等待 size+mtime 稳定
    UPLOADED = "uploaded"  # 已登记资产（asset_id 已写），尚未入队；每轮扫描补偿入队
    DONE = "done"  # 已入队管线
    # 目标空间或已发布的公共空间已有可用的同内容资产，跳过（asset_id 指向该资产）
    DUPLICATE = "duplicate"
    ERROR = "error"  # 登记前失败（error_message）；文件 size/mtime 变化后重新处理


class IngestFile(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """监听目录中的一个文件（每个 watch_dir + file_path 一行）。

    稳定判定：(file_size, file_mtime) 至少连续两轮扫描一致且自 stable_since 起持续
    ingest_watch.stability_seconds 秒；变化即刷新 stable_since 并回到 pending_stable
    （checksum/asset_id 保留，内容未变时判 duplicate）。
    """

    __tablename__ = "ingest_file"
    __table_args__ = (UniqueConstraint("watch_dir", "file_path", name="uq_ingest_file_path"),)

    watch_dir: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_mtime: Mapped[float] = mapped_column(Float, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # sha256
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IngestFileStatus.PENDING_STABLE.value, index=True
    )
    # 外键延迟到提交时校验：登记时台账先写预分配的 asset_id，资产行随后在同一事务插入
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asset.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"),
        nullable=True,
        index=True,
    )
    # 当前 (file_size, file_mtime) 首次被观察到的时间（稳定计时起点）
    stable_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 最近一次在目录中见到该文件的时间（终态行最多每天刷新一次）；文件消失超过保留期即删行
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
