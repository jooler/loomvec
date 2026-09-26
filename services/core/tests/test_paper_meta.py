"""论文自动命名：DOI 识别、Crossref 解析、LLM 兜底与文件名规则。"""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx
from pypdf import PdfWriter

from loomvec.core.config import PipelineSettings
from loomvec.core.pipeline import paper_meta
from loomvec.core.pipeline.paper_meta import (
    PaperMeta,
    build_paper_filename,
    extract_paper_meta,
    fetch_crossref,
    find_doi,
    meta_from_pdf_info,
    parse_crossref,
    parse_llm_meta,
    read_pdf_info,
    title_in_text,
)

HEAD = (
    "# Deep Residual Learning for Image Recognition\n"
    "Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun\n"
    "DOI: 10.1109/CVPR.2016.90.\n"
    "Abstract: Deeper neural networks are more difficult to train."
)

CROSSREF_MESSAGE = {
    "title": ["Deep Residual Learning for Image Recognition"],
    "container-title": ["2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)"],
    "short-container-title": [],
    "published-print": {"date-parts": [[2016, 6]]},
    "author": [{"given": "Kaiming", "family": "He"}, {"given": "Xiangyu", "family": "Zhang"}],
}


def test_find_doi_strips_trailing_punctuation():
    assert find_doi(None, "", HEAD) == "10.1109/CVPR.2016.90"
    assert find_doi("see (doi 10.1038/s41467-021-12345-6)") == "10.1038/s41467-021-12345-6"
    assert find_doi("no identifier here") is None


def test_parse_crossref_prefers_conference_acronym():
    meta = parse_crossref(CROSSREF_MESSAGE, "10.1109/CVPR.2016.90")
    assert (meta.year, meta.journal, meta.first_author) == (2016, "CVPR", "He")
    assert meta.source == "crossref"


def test_parse_crossref_prefers_short_container_title():
    msg = {
        **CROSSREF_MESSAGE,
        "container-title": ["Nature Communications"],
        "short-container-title": ["Nat Commun"],
    }
    assert parse_crossref(msg, "x").journal == "Nat Commun"


def test_title_in_text_rejects_reference_doi():
    assert title_in_text("Deep Residual Learning for Image Recognition", HEAD)
    assert not title_in_text("ImageNet Classification with Deep CNNs", HEAD)


def test_build_filename_order_sanitize_and_missing_fields():
    meta = PaperMeta(year=2016, journal="CVPR", title="Deep: Residual/Learning?", first_author="He")
    assert build_paper_filename(meta) == "2016-CVPR-Deep Residual Learning-He.pdf"
    assert build_paper_filename(PaperMeta(year=2020, title="基于图神经网络的推荐")) == (
        "2020-基于图神经网络的推荐.pdf"
    )


def test_build_filename_truncates_long_title_on_word_boundary():
    meta = PaperMeta(year=2021, title="word " * 40)
    name = build_paper_filename(meta)
    title = name.removeprefix("2021-").removesuffix(".pdf")
    assert len(title) <= paper_meta.MAX_TITLE_CHARS and not title.endswith(" ")


def test_parse_llm_meta():
    assert parse_llm_meta({"is_paper": False}) is None
    meta = parse_llm_meta(
        {
            "is_paper": True,
            "year": "2020",
            "journal": "计算机学报",
            "title": "标题",
            "first_author": None,
        }
    )
    assert (meta.year, meta.journal, meta.first_author, meta.source) == (
        2020,
        "计算机学报",
        None,
        "llm",
    )
    assert parse_llm_meta({"is_paper": True, "year": 99999, "title": "t"}).year is None


def test_pdf_info_roundtrip_and_junk_title_filter():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "Microsoft Word - draft.docx", "/Author": "Smith, J.; Doe, A."})
    buf = io.BytesIO()
    writer.write(buf)
    info = read_pdf_info(buf.getvalue())
    meta = meta_from_pdf_info(info)
    assert meta.title is None and meta.first_author == "Smith"
    assert read_pdf_info(b"not a pdf") == {}


async def test_fetch_crossref_via_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/works/10.1109/CVPR.2016.90")
        return httpx.Response(200, json={"message": CROSSREF_MESSAGE})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        meta = await fetch_crossref("10.1109/CVPR.2016.90", client=client)
    assert meta is not None and meta.title == "Deep Residual Learning for Image Recognition"


class _FakeAi:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls = 0

    async def complete_json(self, messages, **kwargs):
        self.calls += 1
        return self.reply


def _deps(ai: _FakeAi, **pipeline) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(pipeline=PipelineSettings(**pipeline)), ai=ai)


async def test_extract_uses_crossref_without_llm(monkeypatch):
    async def fake_fetch(doi, **kwargs):
        return parse_crossref(CROSSREF_MESSAGE, doi)

    monkeypatch.setattr(paper_meta, "fetch_crossref", fake_fetch)
    ai = _FakeAi({"is_paper": False})
    meta = await extract_paper_meta(_deps(ai), HEAD, {})
    assert meta.source == "crossref" and ai.calls == 0
    assert build_paper_filename(meta) == (
        "2016-CVPR-Deep Residual Learning for Image Recognition-He.pdf"
    )


async def test_extract_falls_back_to_llm_when_crossref_disabled():
    ai = _FakeAi({"is_paper": True, "year": 2016, "journal": "CVPR", "title": "Deep Residual"})
    meta = await extract_paper_meta(
        _deps(ai, crossref_enabled=False), HEAD, {"author": "Kaiming He"}
    )
    assert meta.source == "llm" and ai.calls == 1
    assert meta.doi == "10.1109/CVPR.2016.90"
    assert meta.first_author == "Kaiming He"  # LLM 缺字段由 PDF 元数据补齐


async def test_extract_returns_none_for_non_paper():
    ai = _FakeAi({"is_paper": False})
    assert await extract_paper_meta(_deps(ai, crossref_enabled=False), "合同正文", {}) is None
