"""资产域：资产 / 版本 / 派生物 / 标签分类元数据 / 语义单元（迁移 0002/0003）。"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class ReviewStatus(enum.StrEnum):
    """审核状态：仅 review_required 空间使用；None = 审核不适用。"""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class AssetStatus(enum.StrEnum):
    PENDING = "pending"  # 已登记，等待管线
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class GraphStatus(enum.StrEnum):
    """资产图谱抽取状态（P3-WRK-01）：与资产处理状态解耦，独立降级重试。

    pending=待抽取；ready=已入库；pending_retry=抽取无效待重试（分片不受牵连）；
    failed=重试耗尽；skipped=该类型不做图谱（图片等）。
    """

    PENDING = "pending"
    READY = "ready"
    PENDING_RETRY = "pending_retry"
    FAILED = "failed"
    SKIPPED = "skipped"


class Asset(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """资产：一个用户可见的文件/文本条目；内容演进走 asset_version。"""

    __tablename__ = "asset"

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("space.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    ext: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 原始对象在 raw bucket 的 key（文本摄取时内容直接入库，此列为空）
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)  # sha256
    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus, name="asset_status", values_callable=enum_values),
        nullable=False,
        default=AssetStatus.PENDING,
        index=True,
    )
    # failed 时的可读原因（worker 写入，任务记录可见）
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # 自由元信息（页数、语言等，解析步骤填充）
    asset_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # P2-WRK-02 先审后见：review_required 空间内管线完成后置 pending_review，
    # 审核通过才对 viewer 可见；驳回即下架（reviewer 理由见 review_reason）
    review_status: Mapped[ReviewStatus | None] = mapped_column(
        Enum(ReviewStatus, name="review_status", values_callable=enum_values),
        nullable=True,
        default=None,
        index=True,
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P2-CORE-04 分类（空间内树形分类；单选）
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("category.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # P2-CORE-04 空间 schema 元数据（键值按 metadata_field 定义校验后落 JSONB）
    user_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # P3-WRK-01 图谱抽取状态（GraphStatus；None=历史数据未参与图谱）
    graph_status: Mapped[GraphStatus | None] = mapped_column(
        Enum(GraphStatus, name="graph_status", values_callable=enum_values),
        nullable=True,
        default=None,
        index=True,
    )


class AssetTag(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """资产↔标签关联（多对多）。"""

    __tablename__ = "asset_tag"
    __table_args__ = (UniqueConstraint("asset_id", "tag_id", name="uq_asset_tag_pair"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tag.id", ondelete="CASCADE"), nullable=False, index=True
    )


class Tag(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """标签：租户级共享命名空间（空间内使用）。"""

    __tablename__ = "tag"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tag_tenant_name"),)

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Category(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """分类：空间内树形（parent_id 自引用；删除父级置子级 parent 为空由业务保证）。"""

    __tablename__ = "category"

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("space.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class MetadataFieldType(enum.StrEnum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    SELECT = "select"


class MetadataField(UuidPkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, Base):
    """空间元数据 schema 定义：资产 user_meta 的键必须在此登记且类型匹配。"""

    __tablename__ = "metadata_field"
    __table_args__ = (UniqueConstraint("space_id", "key", name="uq_metadata_field_space_key"),)

    space_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("space.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    field_type: Mapped[MetadataFieldType] = mapped_column(
        Enum(MetadataFieldType, name="metadata_field_type", values_callable=enum_values),
        nullable=False,
        default=MetadataFieldType.TEXT,
    )
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # select 类型的可选值列表
    options: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)


class RenditionKind(enum.StrEnum):
    THUMBNAIL = "thumbnail"
    PREVIEW = "preview"
    TRANSCODE = "transcode"  # P3-WRK-05 懒转码预览产物（H.264/AAC MP4）
    SUBTITLES = "subtitles"  # P3-WRK-04 转写字幕（WebVTT）


class AssetRendition(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """资产派生物（P2-CORE-05/WK-01：缩略图等，落对象存储 derived bucket）。"""

    __tablename__ = "asset_rendition"
    __table_args__ = (UniqueConstraint("asset_id", "version_id", "kind", name="uq_rendition_kind"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asset_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[RenditionKind] = mapped_column(
        Enum(RenditionKind, name="rendition_kind", values_callable=enum_values),
        nullable=False,
        default=RenditionKind.THUMBNAIL,
    )
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class AssetVersion(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """资产内容版本。P1 每次登记 v1；重解析/换版本在后续阶段扩展。"""

    __tablename__ = "asset_version"
    __table_args__ = (UniqueConstraint("asset_id", "version", name="uq_asset_version_seq"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 纯文本/Markdown 摄取时的原始内容（文件类资产此列为空，内容在对象存储）
    inline_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 幂等缓存键：(checksum, 解析器版本, 分片模型, prompt 版本)——03 文档 §二
    chunk_cache_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    version_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class UnitType(enum.StrEnum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"  # P2 图片资产单元（caption + EXIF 文本）


class ChunkMethod(enum.StrEnum):
    LLM_MARKERS = "llm_markers"
    STRUCTURAL_FALLBACK = "structural_fallback"


class SemanticUnit(UuidPkMixin, TenantMixin, TimestampMixin, Base):
    """语义单元：检索的最小粒度（03 文档 §二 marker 契约的落库形态）。"""

    __tablename__ = "semantic_unit"

    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("asset_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    space_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    unit_type: Mapped[UnitType] = mapped_column(
        Enum(UnitType, name="unit_type", values_callable=enum_values),
        nullable=False,
        default=UnitType.TEXT,
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 定位信息：{"pages": [..], "bbox": [...], "start_line": n, "end_line": m}
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 父子块：过大的 chunk 二次切分后子块指向父块（03 文档 §二.6）
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("semantic_unit.id", ondelete="SET NULL"), nullable=True
    )
    chunk_method: Mapped[ChunkMethod] = mapped_column(
        Enum(ChunkMethod, name="chunk_method", values_callable=enum_values), nullable=False
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 幂等：已嵌入且模型版本一致则嵌入/索引步骤跳过
    embed_model_version: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # P3-WRK-03/04 音视频单元的时间区间（秒；文档/图片单元为空）
    time_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_end: Mapped[float | None] = mapped_column(Float, nullable=True)
