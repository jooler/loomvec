"""P1/P2 管线步骤实现：parse → chunk → embed → index（编排见 runner.py）。

- 文档流（本模块）：MIME 嗅探 → MinerU/文本直读 → LLM 分片 → 文本嵌入 → 索引；
- 图片流（image_flow.py）：EXIF/缩略图/caption → 整图单单元 → clip 嵌入；
  run() 依 mime 分支委托，index 步骤两类型共用。

幂等约定（03 文档 §二.5）：
- chunk 按 (checksum, 解析器版本, 模型, prompt 版本, 分片策略, 抽取开关, 领域配置) 缓存键跳过；
- embed 按 embed_model_version 跳过，向量落派生 bucket 供 index 复用；
- index 重跑先清该资产旧向量再写入。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.constants import CHUNK_PRESETS
from loomvec.core.db.models import (
    Asset,
    AssetVersion,
    ChunkMethod,
    GraphStatus,
    SemanticUnit,
    Space,
    UnitType,
)
from loomvec.core.db.system_config import load_extraction_profiles
from loomvec.core.errors import ValidationError
from loomvec.core.graph.profiles import ExtractionProfile
from loomvec.core.mineru_slots import MineruBusyError
from loomvec.core.pipeline.base import (
    LAST_STEP,
    PIPELINE_STEPS,
    PipelineDeps,
    chunk_cache_key,
    failed_extraction_batches,
    resolve_chunk_strategy,
    resolve_extraction_profile,
    resolve_graph_extraction,
)
from loomvec.core.pipeline.chunking import (
    ChunkDraft,
    build_chunk_messages,
    detect_table_ranges,
    split_batches,
    strip_html,
    structural_fallback,
    validate_markers,
)
from loomvec.core.pipeline.extraction import (
    EntityMention,
    MergedExtraction,
    RelationMention,
    attribute_relations,
    build_extraction_messages,
    parse_merged,
    sanitize_extraction,
)
from loomvec.core.pipeline.graph_flow import GraphStep
from loomvec.core.pipeline.image_flow import chunk_image, embed_image, parse_image
from loomvec.core.pipeline.media_flow import chunk_media, embed_media, parse_media
from loomvec.core.pipeline.mime import (
    is_image_mime,
    is_media_mime,
    is_mineru_mime,
    sniff_mime,
)
from loomvec.core.pipeline.pages import build_line_page_map, draft_text, sanitize_text, unit_locator
from loomvec.core.pipeline.paper_meta import PDF_MIME, read_pdf_info
from loomvec.core.pipeline.parsers import parse_document
from loomvec.core.storage_keys import embed_vectors_key, parse_layout_key, parse_md_key

logger = structlog.get_logger("loomvec.pipeline")

__all__ = [
    "LAST_STEP",
    "PIPELINE_STEPS",
    "STEP_CLASSES",
    "ChunkStep",
    "EmbedStep",
    "GraphStep",
    "IndexStep",
    "ParseStep",
    "PipelineDeps",
    "chunk_cache_key",
]


class ParseStep:
    """P1-WRK-02 解析：MIME 嗅探 → MinerU HTTP / 文本直读 → Markdown + 版面坐标。"""

    name = "parse"
    _deps: PipelineDeps

    async def run(self, session: AsyncSession, asset: Asset, version: AssetVersion) -> dict:
        deps = self._deps
        parser_name = "plain_text"
        if version.inline_text is not None:
            markdown = sanitize_text(version.inline_text)
            content_list: list[dict[str, Any]] = []
            md_key = layout_key = None
            mime = asset.mime_type
        else:
            if not version.storage_key:
                raise ValidationError(
                    "资产既无 inline_text 也无 storage_key", asset_id=str(asset.id)
                )
            # 名额满时在下载原文件之前就退出：64 进程抢 1 个名额、30s 一轮重试，
            # 若每轮都先把原文件（可达数 MB）从对象存储拉一遍，纯属浪费 I/O。
            # 这里只按登记时的 mime_type 判断（嗅探前，略粗但足够省掉绝大多数无谓下载）；
            # 真正的占用判定仍在 MineruClient.parse 内的 slots.acquire()。
            if is_mineru_mime(asset.mime_type) and await deps.mineru.likely_busy():
                raise MineruBusyError("MinerU 解析名额已满（下载前探测）")
            data = await deps.storage.get_object(
                deps.settings.storage.bucket_raw, version.storage_key
            )
            mime, _ext = sniff_mime(data, asset.name)
            if mime != asset.mime_type:
                asset.mime_type = mime  # 嗅探结果回写（登记时可能仅凭扩展名）
            if is_image_mime(mime):
                return await parse_image(session, deps, asset, version, data, mime)
            if is_media_mime(mime):
                return await parse_media(session, deps, asset, version, data, mime)
            if mime == PDF_MIME:
                asset.asset_meta = {**asset.asset_meta, "pdf_info": read_pdf_info(data)}
            # 解析器链（03 文档 §一）：MIME → 第一个支持的解析器（MinerU → 纯文本兜底）
            doc = await parse_document(deps.mineru, asset.name, data, mime)
            markdown = sanitize_text(doc.markdown)
            content_list = doc.content_list
            parser_name = doc.parser_name

            checksum = hashlib.sha256(data).hexdigest()
            version.checksum = version.checksum or checksum
            asset.checksum = asset.checksum or checksum
            md_key = parse_md_key(asset.id, version.version)
            layout_key = parse_layout_key(asset.id, version.version)
            await deps.storage.put_object(
                deps.settings.storage.bucket_derived,
                md_key,
                markdown.encode(),
                content_type="text/markdown",
            )
            await deps.storage.put_object(
                deps.settings.storage.bucket_derived,
                layout_key,
                json.dumps(content_list, ensure_ascii=False).encode(),
                content_type="application/json",
            )

        page_count = max((int(b.get("page_idx", 0)) for b in content_list), default=-1) + 1 or 1
        version.version_meta = {
            **version.version_meta,
            "parse": {
                "parser": parser_name,
                "parser_version": deps.settings.pipeline.parser_version,
                "md_key": md_key,
                "layout_key": layout_key,
                "lines": markdown.count("\n") + 1,
                "page_count": page_count,
                "mime": mime,
            },
        }
        asset.asset_meta = {**asset.asset_meta, "page_count": page_count}
        await session.flush()
        return {"lines": markdown.count("\n") + 1, "page_count": page_count}


class ChunkStep:
    """P3-WRK-01 分片，按分片策略（本次运行覆盖 > Space 覆盖 > 全局 pipeline.chunk_strategy）
    二选一：

    - llm：每批一次 LLM 调用（按图谱抽取开关决定是否同时请求实体/关系，
      见 _resolve_graph_extraction）；
    - structural：按标题/段落/表格规则确定性分片，不调用 LLM，不产出实体关系
      （与图谱抽取开关互斥，不支持"规则分片+仍抽图谱"组合）。

    抽取领域配置（graph/profiles）同样按 本次运行覆盖 > Space.extraction_profile > 全局
    pipeline.extraction_profile 解析，决定抽取 prompt 的领域内容与兜底过滤规则。

    llm 策略下独立校验互不牵连（03 文档 §3.2）：
    - markers 有效、抽取无效（LLM 调用失败 / 结构无效）→ 正常分片入库，该批无实体关系，
      批数记入 version_meta 的 extraction_failed_batches（不改 graph_status）；
    - markers 失效、抽取有效 → 结构化兜底分片，抽取按兜底 chunk 行区间归属；
    - 抽取结果（全文行号锚定）落 version_meta['graph']['extraction']，
      由 GraphStep 消费（PG 主表→AGE→Milvus entities）。
    """

    name = "chunk"
    _deps: PipelineDeps

    @staticmethod
    async def _extract_batches(
        deps: PipelineDeps,
        lines: list[str],
        batches: list[tuple[int, int]],
        *,
        graph_on: bool,
        profile: ExtractionProfile,
        min_chars: int,
        max_chars: int,
    ) -> list[MergedExtraction | BaseException]:
        """各批的 LLM 分片（+抽取）调用，最多 `pipeline.llm_concurrency` 个并发。

        返回与 batches 等长、同序的结果；某批失败的位置是异常对象，
        不影响其它批，由调用方走结构化兜底。
        """
        sem = asyncio.Semaphore(max(1, deps.settings.pipeline.llm_concurrency))
        max_line_chars = deps.settings.pipeline.llm_batch_chars

        async def _one(batch_start: int, batch_end: int) -> MergedExtraction:
            async with sem:
                if graph_on:
                    messages = build_extraction_messages(
                        lines,
                        batch_start,
                        batch_end,
                        min_chars=min_chars,
                        max_chars=max_chars,
                        max_line_chars=max_line_chars,
                        profile=profile,
                    )
                else:
                    messages = build_chunk_messages(
                        lines,
                        batch_start,
                        batch_end,
                        min_chars=min_chars,
                        max_chars=max_chars,
                        max_line_chars=max_line_chars,
                    )
                raw = await deps.ai.complete_json(messages)
                return parse_merged(
                    raw, batch_start=batch_start, batch_end=batch_end, profile=profile
                )

        return await asyncio.gather(*(_one(s, e) for s, e in batches), return_exceptions=True)

    async def run(
        self,
        session: AsyncSession,
        asset: Asset,
        version: AssetVersion,
        *,
        chunk_strategy: str | None = None,
        graph_extraction: str | None = None,
        extraction_profile: str | None = None,
    ) -> dict:
        """chunk_strategy/graph_extraction/extraction_profile：本次运行覆盖值
        （llm/structural、on/off、领域配置 key），None = 按空间/全局解析；
        不落库，只影响这一次运行。"""
        deps = self._deps
        if is_image_mime(asset.mime_type):
            asset.graph_status = GraphStatus.SKIPPED
            return await chunk_image(session, asset, version)
        if is_media_mime(asset.mime_type):
            return await chunk_media(session, deps, asset, version)
        markdown = await self._load_markdown(deps, version)
        content_list = await self._load_content_list(deps, version)
        lines = markdown.split("\n")
        page_map = build_line_page_map(markdown, content_list)

        # P2-CORE-01 空间分片预设/策略覆盖全局参数（constants.CHUNK_PRESETS 等）
        space = (
            await session.execute(select(Space).where(Space.id == asset.space_id))
        ).scalar_one_or_none()
        preset = CHUNK_PRESETS.get(space.chunk_preset or "", None) if space else None
        min_chars = (preset or {}).get("chunk_min_chars", deps.settings.pipeline.chunk_min_chars)
        max_chars = (preset or {}).get("chunk_max_chars", deps.settings.pipeline.chunk_max_chars)
        strategy = self._resolve_chunk_strategy(deps, space, chunk_strategy)
        graph_on = self._resolve_graph_extraction(deps, space, graph_extraction)
        profile = resolve_extraction_profile(
            deps.settings, space, await load_extraction_profiles(session), extraction_profile
        )
        # 领域配置只在 LLM 分片且抽图谱时影响结果：只有这时才入缓存键、落 version_meta
        used_profile = profile if strategy == "llm" and graph_on else None

        key = chunk_cache_key(
            deps.settings,
            version.checksum,
            deps.settings.ai.llm.model,
            chunk_strategy=strategy,
            graph_extraction=graph_on,
            extraction_profile=used_profile,
        )
        existing_ids = await self._unit_ids(session, version.id)
        # 上次有批次抽取失败（如网关 503）的结果不当作可复用缓存，重跑才能补抽
        graph_meta_prev = version.version_meta.get("graph") or {}
        prev_failed = failed_extraction_batches(
            version.version_meta.get("chunk"),
            has_extraction=graph_meta_prev.get("extraction") is not None,
        )
        if version.chunk_cache_key == key and existing_ids and not prev_failed:
            return {"skipped": True, "reason": "chunk_cache_key 命中（幂等跳过）"}

        tables = detect_table_ranges(lines)
        batches = split_batches(lines, deps.settings.pipeline.llm_batch_chars)
        drafts: list[ChunkDraft] = []
        fallback_batches = 0
        # 抽图谱时 LLM 调用失败 / 抽取结构无效而没有产出实体的批次数（分片会走兜底、该批无实体）；
        # 落 version_meta 供 reextract_graph.py 识别「有失败批次、需重抽」的资产
        extraction_failed_batches = 0
        extraction = self._new_extraction_acc()

        # LLM 调用按 pipeline.llm_concurrency 并发（批与批互不依赖），结果按批次顺序消费：
        # 分片、实体、关系的拼接顺序与串行时完全一致
        llm_results: list[MergedExtraction | BaseException] = []
        if strategy != "structural":
            llm_results = await self._extract_batches(
                deps,
                lines,
                batches,
                graph_on=graph_on,
                profile=profile,
                min_chars=min_chars,
                max_chars=max_chars,
            )

        for batch_index, (batch_start, batch_end) in enumerate(batches):
            batch_tables = [
                t for t in tables if t.start_line >= batch_start and t.end_line <= batch_end
            ]
            merged: MergedExtraction | None = None
            if strategy == "structural":
                # 规则分片：不调用 LLM，按标题/段落/表格确定性分片（无实体关系产出，
                # 与 graph_on 无关——二者互斥，不支持"规则分片+仍抽图谱"组合）
                fallback_batches += 1
                drafts.extend(
                    structural_fallback(
                        lines,
                        batch_start=batch_start,
                        batch_end=batch_end,
                        tables=batch_tables,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                )
                continue
            outcome = llm_results[batch_index]
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome  # 取消等非业务异常不能当成"这批失败"吞掉
                # LLM 超时/整体解析失败 → 整批结构化兜底
                logger.warning(
                    "chunk_llm_batch_fallback", batch=(batch_start, batch_end), error=str(outcome)
                )
                fallback_batches += 1
                extraction_failed_batches += int(graph_on)
                drafts.extend(
                    structural_fallback(
                        lines,
                        batch_start=batch_start,
                        batch_end=batch_end,
                        tables=batch_tables,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                )
                continue
            merged = outcome

            # ---- markers 路：有效则按 marker 分片，失效则兜底（抽取不受牵连）----
            if merged.markers_ok:
                drafts.extend(
                    validate_markers(
                        merged.markers,
                        lines,
                        batch_start=batch_start,
                        batch_end=batch_end,
                        tables=batch_tables,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                )
            else:
                fallback_batches += 1
                logger.warning("chunk_markers_invalid_fallback", batch=(batch_start, batch_end))
                drafts.extend(
                    structural_fallback(
                        lines,
                        batch_start=batch_start,
                        batch_end=batch_end,
                        tables=batch_tables,
                        min_chars=min_chars,
                        max_chars=max_chars,
                    )
                )
            # ---- 抽取路：独立累积 ----
            if merged.extraction_ok:
                extraction["entities"].extend(merged.entities)
                extraction["relations"].extend(merged.relations)
            else:
                extraction_failed_batches += int(graph_on)

        # 表格独立成片兜底：未被任何批次覆盖的表格区间（理论不发生）
        covered = {(d.start_line, d.end_line) for d in drafts if d.is_table}
        for t in tables:
            if (t.start_line, t.end_line) not in covered:
                drafts.append(
                    ChunkDraft(
                        t.start_line,
                        t.end_line,
                        unit_type="table",
                        method=ChunkMethod.STRUCTURAL_FALLBACK,
                    )
                )

        await self._delete_units(session, version.id)  # 重跑清理旧单元

        units: list[SemanticUnit] = []
        for order, d in enumerate(drafts):
            units.append(
                SemanticUnit(
                    asset_id=asset.id,
                    version_id=version.id,
                    space_id=asset.space_id,
                    tenant_id=asset.tenant_id,
                    unit_type=UnitType.TABLE if d.is_table else UnitType.TEXT,
                    title=d.title,
                    content=draft_text(lines, d),  # 表格草稿的行区间即 HTML 块
                    keywords=d.keywords,
                    locator=unit_locator(d, page_map),
                    chunk_method=d.method,
                    order_index=order,
                    char_count=len(draft_text(lines, d)),
                )
            )
            session.add(units[-1])
        await session.flush()  # 服务端 uuidv7() 主键回填

        # 父子块：首片为父块，其余片指向它
        key_to_id = {d.key: units[i].id for i, d in enumerate(drafts)}
        for i, d in enumerate(drafts):
            if d.parent_key is not None:
                units[i].parent_id = key_to_id.get(d.parent_key)

        # ---- 抽取结果归属 chunk + 落 version_meta（GraphStep 消费）----
        entity_payload, relation_payload = self._extraction_payload(
            extraction, drafts, drop_isolated=deps.settings.graph.drop_isolated_entities
        )
        extraction_ok = extraction["entities"] is not None
        graph_enabled = deps.settings.graph.enabled and graph_on
        if strategy == "structural":
            # 规则分片模式二选一：无论 graph_on 取值，本就没有调用 LLM、没有抽取结果
            asset.graph_status = GraphStatus.SKIPPED
            graph_meta = {"status": "skipped", "reason": "chunk_strategy=structural"}
        elif not graph_enabled:
            asset.graph_status = GraphStatus.SKIPPED
            graph_meta = {"status": "skipped"}
        elif extraction_ok:
            asset.graph_status = GraphStatus.PENDING
            graph_meta = {
                "status": "pending",
                "prompt_version": deps.settings.pipeline.prompt_version,
                **self._profile_meta(used_profile),
                "extraction_failed_batches": extraction_failed_batches,
                "extraction": {
                    "entities": entity_payload,
                    "relations": relation_payload,
                },
            }
        else:
            asset.graph_status = GraphStatus.PENDING_RETRY
            graph_meta = {"status": "pending_retry", "reason": "extraction_invalid"}

        version.chunk_cache_key = key
        version.version_meta = {
            **version.version_meta,
            "chunk": {
                "units": len(units),
                "fallback_batches": fallback_batches,
                "extraction_failed_batches": extraction_failed_batches,
                "prompt_version": deps.settings.pipeline.prompt_version,
                **self._profile_meta(used_profile),
                "llm_model": deps.settings.ai.llm.model
                or ("mock" if deps.settings.ai.mock else "unknown"),
            },
            "graph": graph_meta,
        }
        asset.asset_meta = {
            **asset.asset_meta,
            "chunk_method": "structural_fallback" if fallback_batches else "llm_markers",
        }
        await session.flush()
        return {
            "units": len(units),
            "fallback_batches": fallback_batches,
            "graph_status": asset.graph_status.value if asset.graph_status else None,
        }

    @staticmethod
    def _resolve_chunk_strategy(
        deps: PipelineDeps, space: Space | None, override: str | None = None
    ) -> str:
        return resolve_chunk_strategy(deps.settings, space, override)

    @staticmethod
    def _resolve_graph_extraction(
        deps: PipelineDeps, space: Space | None, override: str | None = None
    ) -> bool:
        return resolve_graph_extraction(deps.settings, space, override)

    @staticmethod
    def _profile_meta(profile: ExtractionProfile | None) -> dict[str, str | None]:
        """version_meta 中的领域配置记录（未抽图谱时为 None）；reextract 脚本据指纹判断要否重抽。"""
        return {
            "extraction_profile": profile.key if profile else None,
            "extraction_profile_fingerprint": profile.fingerprint if profile else None,
        }

    @staticmethod
    def _new_extraction_acc() -> dict[str, list | None]:
        return {"entities": [], "relations": []}

    @staticmethod
    def _extraction_payload(
        acc: dict[str, list | None], drafts: list[ChunkDraft], *, drop_isolated: bool = False
    ) -> tuple[list[dict], list[dict]]:
        """把累积的提及对象序列化为 version_meta 结构；关系归属 chunk_key。"""
        if acc["entities"] is None:
            return [], []
        entities: list[EntityMention] = acc["entities"]
        relations: list[RelationMention] = acc["relations"] or []
        entities, relations = sanitize_extraction(entities, relations, drop_isolated=drop_isolated)
        attribute_relations(relations, drafts)
        return (
            [
                {
                    "name": e.name,
                    "type": e.type,
                    "description": e.description,
                    "lines": e.lines,
                    "aliases": e.aliases,
                }
                for e in entities
            ],
            [
                {
                    "head": r.head,
                    "tail": r.tail,
                    "type": r.type,
                    "evidence_lines": r.evidence_lines,
                    "chunk_key": r.chunk_key,
                }
                for r in relations
            ],
        )

    @staticmethod
    async def _unit_ids(session: AsyncSession, version_id) -> set:
        return set(
            (
                await session.execute(
                    select(SemanticUnit.id).where(SemanticUnit.version_id == version_id)
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def _delete_units(session: AsyncSession, version_id) -> None:
        for u in (
            (
                await session.execute(
                    select(SemanticUnit).where(SemanticUnit.version_id == version_id)
                )
            )
            .scalars()
            .all()
        ):
            await session.delete(u)

    async def _load_markdown(self, deps: PipelineDeps, version: AssetVersion) -> str:
        parse_meta = version.version_meta.get("parse") or {}
        md_key = parse_meta.get("md_key")
        if md_key:
            data = await deps.storage.get_object(deps.settings.storage.bucket_derived, md_key)
            return sanitize_text(data.decode())
        if version.inline_text is not None:
            return sanitize_text(version.inline_text)
        raise ValidationError("缺少解析产物（请先跑 parse 步骤）", version_id=str(version.id))

    async def _load_content_list(self, deps: PipelineDeps, version: AssetVersion) -> list[dict]:
        parse_meta = version.version_meta.get("parse") or {}
        layout_key = parse_meta.get("layout_key")
        if not layout_key:
            return []
        data = await deps.storage.get_object(deps.settings.storage.bucket_derived, layout_key)
        return list(json.loads(data.decode()))


class EmbedStep:
    """P1-WRK-04 嵌入：批量云端向量化，向量缓存到派生 bucket 供索引步骤复用。"""

    name = "embed"
    _deps: PipelineDeps

    async def run(self, session: AsyncSession, asset: Asset, version: AssetVersion) -> dict:
        deps = self._deps
        if is_image_mime(asset.mime_type):
            return await embed_image(session, deps, asset, version)
        if is_media_mime(asset.mime_type):
            return await embed_media(session, deps, asset, version)
        model_version = (
            "mock" if deps.settings.ai.mock else (deps.settings.ai.embedding.model or "unknown")
        )
        units = (
            (
                await session.execute(
                    select(SemanticUnit).where(
                        SemanticUnit.version_id == version.id,
                        SemanticUnit.embed_model_version.is_(None)
                        | (SemanticUnit.embed_model_version != model_version),
                    )
                )
            )
            .scalars()
            .all()
        )

        batch_size = max(1, deps.settings.pipeline.embed_batch_size)
        max_chars = deps.settings.pipeline.embed_text_max_chars
        vectors: dict[str, list[float]] = {}
        for i in range(0, len(units), batch_size):
            batch = units[i : i + batch_size]
            vecs = await deps.ai.embed([self._embed_text(u, max_chars) for u in batch])
            for u, v in zip(batch, vecs, strict=True):
                vectors[str(u.id)] = v
                u.embed_model_version = model_version
            await session.flush()

        vectors_key = embed_vectors_key(asset.id, version.version)
        await deps.storage.put_object(
            deps.settings.storage.bucket_derived,
            vectors_key,
            json.dumps(vectors).encode(),
            content_type="application/json",
        )
        version.version_meta = {
            **version.version_meta,
            "embed": {
                "model_version": model_version,
                "vectors_key": vectors_key,
                "count": len(vectors),
                "channel": "text_dense",
            },
        }
        await session.flush()
        return {"embedded": len(vectors), "model_version": model_version}

    @staticmethod
    def _embed_text(unit: SemanticUnit, max_chars: int) -> str:
        body = strip_html(unit.content) if unit.unit_type == UnitType.TABLE else unit.content
        title = f"{unit.title}\n" if unit.title else ""
        return (title + body)[:max_chars]


class IndexStep:
    """P1-WRK-04 索引：semantic_units 集合写入（dense + BM25 Function + 标量字段）。"""

    name = "index"
    _deps: PipelineDeps

    async def run(self, session: AsyncSession, asset: Asset, version: AssetVersion) -> dict:
        deps = self._deps
        units = (
            (
                await session.execute(
                    select(SemanticUnit).where(
                        SemanticUnit.version_id == version.id,
                        SemanticUnit.embed_model_version.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not units:
            raise ValidationError("无可索引单元（请先跑 embed 步骤）", asset_id=str(asset.id))

        embed_meta = version.version_meta.get("embed") or {}
        vectors_key = embed_meta.get("vectors_key")
        if not vectors_key:
            raise ValidationError("缺少向量缓存（请先跑 embed 步骤）", asset_id=str(asset.id))
        cache = json.loads(
            (
                await deps.storage.get_object(deps.settings.storage.bucket_derived, vectors_key)
            ).decode()
        )
        # 媒体缓存为嵌套布局 {"units", "frames"}（frames → 场景单元 clip_dense）
        if "units" in cache and isinstance(cache.get("units"), dict):
            unit_vectors: dict[str, list[float]] = cache["units"]
            frame_vectors: dict[str, list[float]] = cache.get("frames") or {}
        else:
            unit_vectors, frame_vectors = cache, {}

        max_chars = deps.settings.pipeline.index_text_max_chars
        # 图片单元走 clip_dense 通道；两通道字段在集合中均非空，缺侧补零向量
        is_image = is_image_mime(asset.mime_type)
        zero_clip = [0.0] * deps.milvus.clip_dim
        zero_text = [0.0] * deps.milvus.text_dim
        rows: list[dict[str, Any]] = []
        for u in units:
            vec = unit_vectors.get(str(u.id))
            if not vec:
                continue
            text = u.content if u.unit_type == UnitType.TEXT else strip_html(u.content)
            frame_vec = frame_vectors.get((u.locator or {}).get("frame") or "")
            rows.append(
                {
                    "id": str(u.id),
                    "space_id": str(u.space_id),
                    "tenant_id": str(u.tenant_id) if u.tenant_id else "",
                    "asset_id": str(u.asset_id),
                    "unit_type": u.unit_type.value,
                    "model_version": u.embed_model_version,
                    "text": text[:max_chars],
                    "text_dense": zero_text if is_image else vec,
                    "clip_dense": vec if is_image else (frame_vec or zero_clip),
                }
            )
        await deps.milvus.async_ensure_collection()
        await deps.milvus.async_delete_asset_units(asset.id)  # 重跑：先清旧向量
        await deps.milvus.async_upsert_units(rows)
        return {"indexed": len(rows), "channel": "clip_dense" if is_image else "text_dense"}


STEP_CLASSES: dict[str, type] = {
    s.name: s for s in (ParseStep, ChunkStep, GraphStep, EmbedStep, IndexStep)
}
