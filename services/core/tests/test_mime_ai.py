"""P1-WRK-02 MIME 嗅探与 P1-CORE-02 AI 网关 mock 模式单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from loomvec.core.ai import AiGateway
from loomvec.core.config import AiSettings, EmbeddingSettings, LlmSettings
from loomvec.core.errors import ValidationError
from loomvec.core.pipeline.mime import is_mineru_mime, sniff_mime

# ---------------------------------------------------------------------------
# MIME 嗅探
# ---------------------------------------------------------------------------


def test_sniff_pdf_by_magic():
    mime, ext = sniff_mime(b"%PDF-1.7 ...", "报告.pdf")
    assert (mime, ext) == ("application/pdf", ".pdf")


def test_sniff_docx_zip_with_ext():
    data = b"PK\x03\x04" + b"\x00" * 16
    mime, ext = sniff_mime(data, "文档.docx")
    assert is_mineru_mime(mime) and ext == ".docx"


def test_sniff_zip_without_office_ext_rejected():
    with pytest.raises(ValidationError):
        sniff_mime(b"PK\x03\x04" + b"\x00" * 16, "archive.zip")


def test_sniff_markdown_text():
    (mime, _ext) = sniff_mime("# 标题\n正文".encode(), "notes.md")
    assert mime == "text/markdown"


def test_sniff_disallowed_ext():
    # P3 起音视频（mp4/mov/webm/mkv/mp3/wav/m4a/flac）纳入白名单， avi 仍被拒绝
    with pytest.raises(ValidationError):
        sniff_mime(b"anything", "movie.avi")


def test_sniff_mime_and_ext_mismatch_rejected():
    # PDF 魔数但声明 .docx：扩展名校验先行拒绝
    with pytest.raises(ValidationError):
        sniff_mime(b"%PDF-1.7", "伪装.docx")


# ---------------------------------------------------------------------------
# AI 网关 mock 模式
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gateway() -> AiGateway:
    return AiGateway(
        AiSettings(
            mock=True,
            embedding=EmbeddingSettings(dim=64, batch_size=8),
            llm=LlmSettings(json_retries=1),
        )
    )


def test_mock_embed_deterministic_and_normalized(mock_gateway: AiGateway):
    v1 = asyncio.run(mock_gateway.embed(["同一段文本"]))
    v2 = asyncio.run(mock_gateway.embed(["同一段文本"]))
    assert v1 == v2
    assert len(v1[0]) == 64
    norm = sum(x * x for x in v1[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    empty = asyncio.run(mock_gateway.embed([]))
    assert empty == []


def test_mock_rerank_orders_by_relevance(mock_gateway: AiGateway):
    ranked = asyncio.run(mock_gateway.rerank("季度营收", ["营收分析报告", "天气预报"]))
    assert ranked[0]["index"] == 0
    assert ranked[0]["relevance_score"] >= ranked[1]["relevance_score"]


def test_mock_complete_chunk_markers(mock_gateway: AiGateway):
    out = asyncio.run(
        mock_gateway.complete(
            [
                {
                    "role": "user",
                    "content": (
                        "[LOOMVEC_TASK=chunk_markers]\n[DOC_START_LINE=1]\n[DOC_END_LINE=45]"
                    ),
                }
            ],
            json_mode=True,
        )
    )
    assert "chunk_markers" in out
