"""附件落盘与路径安全（14 文档 §5.8）。

- 文件/图片统一写 `workspace/.loomvec/uploads/{session_id}/{filename}`；
- realpath 防穿越：解出的绝对路径必须仍在 uploads 目录内；
- 类型/大小白名单校验（配置 agent.session.attachments）；
- 图片随 prompt 以 base64 SdkEncodedImageBlock 注入（模型原生可见）。
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from loomvec.agent.config import AgentRuntimeConfig

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_COPY_CHUNK = 1024 * 1024  # 流式落盘分块大小


@dataclass
class StoredAttachment:
    attachment_id: str
    path: str  # 相对 workspace 的路径
    size: int
    mime: str
    is_image: bool
    abs_path: Path


class AttachmentError(ValueError):
    pass


def sanitize_filename(name: str) -> str:
    name = (name or "attachment").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    name = _SAFE_NAME.sub("_", name).strip("._") or "attachment"
    return name[:120]


def store_attachment(
    cfg: AgentRuntimeConfig,
    env_id: str,
    session_id: str,
    filename: str,
    src: BinaryIO,
    mime: str,
) -> StoredAttachment:
    """流式校验并落盘；返回附件描述（越权/超限抛 AttachmentError）。

    src 为二进制文件对象（Starlette UploadFile 的 spooled file，大文件已在
    磁盘缓冲）；分块写入 + 写入中限长，避免整包进内存。
    文件名 = {attachment_id}-{sha256前8}_{原名}；sidecar `.meta.json` 记录
    mime/size（prompt 组装时按 id 找回并判断图片/文件注入方式）。
    """
    att = cfg.agent.session.attachments
    mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
    if mime not in att.allowed_mime:
        raise AttachmentError(f"不支持的附件类型：{mime}")

    import hashlib
    import json
    import uuid

    max_bytes = att.max_file_mb * 1024 * 1024
    uploads = cfg.uploads_dir(env_id, session_id)
    uploads.mkdir(parents=True, exist_ok=True)
    safe = sanitize_filename(filename)
    attachment_id = uuid.uuid4().hex[:12]
    digest = hashlib.sha256()
    size = 0
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=uploads, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            while chunk := src.read(_COPY_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise AttachmentError(f"附件超过 {att.max_file_mb}MB 上限")
                digest.update(chunk)
                tmp.write(chunk)
        if size == 0:
            raise AttachmentError("附件为空")
        target = uploads / f"{attachment_id}-{digest.hexdigest()[:8]}_{safe}"
        if target.resolve().parent != uploads.resolve():
            raise AttachmentError("非法附件路径")
        assert tmp_path is not None
        tmp_path.replace(target)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    (uploads / f"{attachment_id}.meta.json").write_text(
        json.dumps(
            {
                "attachment_id": attachment_id,
                "filename": safe,
                "path": str(target.relative_to(cfg.workspace(env_id))),
                "mime": mime,
                "size": size,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return StoredAttachment(
        attachment_id=attachment_id,
        path=str(target.relative_to(cfg.workspace(env_id))),
        size=size,
        mime=mime,
        is_image=mime in _IMAGE_MIME,
        abs_path=target,
    )


def resolve_attachment(
    cfg: AgentRuntimeConfig, env_id: str, session_id: str, attachment_id: str
) -> dict | None:
    """按 attachment_id 找回附件元数据（供 prompt 组装）。"""
    import json

    uploads = cfg.uploads_dir(env_id, session_id)
    meta_path = uploads / f"{attachment_id}.meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    target = uploads / meta.get("path", "").rsplit("/", 1)[-1]
    if not target.is_file() or target.resolve().parent != uploads.resolve():
        return None
    meta["abs_path"] = str(target)
    meta["is_image"] = str(meta.get("mime", "")).lower() in _IMAGE_MIME
    return meta
