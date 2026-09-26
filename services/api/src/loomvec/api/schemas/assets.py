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
    folder_id: uuid.UUID | None = Field(default=None, description="目标文件夹（空 = 空间根目录）")


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
    folder_id: uuid.UUID | None = Field(default=None, description="目标文件夹（空 = 空间根目录）")


class AssetTextRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)
    space_id: uuid.UUID | None = None
    folder_id: uuid.UUID | None = Field(default=None, description="目标文件夹（空 = 空间根目录）")


class PaperMetaOut(BaseModel):
    """列表用论文元数据投影（来自 asset_meta.paper；非论文为 null）。"""

    year: int | None = None
    journal: str | None = None
    title: str | None = None
    first_author: str | None = None
    authors: list[str] = []
    doi: str | None = None
    source: str | None = None


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
    folder_id: uuid.UUID | None = None
    paper: PaperMetaOut | None = None
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
    """P2-API-03 资产编辑：名称 / 标签 / 分类 / 元数据（按空间 schema 校验）。

    Finder 文件管理：folder_id / unset_folder 移动资产（目标文件夹须同空间）。
    """

    name: str | None = Field(default=None, min_length=1, max_length=512)
    tags: list[str] | None = Field(default=None, description="整体替换标签（按名称）")
    category_id: uuid.UUID | None = None
    unset_category: bool = Field(default=False)
    metadata: dict[str, Any] | None = None
    folder_id: uuid.UUID | None = None
    unset_folder: bool = Field(default=False, description="移动回空间根目录")


class AutoRenameOut(BaseModel):
    """论文 PDF 自动命名结果（按元数据重写「年份-期刊-标题-作者」）。"""

    name: str
    renamed: bool
    reason: str | None = None


class FolderOut(BaseModel):
    """文件夹（Finder 目录树节点）。"""

    id: uuid.UUID
    space_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    created_at: datetime
    created_by: uuid.UUID | None = None


class FolderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    parent_id: uuid.UUID | None = Field(default=None, description="父文件夹（空 = 根目录）")


class FolderRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=512)


class FolderMoveRequest(BaseModel):
    """移动文件夹（同空间内）：parent_id 为空 = 移到根目录。"""

    parent_id: uuid.UUID | None = None
    unset_parent: bool = Field(default=False, description="移到空间根目录")


class FolderCopyRequest(BaseModel):
    """复制文件夹（递归，含资产）：目标父文件夹（同空间；空 = 根目录）。"""

    parent_id: uuid.UUID | None = None


class AssetMoveRequest(BaseModel):
    """移动资产（同空间内）：folder_id 为空 = 移到根目录。"""

    folder_id: uuid.UUID | None = None
    unset_folder: bool = Field(default=False, description="移到空间根目录")


class AssetCopyRequest(BaseModel):
    """复制资产：同空间内复制到目标文件夹；跨空间须为目标空间成员（editor+）。"""

    target_space_id: uuid.UUID | None = None
    target_folder_id: uuid.UUID | None = None


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
    # file = 仅原文、无解析产物（docx/xlsx 等尚未产出 md_key）
    mode: Literal["pdf", "markdown", "image", "file"]
    url: str | None = None
    content: str | None = None
    page_count: int | None = None
    # 原始对象预签名 URL（解析类资产可同时呈现原文与 MinerU 产物入口；文本直摄为空）
    original_url: str | None = None
    # 解析产物（Markdown）预签名 URL：markdown 模式与 url 同值；PDF 也可能有产物
    parsed_url: str | None = None


class UnitOut(BaseModel):
    """语义单元（chunk）只读字段 + 管理状态（embed_model_version 判断是否已向量化）。"""

    id: uuid.UUID
    unit_type: str
    title: str | None = None
    content: str
    keywords: list = []
    locator: dict[str, Any] = {}
    chunk_method: str
    order_index: int
    char_count: int
    embed_model_version: str | None = None
    created_at: datetime


class UnitListOut(BaseModel):
    items: list[UnitOut]
    total: int


class UnitCreateRequest(BaseModel):
    """新增 chunk（RAGFlow 式手动补片）：创建后同步向量化并写入索引。"""

    content: str = Field(min_length=1, max_length=100_000)
    title: str | None = Field(default=None, max_length=512)
    keywords: list[str] | None = None
    unit_type: str = Field(default="text", description="text / table")


class UnitPatchRequest(BaseModel):
    """编辑 chunk：内容变更后重新向量化（原模型版本清零 → 同步重嵌）。"""

    title: str | None = Field(default=None, max_length=512)
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    keywords: list[str] | None = None


class UnitBulkDeleteRequest(BaseModel):
    unit_ids: list[uuid.UUID] = Field(min_length=1)


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
