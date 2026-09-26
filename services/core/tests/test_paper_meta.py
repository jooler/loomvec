"""论文自动命名：DOI 识别、Crossref 解析、LLM 兜底与文件名规则。"""

from __future__ import annotations

import io
import uuid
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
    assert meta.authors == ["He, Kaiming", "Zhang, Xiangyu"]
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
    assert meta.authors == ["Smith, J.", "Doe, A."]
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


async def test_extract_uses_doi_from_pdf_subject_when_markdown_lacks_doi(monkeypatch):
    """MinerU 丢失页眉 DOI 时，Subject 中的 DOI 仍可走 Crossref（右键回源 pdf_info 兜底依赖此路径）。"""
    title = "CAREPath: semantic context-aware reasoning paths with mechanism-augmented embeddings for drug repurposing"
    head = f"# {title}\nHaerin Song\n## Abstract\nBiomedical knowledge graphs."
    subject = f"DOI: 10.1093/bib/bbag472, Briefings in Bioinformatics, 2026. Abstract: …"

    async def fake_fetch(doi, **kwargs):
        assert doi == "10.1093/bib/bbag472"
        return parse_crossref(
            {
                "title": [title],
                "container-title": ["Briefings in Bioinformatics"],
                "short-container-title": ["Brief Bioinform"],
                "published-print": {"date-parts": [[2026, 9]]},
                "author": [{"given": "Haerin", "family": "Song"}],
            },
            doi,
        )

    monkeypatch.setattr(paper_meta, "fetch_crossref", fake_fetch)
    ai = _FakeAi({"is_paper": False})
    meta = await extract_paper_meta(
        _deps(ai), head, {"title": title, "subject": subject}
    )
    assert meta is not None and meta.source == "crossref" and ai.calls == 0
    assert meta.usable and meta.doi == "10.1093/bib/bbag472"


async def test_load_pdf_info_from_raw_reads_subject_doi():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata(
        {
            "/Title": "CAREPath: semantic context-aware reasoning paths",
            "/Subject": "DOI: 10.1093/bib/bbag472, Briefings in Bioinformatics, 2026.",
        }
    )
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    class _Storage:
        async def get_object(self, bucket, key):
            assert bucket == "raw" and key == "k.pdf"
            return pdf_bytes

    deps = SimpleNamespace(
        storage=_Storage(),
        settings=SimpleNamespace(storage=SimpleNamespace(bucket_raw="raw")),
    )
    info = await paper_meta._load_pdf_info_from_raw(deps, "k.pdf")
    assert info is not None
    assert find_doi(info.get("doi"), info.get("subject")) == "10.1093/bib/bbag472"
    assert await paper_meta._load_pdf_info_from_raw(deps, None) is None


class _FakeSession:
    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _force_rename_fixture(title: str, subject: str, *, cached_pdf_info: dict | None = None):
    """构造 force 自动命名：缓存缺 Subject、raw PDF 含 Subject DOI。"""
    asset_id = uuid.uuid4()
    head = f"# {title}\nHaerin Song\n## Abstract\nBiomedical knowledge graphs."
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": title[:60], "/Subject": subject})
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    version = SimpleNamespace(
        checksum="sha-v1",
        storage_key="papers/upload.pdf",
        version_meta={"parse": {"md_key": "derived/x.md"}},
    )
    asset = SimpleNamespace(
        name="upload.pdf",
        ext=".pdf",
        mime_type=paper_meta.PDF_MIME,
        asset_meta={"pdf_info": dict(cached_pdf_info or {})},
    )

    raw_gets: list[str] = []

    class _Storage:
        async def get_object(self, bucket, key):
            if bucket == "raw":
                raw_gets.append(key)
                return pdf_bytes
            if bucket == "derived":
                return head.encode()
            raise AssertionError(f"unexpected get_object({bucket!r}, {key!r})")

    ai = _FakeAi({"is_paper": False})
    deps = SimpleNamespace(
        settings=SimpleNamespace(
            pipeline=PipelineSettings(),
            storage=SimpleNamespace(bucket_raw="raw", bucket_derived="derived"),
        ),
        ai=ai,
        storage=_Storage(),
        session_factory=_FakeSession,
    )
    return asset_id, asset, version, deps, ai, raw_gets


async def test_auto_rename_force_reloads_pdf_info_before_extract(monkeypatch):
    """force：缓存无 Subject 时先回源，再 Crossref，零 LLM，并回写 pdf_info。"""
    title = (
        "CAREPath: semantic context-aware reasoning paths with mechanism-augmented "
        "embeddings for drug repurposing"
    )
    subject = "DOI: 10.1093/bib/bbag472, Briefings in Bioinformatics, 2026."
    asset_id, asset, version, deps, ai, raw_gets = _force_rename_fixture(title, subject)

    async def fake_load(session, aid):
        assert aid == asset_id
        return asset, version

    async def fake_fetch(doi, **kwargs):
        assert doi == "10.1093/bib/bbag472"
        return parse_crossref(
            {
                "title": [title],
                "container-title": ["Briefings in Bioinformatics"],
                "short-container-title": ["Brief Bioinform"],
                "published-print": {"date-parts": [[2026, 9]]},
                "author": [{"given": "Haerin", "family": "Song"}],
            },
            doi,
        )

    monkeypatch.setattr(paper_meta, "_load", fake_load)
    monkeypatch.setattr(paper_meta, "fetch_crossref", fake_fetch)

    new_name = await paper_meta.auto_rename_asset(deps, asset_id, force=True)
    assert new_name is not None and new_name.startswith("2026-Brief Bioinform-")
    assert ai.calls == 0
    assert raw_gets == ["papers/upload.pdf"]
    assert "subject" in asset.asset_meta["pdf_info"]
    assert find_doi(asset.asset_meta["pdf_info"].get("subject")) == "10.1093/bib/bbag472"
    assert asset.asset_meta["paper"]["source"] == "crossref"


async def test_auto_rename_force_skips_reload_when_cached_pdf_info_unchanged(monkeypatch):
    """force：回源结果与缓存相同则不二次写 pdf_info，仍只抽一次。"""
    title = "Deep Residual Learning for Image Recognition"
    subject = "DOI: 10.1109/CVPR.2016.90"
    # 先读出与 PdfWriter 一致的字段，用作「已缓存」
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": title, "/Subject": subject})
    buf = io.BytesIO()
    writer.write(buf)
    cached = read_pdf_info(buf.getvalue())

    asset_id, asset, version, deps, ai, raw_gets = _force_rename_fixture(
        title, subject, cached_pdf_info=cached
    )
    # fixture 又写了一份 PDF；用同一 cached 覆盖 raw 读出的期望键集合
    asset.asset_meta["pdf_info"] = dict(cached)

    extract_calls: list[dict] = []

    async def fake_load(session, aid):
        return asset, version

    async def fake_extract(deps, head, pdf_info):
        extract_calls.append(dict(pdf_info))
        return PaperMeta(year=2016, journal="CVPR", title=title, first_author="He", source="crossref")

    async def fake_reload(deps, storage_key):
        raw_gets.append(storage_key or "")
        return dict(cached)

    monkeypatch.setattr(paper_meta, "_load", fake_load)
    monkeypatch.setattr(paper_meta, "extract_paper_meta", fake_extract)
    monkeypatch.setattr(paper_meta, "_load_pdf_info_from_raw", fake_reload)

    new_name = await paper_meta.auto_rename_asset(deps, asset_id, force=True)
    assert new_name == "2016-CVPR-Deep Residual Learning for Image Recognition-He.pdf"
    assert len(extract_calls) == 1
    assert raw_gets == ["papers/upload.pdf"]
    # fresh == cached → 不回写 pdf_info 键（仍保留原缓存对象内容）
    assert asset.asset_meta["pdf_info"] == cached


async def test_auto_rename_aborts_when_version_changes_mid_flight(monkeypatch):
    title = "Deep Residual Learning for Image Recognition"
    asset_id, asset, version, deps, ai, _ = _force_rename_fixture(title, "DOI: 10.1109/CVPR.2016.90")
    v2 = SimpleNamespace(
        checksum="sha-v2",
        storage_key="papers/replaced.pdf",
        version_meta=version.version_meta,
    )
    loads = {"n": 0}

    async def fake_load(session, aid):
        loads["n"] += 1
        return asset, version if loads["n"] == 1 else v2

    async def ok_extract(deps, head, pdf_info):
        return PaperMeta(year=2016, journal="CVPR", title=title, first_author="He", source="crossref")

    async def no_reload(*args, **kwargs):
        return None

    monkeypatch.setattr(paper_meta, "_load", fake_load)
    monkeypatch.setattr(paper_meta, "extract_paper_meta", ok_extract)
    monkeypatch.setattr(paper_meta, "_load_pdf_info_from_raw", no_reload)

    assert await paper_meta.auto_rename_asset(deps, asset_id, force=True) is None
    assert asset.name == "upload.pdf"
