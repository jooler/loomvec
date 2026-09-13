"""资产域契约模型：上传三步 / 资产 CRUD / 任务与预览（P1-API-01/02/05）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(gt=0)
    content_type: str | None = None
    space_id: uuid.UUID | None = None


class UploadResponse(BaseModel):
    key: str
    upload_url: str
    method: str = "PUT"
    expires_in: int


class AssetCreateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=1024, description="POST /uploads 返回的 key")
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(gt=0)
    content_type: str | None = None
    space_id: uuid.UUID | None = None


class AssetTextRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)
    space_id: uuid.UUID | None = None


class AssetOut(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID | None = None
    name: str
    mime_type: str
    ext: str
    size_bytes: int
    status: str
    status_reason: str | None = None
    review_status: str | None = None
    is_image: bool = False
    page_count: int | None = None
    checksum: str | None = None
    created_at: datetime
    created_by: uuid.UUID | None = None


class AssetListOut(BaseModel):
    items: list[AssetOut]
    next_cursor: str | None = None


class TagOut(BaseModel):
    id: uuid.UUID
    name: str


class RenditionOut(BaseModel):
    kind: str
    mime_type: str
    width: int | None = None
    height: int | None = None
    url: str | None = None


class AssetDetailOut(AssetOut):
    chunk_method: str | None = None
    version_meta: dict[str, Any] = {}
    asset_meta: dict[str, Any] = {}
    tags: list[TagOut] = []
    category_id: uuid.UUID | None = None
    metadata: dict[str, Any] = {}
    review_reason: str | None = None
    renditions: list[RenditionOut] = []


class AssetPatchRequest(BaseModel):
    """P2-API-03 资产编辑：名称 / 标签 / 分类 / 元数据（按空间 schema 校验）。"""

    name: str | None = Field(default=None, min_length=1, max_length=512)
    tags: list[str] | None = Field(default=None, description="整体替换标签（按名称）")
    category_id: uuid.UUID | None = None
    unset_category: bool = False
    metadata: dict[str, Any] | None = None


class DuplicateCheckOut(BaseModel):
    exists: bool
    assets: list[AssetOut] = []


class JobOut(BaseModel):
    id: uuid.UUID
    job_type: str
    status: str
    progress: float
    attempts: int
    error: str | None = None
    job_meta: dict[str, Any] = {}
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RetryRequest(BaseModel):
    step: str | None = Field(
        default=None, description="parse/chunk/embed/index；默认从首个失败步骤重跑"
    )


class PreviewOut(BaseModel):
    mode: Literal["pdf", "markdown", "image"]
    url: str | None = None
    content: str | None = None
    page_count: int | None = None


class MetadataFieldOut(BaseModel):
    """空间元数据 schema（P2-CORE-04；详情编辑器按此渲染）。"""

    id: uuid.UUID
    key: str
    name: str
    field_type: str
    required: bool
    options: list = []


class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None = None
