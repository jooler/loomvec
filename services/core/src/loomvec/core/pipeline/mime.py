"""P1-WRK-02 MIME 嗅探：魔数优先、扩展名兜底，双重校验白名单。

技术方案（03 文档 §一）指定 Tika 负责格式探测；P1 以纯 Python 魔数嗅探
覆盖当前白名单格式（pdf/office/text），接口保持 `sniff_mime` 单点，
后续引入 Tika（JVM sidecar）时仅替换本模块实现。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from loomvec.core.errors import ValidationError

EXT_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    # P2 图片管线（缩略图/EXIF/图文向量）
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    # P3 视频/音频管线（ffprobe/场景切分/转写/懒转码）
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".wav": "audio/x-wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}

# 图片 mime 家族（P2 图片管线分支判定）
IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/bmp",
}

# 音视频 mime 家族（P3 媒体管线分支判定）
VIDEO_MIMES = {
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
}
AUDIO_MIMES = {
    "audio/mpeg",
    "audio/x-wav",
    "audio/mp4",
    "audio/flac",
}
MEDIA_MIMES = VIDEO_MIMES | AUDIO_MIMES

# 魔数：图片格式（P2）
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

# 魔数：媒体容器（P3）；EBML 家族（webm/mkv）按扩展名细分，mp4/mov 按 ftyp brand
_MEDIA_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"ID3", "audio/mpeg"),
    (b"fLaC", "audio/flac"),
    (b"\x1aE\xdf\xa3", "ebml"),  # webm / mkv
)

# 走 MinerU 解析的格式（03 文档 §一）；其余白名单格式为文本直读
MINERU_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_OFFICE_EXT_BY_MIME = {v: k for k, v in EXT_TO_MIME.items() if k in (".docx", ".pptx", ".xlsx")}

_MIME_TO_EXT = {v: k for k, v in EXT_TO_MIME.items()}


def is_image_mime(mime: str) -> bool:
    """图片家族判定（P2 图片管线分支）。"""
    return mime in IMAGE_MIMES


def is_video_mime(mime: str) -> bool:
    return mime in VIDEO_MIMES


def is_audio_mime(mime: str) -> bool:
    return mime in AUDIO_MIMES


def is_media_mime(mime: str) -> bool:
    """音视频家族判定（P3 媒体管线分支）。"""
    return mime in MEDIA_MIMES


def is_native_video_mime(mime: str, native: list[str] | None = None) -> bool:
    """浏览器可直放的容器（懒转码豁免；配置可覆盖白名单）。"""
    allowed = set(native) if native else {"video/mp4", "video/webm"}
    return mime in allowed


def _ext(filename: str) -> str:
    return file_ext(filename)


def file_ext(filename: str) -> str:
    """规范化扩展名（小写，含点）；无扩展名返回空串。"""
    return PurePosixPath(filename.replace("\\", "/")).suffix.lower()


def _sniff_magic(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip"  # OOXML 家族，具体类型需结合扩展名
    # MP4/MOV/M4A：offset 4 起为 "ftyp" + brand
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand.startswith(b"qt"):
            return "video/quicktime"
        if brand.startswith(b"M4A"):
            return "audio/mp4"
        return "video/mp4"
    if data[:4] == b"RIFF":
        if data[8:12] == b"WAVE":
            return "audio/x-wav"
        if data[8:12] == b"WEBP":
            return "image/webp"
    # MP3 裸帧（0xFF Ex）
    if data[:1] == b"\xff" and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    for magic, mime in _MEDIA_MAGIC:
        if data.startswith(magic):
            return mime
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    return None


def sniff_mime(data: bytes, filename: str) -> tuple[str, str]:
    """返回 (mime, 规范化扩展名)；不在白名单时抛 ValidationError。"""
    ext = _ext(filename)
    if ext not in EXT_TO_MIME:
        raise ValidationError(
            f"不支持的文件类型：{ext or '(无扩展名)'}", allowed=sorted(EXT_TO_MIME)
        )

    magic = _sniff_magic(data)
    if magic == "application/pdf":
        if ext != ".pdf":
            raise ValidationError(f"文件内容为 PDF 但扩展名为 {ext}", ext=ext)
        return EXT_TO_MIME[".pdf"], ".pdf"
    if magic == "zip":
        if ext in (".docx", ".pptx", ".xlsx"):
            return EXT_TO_MIME[ext], ext
        raise ValidationError("ZIP 容器仅支持 docx/pptx/xlsx", ext=ext)
    if magic in IMAGE_MIMES:
        expected_ext = _MIME_TO_EXT.get(magic)
        # jpeg 双扩展名互通；其余图片要求扩展名与魔数一致
        if magic == "image/jpeg" and ext in (".jpg", ".jpeg"):
            return magic, ext
        if expected_ext and ext != expected_ext:
            raise ValidationError(f"文件内容为 {magic} 但扩展名为 {ext}", ext=ext)
        return magic, ext
    if magic == "ebml":
        # EBML 容器：webm / mkv 按扩展名细分
        if ext not in (".webm", ".mkv"):
            raise ValidationError("EBML 容器仅支持 webm/mkv", ext=ext)
        return EXT_TO_MIME[ext], ext
    if magic in MEDIA_MIMES:
        expected_ext = _MIME_TO_EXT.get(magic)
        if magic == "video/mp4" and ext == ".mov":
            # ftyp 无 qt brand 的 MOV 兼容：按扩展名放行
            return EXT_TO_MIME[".mov"], ext
        if magic == "audio/mpeg" and ext != ".mp3":
            raise ValidationError(f"文件内容为 {magic} 但扩展名为 {ext}", ext=ext)
        mp4_compat = magic == "video/mp4" and ext in (".mp4", ".mov")
        if expected_ext and ext != expected_ext and not mp4_compat:
            raise ValidationError(f"文件内容为 {magic} 但扩展名为 {ext}", ext=ext)
        return EXT_TO_MIME.get(ext, magic), ext
    # 非魔数格式：校验可解码为文本（utf-8 优先，容忍 latin-1）
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            data.decode("latin-1")
        except UnicodeDecodeError as e:
            raise ValidationError("文件既非已知二进制格式也无法按文本解码", ext=ext) from e
    mime = EXT_TO_MIME[ext]
    return mime, ext


def is_mineru_mime(mime: str) -> bool:
    return mime in MINERU_MIMES
