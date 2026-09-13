"""对象存储 key 约定单源（P1/P2）：管线写入与级联清理共用，禁止散落 f-string。

bucket 约定见 config.StorageSettings：bucket_raw（原始文件）/ bucket_derived（派生物）。
key 布局：
- raw/{space_id}/{uuid}/{filename}          原始文件（预签名直传）
- parse/{asset_id}/v{n}/content.md          解析产物 Markdown
- parse/{asset_id}/v{n}/layout.json         版面 content_list
- embed/{asset_id}/v{n}/vectors.json        向量缓存（index 复用）
- thumbnails/{asset_id}/v{n}/thumb.webp     图片缩略图
"""

from __future__ import annotations

import uuid

RAW_PREFIX = "raw"
PARSE_PREFIX = "parse"
EMBED_PREFIX = "embed"
THUMBNAILS_PREFIX = "thumbnails"
KEYFRAMES_PREFIX = "keyframes"
TRANSCODE_PREFIX = "transcode"
THUMBNAIL_FILENAME = "thumb.webp"


def raw_upload_key(space_id: uuid.UUID, token: str, filename: str) -> str:
    """直传原始对象 key（token 为登记前生成的随机串）。"""
    return f"{RAW_PREFIX}/{space_id}/{token}/{filename}"


def raw_space_prefix(space_id: uuid.UUID) -> str:
    """空间全部原始对象前缀（空间删除级联）。"""
    return f"{RAW_PREFIX}/{space_id}/"


def parse_md_key(asset_id: uuid.UUID, version: int) -> str:
    return f"{PARSE_PREFIX}/{asset_id}/v{version}/content.md"


def parse_layout_key(asset_id: uuid.UUID, version: int) -> str:
    return f"{PARSE_PREFIX}/{asset_id}/v{version}/layout.json"


def embed_vectors_key(asset_id: uuid.UUID, version: int) -> str:
    return f"{EMBED_PREFIX}/{asset_id}/v{version}/vectors.json"


def thumbnail_key(asset_id: uuid.UUID, version: int) -> str:
    return f"{THUMBNAILS_PREFIX}/{asset_id}/v{version}/{THUMBNAIL_FILENAME}"


def keyframe_key(asset_id: uuid.UUID, version: int, index: int, ext: str = "jpg") -> str:
    """视频场景关键帧（P3-WRK-03）：index 为场景序号。"""
    return f"{KEYFRAMES_PREFIX}/{asset_id}/v{version}/scene-{index:04d}.{ext}"


def transcode_key(asset_id: uuid.UUID, version: int) -> str:
    """懒转码预览产物（P3-WRK-05）：H.264/AAC MP4。"""
    return f"{TRANSCODE_PREFIX}/{asset_id}/v{version}/preview.mp4"


def subtitles_key(asset_id: uuid.UUID, version: int) -> str:
    """转写字幕 WebVTT（P3-WRK-04）。"""
    return f"{TRANSCODE_PREFIX}/{asset_id}/v{version}/subtitles.vtt"


def asset_derived_prefixes(asset_id: uuid.UUID) -> list[str]:
    """单资产全部派生物前缀（资产/空间删除级联清理）。"""
    return [
        f"{PARSE_PREFIX}/{asset_id}/",
        f"{EMBED_PREFIX}/{asset_id}/",
        f"{THUMBNAILS_PREFIX}/{asset_id}/",
        f"{KEYFRAMES_PREFIX}/{asset_id}/",
        f"{TRANSCODE_PREFIX}/{asset_id}/",
    ]
