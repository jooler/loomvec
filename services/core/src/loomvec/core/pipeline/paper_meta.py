"""论文 PDF 自动命名：{年份}-{期刊}-{标题}-{第一作者}.pdf（缺失字段省略该段）。

元数据来源按成本递增逐级尝试：
1. 正文开头 / PDF 元数据中的 DOI → Crossref（零 token，最准）；
2. PDF 文档信息字典（Title/Author，仅作缺字段兜底）；
3. LLM 读 Markdown 开头 ~1500 字（单次小调用）。

由 PipelineRunner 在 chunk 步骤后调用（资产仍为 processing，前端轮询可见新名）；
任何失败只记日志，不影响资产管线。幂等：asset_meta['paper'].checksum 命中即跳过。
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from loomvec.core.db.models import Asset, AssetVersion
from loomvec.core.pipeline.base import PipelineDeps

logger = structlog.get_logger("loomvec.pipeline.paper")

_TASK_TAG = "paper_meta"
PDF_MIME = "application/pdf"
CROSSREF_URL = "https://api.crossref.org/works/"

_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
_DOI_TRAILING = ".,;:)]}"
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACES_RE = re.compile(r"\s+")
_NORM_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_ACRONYM_RE = re.compile(r"\(([A-Z][A-Za-z\-]{1,15})\)\s*$")
_JUNK_PDF_TITLE_RE = re.compile(r"(microsoft word|untitled|\.docx?$|\.pdf$|\.tex$)", re.IGNORECASE)

HEAD_CHARS_DOI = 3000
HEAD_CHARS_LLM = 1500
MAX_TITLE_CHARS = 80
MAX_JOURNAL_CHARS = 40


@dataclass
class PaperMeta:
    year: int | None = None
    journal: str | None = None
    title: str | None = None
    first_author: str | None = None
    authors: list[str] = field(default_factory=list)
    doi: str | None = None
    source: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.title) and bool(self.year or self.journal)


# ---------- 来源 1：DOI → Crossref ----------


def find_doi(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        m = _DOI_RE.search(text)
        if m:
            return m.group(1).rstrip(_DOI_TRAILING)
    return None


def _norm(text: str) -> str:
    return _NORM_RE.sub("", text.lower())


def title_in_text(title: str, text: str) -> bool:
    """DOI 可能属于参考文献：要求 Crossref 标题开头确实出现在正文开头。"""
    probe = _norm(title)[:30]
    return bool(probe) and probe in _norm(text)


def _first(values: Any) -> str | None:
    if isinstance(values, list) and values:
        return str(values[0]).strip() or None
    return None


def _crossref_author_label(entry: dict[str, Any]) -> str | None:
    """展示用：Family, Given；无 given 则 family / name。"""
    family = str(entry.get("family") or "").strip()
    given = str(entry.get("given") or "").strip()
    if family and given:
        return f"{family}, {given}"
    return family or given or (str(entry.get("name") or "").strip() or None)


def parse_crossref(message: dict[str, Any], doi: str) -> PaperMeta:
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    journal = _first(message.get("short-container-title"))
    if not journal:
        full = _first(message.get("container-title"))
        acronym = _ACRONYM_RE.search(full or "")
        journal = acronym.group(1) if acronym else full
    raw_authors = message.get("author") or []
    authors: list[str] = []
    first_author = None
    for i, entry in enumerate(raw_authors):
        if not isinstance(entry, dict):
            continue
        label = _crossref_author_label(entry)
        if label:
            authors.append(label)
        if i == 0:
            # first_author 保留「姓」供文件名；authors 用展示标签 Family, Given
            first_author = entry.get("family") or entry.get("name") or label
    return PaperMeta(
        year=year,
        journal=journal,
        title=_first(message.get("title")),
        first_author=first_author,
        authors=authors,
        doi=doi,
        source="crossref",
    )


async def fetch_crossref(
    doi: str, *, mailto: str = "", timeout: float = 10.0, client: httpx.AsyncClient | None = None
) -> PaperMeta | None:
    agent = "LoomVec/0.1" + (f" (mailto:{mailto})" if mailto else "")
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(CROSSREF_URL + doi, headers={"User-Agent": agent})
        if resp.status_code != 200:
            return None
        return parse_crossref(resp.json().get("message") or {}, doi)
    finally:
        if own:
            await client.aclose()


# ---------- 来源 2：PDF 文档信息字典（ParseStep 已持有原始字节时调用） ----------


def read_pdf_info(data: bytes) -> dict[str, str]:
    try:
        from pypdf import PdfReader

        info = PdfReader(io.BytesIO(data)).metadata or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for key in ("/Title", "/Author", "/Subject", "/Keywords", "/doi"):
        value = info.get(key)
        if value:
            out[key.lstrip("/").lower()] = str(value).strip()[:500]
    return out


def meta_from_pdf_info(info: dict[str, str]) -> PaperMeta:
    title = info.get("title")
    if title and (len(title) < 10 or _JUNK_PDF_TITLE_RE.search(title)):
        title = None
    raw = (info.get("author") or "").strip()
    authors = [a.strip() for a in re.split(r"[;|]", raw) if a.strip()] if raw else []
    first_author = None
    if authors:
        # 文件名仍用「姓」：取第一作者逗号前段
        first_author = authors[0].split(",")[0].strip() or authors[0]
    return PaperMeta(title=title, first_author=first_author, authors=authors, source="pdf_info")


# ---------- 来源 3：LLM 读开头 ----------


def build_paper_meta_messages(head: str) -> list[dict[str, str]]:
    system = (
        "你是学术文献元数据抽取器。根据论文开头文本输出 JSON（仅输出 JSON）：\n"
        '{"is_paper": true, "year": 2021, "journal": "期刊或会议名（有通用缩写用缩写）", '
        '"title": "论文标题", "first_author": "第一作者姓（中文用全名）", '
        '"authors": ["Family, Given", "…"]}\n'
        '规则：非学术论文（合同、报告、手册等）返回 {"is_paper": false}；'
        "authors 按文中出现顺序列出（最多 8 人，超出省略）；"
        "文中找不到的字段填 null，不要猜测。"
    )
    user = f"[LOOMVEC_TASK={_TASK_TAG}]\n{head}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_llm_meta(data: dict[str, Any]) -> PaperMeta | None:
    if not data.get("is_paper"):
        return None
    year = data.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    if year is not None and not 1900 <= year <= 2100:
        year = None

    def text(key: str) -> str | None:
        value = data.get(key)
        if not value:
            return None
        return str(value).strip() or None

    authors: list[str] = []
    raw_authors = data.get("authors")
    if isinstance(raw_authors, list):
        for item in raw_authors[:8]:
            if item and str(item).strip():
                authors.append(str(item).strip())
    first_author = text("first_author")
    if not first_author and authors:
        first_author = authors[0].split(",")[0].strip() or authors[0]

    return PaperMeta(
        year=year,
        journal=text("journal"),
        title=text("title"),
        first_author=first_author,
        authors=authors,
        source="llm",
    )


# ---------- 命名 ----------


def _clean(value: str, limit: int) -> str:
    value = _SPACES_RE.sub(" ", _ILLEGAL_CHARS_RE.sub(" ", value)).strip(" .-")
    if len(value) > limit:
        value = value[:limit].rsplit(" ", 1)[0] if " " in value[:limit] else value[:limit]
    return value.strip(" .-")


def build_paper_filename(meta: PaperMeta, ext: str = ".pdf") -> str:
    parts = [
        str(meta.year) if meta.year else "",
        _clean(meta.journal or "", MAX_JOURNAL_CHARS),
        _clean(meta.title or "", MAX_TITLE_CHARS),
        _clean(meta.first_author or "", MAX_JOURNAL_CHARS),
    ]
    return "-".join(p for p in parts if p) + ext


def _fill_missing(meta: PaperMeta, fallback: PaperMeta) -> PaperMeta:
    for key in ("year", "journal", "title", "first_author", "doi"):
        if not getattr(meta, key) and getattr(fallback, key):
            setattr(meta, key, getattr(fallback, key))
    if not meta.authors and fallback.authors:
        meta.authors = list(fallback.authors)
    if not meta.first_author and meta.authors:
        meta.first_author = meta.authors[0].split(",")[0].strip() or meta.authors[0]
    return meta


async def extract_paper_meta(
    deps: PipelineDeps, markdown_head: str, pdf_info: dict[str, str]
) -> PaperMeta | None:
    cfg = deps.settings.pipeline
    pdf_meta = meta_from_pdf_info(pdf_info)

    doi = find_doi(pdf_info.get("doi"), pdf_info.get("subject"), markdown_head[:HEAD_CHARS_DOI])
    if doi and cfg.crossref_enabled:
        try:
            meta = await fetch_crossref(doi, mailto=cfg.crossref_mailto)
        except Exception as e:
            logger.warning("paper_crossref_failed", doi=doi, error=str(e))
            meta = None
        if meta and meta.title and title_in_text(meta.title, markdown_head):
            return _fill_missing(meta, pdf_meta)

    try:
        data = await deps.ai.complete_json(
            build_paper_meta_messages(markdown_head[:HEAD_CHARS_LLM]),
            temperature=0,
            max_tokens=400,
        )
    except Exception as e:
        logger.warning("paper_llm_failed", error=str(e))
        return None
    meta = parse_llm_meta(data)
    if meta is None:
        return None
    meta.doi = doi
    return _fill_missing(meta, pdf_meta)


# ---------- 管线入口 ----------


async def _load_markdown_head(deps: PipelineDeps, version: AssetVersion) -> str:
    md_key = (version.version_meta.get("parse") or {}).get("md_key")
    if not md_key:
        return ""
    data = await deps.storage.get_object(deps.settings.storage.bucket_derived, md_key)
    return data.decode(errors="ignore")[:HEAD_CHARS_DOI]


def _uploaded_name(version: AssetVersion) -> str | None:
    return version.storage_key.rsplit("/", 1)[-1] if version.storage_key else None


async def _load(session, asset_id: uuid.UUID) -> tuple[Asset | None, AssetVersion | None]:
    asset = (
        await session.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if asset is None:
        return None, None
    version = (
        await session.execute(
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset_id)
            .order_by(AssetVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return asset, version


def _renamable(asset: Asset, version: AssetVersion) -> bool:
    """用户手动改过名则不动：当前名须仍是上传原名或上次自动命名结果。"""
    paper = asset.asset_meta.get("paper") or {}
    return asset.name in {_uploaded_name(version), paper.get("auto_name")}


async def auto_rename_asset(
    deps: PipelineDeps, asset_id: uuid.UUID, *, force: bool = False
) -> str | None:
    """返回新名称；不适用 / 失败返回 None（从不抛异常）。

    force=True：忽略 checksum 幂等与「用户手动改名则跳过」——供右键「重新自动命名」。
    """
    if not deps.settings.pipeline.auto_rename_papers:
        return None
    try:
        async with deps.session_factory() as session:
            asset, version = await _load(session, asset_id)
            if asset is None or version is None or asset.mime_type != PDF_MIME:
                return None
            paper = asset.asset_meta.get("paper") or {}
            if not force and (
                paper.get("checksum") == version.checksum or not _renamable(asset, version)
            ):
                return None
            pdf_info = asset.asset_meta.get("pdf_info") or {}
            head = await _load_markdown_head(deps, version)

        # 网络调用期间不持有会话；写回前重新校验（期间用户可能手动改名）
        meta = await extract_paper_meta(deps, head, pdf_info) if head else None

        async with deps.session_factory() as session:
            asset, version = await _load(session, asset_id)
            if asset is None or version is None:
                return None
            if not force and not _renamable(asset, version):
                return None
            original = paper.get("original_name") or _uploaded_name(version) or asset.name
            record: dict[str, Any] = {"checksum": version.checksum, "original_name": original}
            new_name = None
            if meta is not None and meta.usable:
                new_name = build_paper_filename(meta, asset.ext or ".pdf")
                record.update(asdict(meta), auto_name=new_name)
                asset.name = new_name
            else:
                record["is_paper"] = False
            asset.asset_meta = {**asset.asset_meta, "paper": record}
            await session.commit()
        if new_name:
            logger.info("paper_auto_renamed", asset_id=str(asset_id), name=new_name, force=force)
        return new_name
    except Exception as e:
        logger.warning("paper_auto_rename_failed", asset_id=str(asset_id), error=str(e))
        return None
