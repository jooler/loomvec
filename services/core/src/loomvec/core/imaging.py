"""P2-CORE-05 图片处理服务：缩略图（EXIF 转向）、EXIF 元数据提取。

- 缩略图：优先 pyvips（需系统 libvips），不可用时回退 Pillow（纯轮子，
  本地开发/CI 无外部依赖）；两者都按 EXIF Orientation 摆正后缩放；
- EXIF：调用 exiftool（-j JSON 输出），二进制缺失/失败时优雅降级为空，
  仅保留白名单键（image.exif_whitelist），避免设备隐私字段全量入库；
- 函数均为纯 IO 工具，编排见 pipeline/steps.py 的图片分支。
"""

from __future__ import annotations

import asyncio
import json
import shutil

import structlog

logger = structlog.get_logger("loomvec.imaging")

THUMBNAIL_MIME = "image/webp"


def _load_backend():
    """缩略图后端探测：pyvips 优先，Pillow 兜底；返回 (name, callable) 或 None。"""
    try:
        import pyvips  # type: ignore[import-not-found]

        def vips_thumb(data: bytes, max_w: int, max_h: int, quality: int) -> tuple[bytes, int, int]:
            img = pyvips.Image.thumbnail_buffer(data, width=max_w, height=max_h, fit="inside")
            out = img.webpsave_buffer(Q=quality)
            return bytes(out), img.width, img.height

        return "pyvips", vips_thumb
    except Exception:
        pass
    try:
        from io import BytesIO

        from PIL import Image, ImageOps

        def pillow_thumb(
            data: bytes, max_w: int, max_h: int, quality: int
        ) -> tuple[bytes, int, int]:
            img = ImageOps.exif_transpose(Image.open(BytesIO(data)))
            img.thumbnail((max_w, max_h))
            out = BytesIO()
            img.save(out, format="WEBP", quality=quality)
            return out.getvalue(), img.width, img.height

        return "pillow", pillow_thumb
    except Exception as e:  # pragma: no cover - 无任何图像库
        logger.warning("image_backend_unavailable", error=str(e))
        return None


_BACKEND = None


def _backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _load_backend()
    return _BACKEND


def backend_name() -> str | None:
    b = _backend()
    return b[0] if b else None


async def make_thumbnail(
    data: bytes, *, max_width: int, max_height: int, quality: int = 82
) -> tuple[bytes, int, int] | None:
    """生成缩略图：返回 (webp bytes, width, height)；无可用后端时 None。"""
    b = _backend()
    if b is None:
        return None
    _, fn = b

    def _run() -> tuple[bytes, int, int]:
        return fn(data, max_width, max_height, quality)

    return await asyncio.to_thread(_run)


async def extract_exif(data: bytes, *, whitelist: list[str]) -> dict[str, str]:
    """exiftool 提取（-j）；工具缺失/失败返回空 dict（不阻断管线）。"""
    if shutil.which("exiftool") is None:
        return {}

    def _run() -> dict[str, str]:
        import subprocess

        proc = subprocess.run(
            ["exiftool", "-j", "-n", "-"],
            input=data,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace")[:200])
        rows = json.loads(proc.stdout.decode(errors="replace") or "[]")
        meta = rows[0] if rows else {}
        return {k: str(v) for k, v in meta.items() if k in whitelist}

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("exif_extract_failed", error=str(e))
        return {}
