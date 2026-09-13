"""P2-QA-03 / P2-CORE-05 图片链路单测：mock 图文向量同空间、图片 MIME 嗅探。"""

from __future__ import annotations

import math

import pytest

from loomvec.core.ai.mock import MockBackend
from loomvec.core.config import AiSettings, ClipSettings
from loomvec.core.pipeline.mime import EXT_TO_MIME, is_image_mime, sniff_mime


def _backend() -> MockBackend:
    return MockBackend(AiSettings(mock=True, clip=ClipSettings(dim=128)))


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb or 1.0)


class TestMockClip:
    def test_text_and_image_share_space(self):
        """以文搜图的核心前提：文本查询与图片 caption 向量同空间可命中。"""
        b = _backend()
        q = b.clip_text(["日落 海边"])[0]
        img = b.clip_image([b"jpeg-bytes"], captions=["日落 海边 风景"])[0]
        other = b.clip_text(["财务 报表 数据"])[0]
        assert _cos(q, img) > 0.3
        assert _cos(q, other) < _cos(q, img)

    def test_image_without_caption_is_deterministic(self):
        b = _backend()
        v1 = b.clip_image([b"abc"], captions=[None])[0]
        v2 = b.clip_image([b"abc"], captions=[None])[0]
        assert v1 == v2

    def test_unit_norm(self):
        v = _backend().clip_text(["hello"])[0]
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6


class TestImageMime:
    def test_png_magic(self):
        data = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        mime, ext = sniff_mime(data, "photo.png")
        assert mime == "image/png" and ext == ".png"

    def test_jpeg_magic(self):
        data = b"\xff\xd8\xff" + b"0" * 32
        mime, ext = sniff_mime(data, "photo.jpg")
        assert mime == "image/jpeg" and ext == ".jpg"

    def test_ext_mismatch_rejected(self):
        data = b"\x89PNG\r\n\x1a\n" + b"0" * 32
        from loomvec.core.errors import ValidationError

        with pytest.raises(ValidationError):
            sniff_mime(data, "photo.jpg")

    def test_is_image_mime(self):
        assert is_image_mime("image/png")
        assert not is_image_mime("application/pdf")

    def test_image_exts_in_whitelist_map(self):
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
            assert ext in EXT_TO_MIME


class TestImageThumbnail:
    async def test_make_thumbnail(self):
        from io import BytesIO

        from PIL import Image

        from loomvec.core.imaging import make_thumbnail

        buf = BytesIO()
        Image.new("RGB", (100, 80), "blue").save(buf, format="PNG")
        out = await make_thumbnail(buf.getvalue(), max_width=48, max_height=48)
        assert out is not None
        payload, w, h = out
        assert w <= 48 and h <= 48 and payload[:4] == b"RIFF"  # webp
