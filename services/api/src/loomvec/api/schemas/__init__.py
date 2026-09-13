"""API 契约模型（Pydantic）：代码内注解即源，改这里后跑 `make sdk`。

约定（06 §三-8）：跨路由复用与核心域（资产等）的契约模型集中本目录（assets.py）；
路由本地的简单请求/响应模型可内联在路由文件。类名即 OpenAPI component 名，
移动文件不改名就不会破坏契约。
"""

from loomvec.api.schemas.assets import (
    AssetCreateRequest,
    AssetDetailOut,
    AssetListOut,
    AssetOut,
    AssetTextRequest,
    JobOut,
    PreviewOut,
    RetryRequest,
    UploadRequest,
    UploadResponse,
)

__all__ = [
    "AssetCreateRequest",
    "AssetDetailOut",
    "AssetListOut",
    "AssetOut",
    "AssetTextRequest",
    "JobOut",
    "PreviewOut",
    "RetryRequest",
    "UploadRequest",
    "UploadResponse",
]
