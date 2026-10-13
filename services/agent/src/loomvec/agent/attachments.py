"""附件校验与上传（14 文档 §5.8；P2 起落盘走 acp-gateway 文件接口）。

- 文件 / 图片统一存 workspace 的 `.loomvec/uploads/{session_id}/{attachment_id}`，
  经 `PUT /v1/workspaces/{id}/files/...` 上传（原始字节）；
- `attachment_id` = `{12 位随机 hex}_{清洗后的文件名}`（随机前缀防重名；同时就是网关侧文件名）；
- 路径安全由两层保证：这里对 attachment_id 做严格白名单（无分隔符 / 穿越），网关再做 realpath 校验；
- 类型 / 大小白名单沿用配置 `agent.session.attachments`；
- 图片随 prompt 以 base64 `image` 块注入，其他文件以 `resource_link` + 文字说明指向
  `workspace:///.loomvec/uploads/...`（见 gateway/orchestrator.py）。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import BinaryIO

from loomvec.agent.config import AgentRuntimeConfig
from loomvec.agent.gateway.client import GatewayClient
from loomvec.agent.gateway.errors import GatewayError

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-一-鿿]+")
_ATTACHMENT_ID = re.compile(r"^[0-9a-f]{12}_[A-Za-z0-9._\-一-鿿]{1,200}$")
_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_COPY_CHUNK = 1024 * 1024  # 流式上传分块大小
_MAX_NAME_BYTES = 200  # 网关单段文件名上限 255 字节，留出随机前缀的余量
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".csv": "text/csv",
}


@dataclass(frozen=True)
class AttachmentMeta:
    attachment_id: str
    path: str  # 相对 workspace 的路径（`.loomvec/uploads/{session_id}/{attachment_id}`）
    name: str  # 清洗后的原文件名
    size: int
    mime: str
    is_image: bool


class AttachmentError(ValueError):
    pass


def uploads_prefix(session_id: str) -> str:
    return f".loomvec/uploads/{session_id}"


def sanitize_filename(name: str) -> str:
    name = (name or "attachment").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = _SAFE_NAME.sub("_", name).strip("._") or "attachment"
    # 先留出扩展名，再把主干同时收紧到 120 字符与 UTF-8 字节预算（中文 3 字节/字）
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext) > 10:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    stem = stem[:120]
    while stem and len((stem + suffix).encode()) > _MAX_NAME_BYTES:
        stem = stem[:-1]
    return (stem + suffix) or "attachment"


def mime_for_name(name: str) -> str:
    dot = name.rfind(".")
    if dot < 0:
        return "application/octet-stream"
    return _EXT_MIME.get(name[dot:].lower(), "application/octet-stream")


def validate_upload(cfg: AgentRuntimeConfig, filename: str, mime: str | None) -> tuple[str, str]:
    """类型白名单校验 → (清洗后的文件名, 规范化 mime)。"""
    att = cfg.agent.session.attachments
    mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
    if mime not in att.allowed_mime:
        raise AttachmentError(f"不支持的附件类型：{mime}")
    return sanitize_filename(filename), mime


def _file_size(src: BinaryIO) -> int:
    src.seek(0, 2)
    size = src.tell()
    src.seek(0)
    return size


async def _iter_file(src: BinaryIO, limit: int) -> AsyncIterator[bytes]:
    sent = 0
    while chunk := await asyncio.to_thread(src.read, _COPY_CHUNK):
        sent += len(chunk)
        if sent > limit:
            raise AttachmentError("附件超过大小上限")
        yield chunk


async def store_attachment(
    cfg: AgentRuntimeConfig,
    client: GatewayClient,
    workspace_id: str,
    session_id: str,
    filename: str,
    src: BinaryIO,
    mime: str | None,
) -> AttachmentMeta:
    """校验并流式上传到网关 workspace（越权 / 超限抛 AttachmentError）。

    src 为二进制文件对象（Starlette UploadFile 的 spooled file，大文件已在磁盘缓冲）。
    """
    safe, mime = validate_upload(cfg, filename, mime)
    att = cfg.agent.session.attachments
    max_bytes = att.max_file_mb * 1024 * 1024
    size = _file_size(src)
    if size == 0:
        raise AttachmentError("附件为空")
    if size > max_bytes:
        raise AttachmentError(f"附件超过 {att.max_file_mb}MB 上限")
    attachment_id = f"{uuid.uuid4().hex[:12]}_{safe}"
    path = f"{uploads_prefix(session_id)}/{attachment_id}"
    await client.put_file(workspace_id, path, _iter_file(src, max_bytes), size=size)
    return AttachmentMeta(
        attachment_id=attachment_id,
        path=path,
        name=safe,
        size=size,
        mime=mime,
        is_image=mime in _IMAGE_MIME,
    )


async def resolve_attachment(
    client: GatewayClient, workspace_id: str, session_id: str, attachment_id: str
) -> AttachmentMeta | None:
    """按 attachment_id 向网关取元信息（`GET files/...?stat=1`）；不存在 / 非法 id 返回 None。"""
    if not _ATTACHMENT_ID.fullmatch(attachment_id):
        return None
    path = f"{uploads_prefix(session_id)}/{attachment_id}"
    try:
        stat = await client.stat_file(workspace_id, path)
    except GatewayError as e:
        if e.status in (400, 404):
            return None
        raise
    if stat.get("type") != "file":
        return None
    name = attachment_id.split("_", 1)[1]
    mime = mime_for_name(name)
    return AttachmentMeta(
        attachment_id=attachment_id,
        path=path,
        name=name,
        size=int(stat.get("size") or 0),
        mime=mime,
        is_image=mime in _IMAGE_MIME,
    )
