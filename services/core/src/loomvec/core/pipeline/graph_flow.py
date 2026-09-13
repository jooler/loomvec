"""P3-WRK-01/P3-CORE-03 图谱写入：实体解析（阶段一内联链接）→ 单事务图写入。

写入顺序（03 文档 §3.4）：PG entity 主表 → AGE（同事务）→ Milvus entities 向量；
- 同事务：AGE 与 PG 同库，EntityGraphService 在调用方 session 的事务内执行
  Cypher——原子生效或整体回滚，不留半图；
- 幂等：entity_key 确定性（hash(space, norm, type)），重跑先删本资产旧边再写；
- 并发：per-space advisory lock 序列化同空间的图写；
- Milvus 落后可容忍（最终一致，可由 entity 主表全量重建）；
- 失败降级：图写入异常 → asset.graph_status=pending_retry，不阻断资产管线
  （embed/index 照常，答案只少图谱证据）。

媒体资产（视频/音频）无 version_meta 抽取缓存，GraphStep 现场以单元文本为
伪行做抽取调用（markers 忽略，场景即分片边界）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.constants import EVENT_GRAPH_READY, GRAPH_NAME
from loomvec.core.db.models import Asset, AssetVersion, Entity, GraphStatus, SemanticUnit
from loomvec.core.events import publish_event
from loomvec.core.graph.age import AgeStore, GraphEdge, GraphNode
from loomvec.core.graph.ontology import (
    entity_embed_text,
    entity_key,
    normalize_entity_name,
)
from loomvec.core.pipeline.base import PipelineDeps
from loomvec.core.pipeline.mime import is_media_mime

logger = structlog.get_logger("loomvec.pipeline.graph")

AGE = AgeStore(GRAPH_NAME)


@dataclass
class _Cluster:
    """同 (归一化名, 类型) 的提及聚合：单批内同名多次出现只产一个实体。"""

    name: str
    norm: str
    type: str
    description: str | None = None
    aliases: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    key: str = ""
    resolved_id: UUID | None = None  # 链接到的 entity 主表 id（新建者写回）
    is_new: bool = False


class EntityGraphService:
    """资产级图写入（阶段一内联链接 + 单事务 PG→AGE 写入 + Milvus 跟进）。"""

    def __init__(self, session: AsyncSession, deps: PipelineDeps) -> None:
        self._session = session
        self._deps = deps

    async def write_asset_graph(
        self,
        asset: Asset,
        version: AssetVersion,
        units: list[SemanticUnit],
        extraction: dict[str, list],
    ) -> dict:
        """extraction = {"entities": [...], "relations": [...]}（chunk 步骤落盘结构）。"""
        session, deps = self._session, self._deps
        space_id = str(asset.space_id)

        # 并发控制：同空间图写串行（advisory lock 随事务释放）
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"loomvec:graph:{space_id}"}
        )

        clusters, key_to_cluster = self._cluster_entities(extraction.get("entities") or [], asset)
        if not clusters:
            await self._finish(asset, version, written_nodes=0, written_edges=0)
            return {"entities": 0, "relations": 0}

        # ---- 阶段一内联链接：精确（主表 norm+type）→ 向量（Milvus top-1）→ 新建 ----
        await self._link_exact(clusters, asset)
        await self._link_vector(clusters, asset)

        # ---- 关系解析与归并（head/tail 必须可解析；按端点+类型聚合溯源）----
        unit_by_order = {u.order_index: u for u in units}
        edges = self._merge_relations(
            extraction.get("relations") or [], key_to_cluster, unit_by_order, asset
        )

        # ---- AGE：删旧边 → 幂等写点/边 → 悬空点清理（同一 PG 事务）----
        touched = await AGE.delete_asset_edges(session, space_id, asset.id)
        nodes = [
            GraphNode(
                key=c.key,
                space_id=space_id,
                tenant_id=str(asset.tenant_id) if asset.tenant_id else None,
                name=c.name,
                type=c.type,
                description=c.description,
            )
            for c in clusters
        ]
        await AGE.upsert_nodes(session, nodes)
        await AGE.write_edges(session, edges)
        current_keys = {c.key for c in clusters}
        await AGE.delete_orphan_nodes(
            session, space_id, [k for k in touched if k not in current_keys]
        )

        # ---- PG entity 主表（事实源）：新建 + 描述/别名回填 ----
        await self._upsert_master_rows(clusters, asset)

        # ---- Milvus entities：仅新建实体（向量可由主表重建，最终一致）----
        new_clusters = [c for c in clusters if c.is_new]
        await self._index_new_entities(new_clusters, asset)

        await self._finish(asset, version, len(clusters), len(edges))
        await publish_event(
            deps.redis,
            {
                "type": EVENT_GRAPH_READY,
                "asset_id": str(asset.id),
                "space_id": space_id,
                "entities": len(clusters),
                "relations": len(edges),
            },
        )
        return {"entities": len(clusters), "relations": len(edges)}

    # ---------- 聚合与链接 ----------

    def _cluster_entities(
        self, mentions: list[dict[str, Any]], asset: Asset
    ) -> tuple[list[_Cluster], dict[str, _Cluster]]:
        """提及 → (归一化名, 类型) 聚类；别名收集原文变体。"""
        by_cluster: dict[tuple[str, str], _Cluster] = {}
        cap = self._deps.settings.graph.max_entities_per_asset
        for m in mentions:
            name = str(m.get("name") or "").strip()
            if not name:
                continue
            etype = str(m.get("type") or "other")
            norm = normalize_entity_name(name)
            if not norm:
                continue
            ck = (norm, etype)
            cluster = by_cluster.get(ck)
            if cluster is None:
                if len(by_cluster) >= cap:
                    continue
                description = str(m.get("description") or "") or None
                cluster = _Cluster(name=name, norm=norm, type=etype, description=description)
                cluster.key = entity_key(str(asset.space_id), name, etype)
                by_cluster[ck] = cluster
            else:
                if not cluster.description and m.get("description"):
                    cluster.description = str(m["description"])
                if name != cluster.name and name not in cluster.aliases:
                    cluster.aliases.append(name)
            for ln in m.get("lines") or []:
                if ln not in cluster.lines:
                    cluster.lines.append(ln)
        clusters = sorted(by_cluster.values(), key=lambda c: c.norm)
        return clusters, {(c.norm, c.type): c for c in clusters}

    async def _link_exact(self, clusters: list[_Cluster], asset: Asset) -> None:
        rows = (
            await self._session.execute(
                select(Entity).where(
                    Entity.space_id == asset.space_id,
                    Entity.deleted_at.is_(None),
                    Entity.merged_into.is_(None),
                    Entity.name_norm.in_([c.norm for c in clusters]),
                )
            )
        ).scalars()
        by_norm_type: dict[tuple[str, str], Entity] = {}
        for e in rows:
            by_norm_type.setdefault((e.name_norm, e.type), e)
        for c in clusters:
            existing = by_norm_type.get((c.norm, c.type))
            if existing is not None:
                c.key = existing.entity_key
                c.resolved_id = existing.id

    async def _link_vector(self, clusters: list[_Cluster], asset: Asset) -> None:
        """未精确命中的簇 → Milvus entities top-1 ≥ 阈值判同（跨写法链接）。"""
        pending = [c for c in clusters if c.resolved_id is None]
        if not pending:
            return
        deps = self._deps
        try:
            texts = [entity_embed_text(c.name, c.type, c.description) for c in pending]
            vectors = await deps.ai.embed(texts)
        except Exception as e:
            logger.warning("entity_embed_failed", error=str(e))
            return  # 全部按新建处理（重跑可再链接，主表唯一约束防重复）
        threshold = deps.settings.graph.entity_link_threshold
        for c, vec in zip(pending, vectors, strict=True):
            try:
                hits = await deps.milvus.async_search_entities(
                    query_vector=vec, space_ids=[asset.space_id], top_k=1
                )
            except Exception as e:
                logger.debug("entity_vector_link_unavailable", error=str(e))
                continue
            if hits and hits[0][1] >= threshold:
                winner_key, score = hits[0]
                existing = (
                    await self._session.execute(
                        select(Entity).where(
                            Entity.entity_key == winner_key, Entity.deleted_at.is_(None)
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    logger.info(
                        "entity_linked_by_vector",
                        name=c.name,
                        to=existing.name,
                        score=round(score, 4),
                    )
                    c.key = existing.entity_key
                    c.resolved_id = existing.id
                    if not existing.aliases and c.name != existing.name:
                        existing.aliases = [c.name]

    def _merge_relations(
        self,
        relations: list[dict[str, Any]],
        key_to_cluster: dict[tuple[str, str], _Cluster],
        unit_by_order: dict[int, SemanticUnit],
        asset: Asset,
    ) -> list[GraphEdge]:
        """关系聚合：端点解析 → (head, tail, type) 归并 → chunk_ids 溯源。"""
        cap = self._deps.settings.graph.max_relations_per_asset
        mentions = self._mention_index(relations)
        merged: dict[tuple[str, str, str], GraphEdge] = {}
        for r in relations:
            head_c = self._resolve_endpoint(r.get("head"), mentions, key_to_cluster)
            tail_c = self._resolve_endpoint(r.get("tail"), mentions, key_to_cluster)
            if head_c is None or tail_c is None or head_c.key == tail_c.key:
                continue
            rel_type = str(r.get("type") or "REL").upper()
            ck = (head_c.key, tail_c.key, rel_type)
            edge = merged.get(ck)
            if edge is None:
                if len(merged) >= cap:
                    continue
                edge = GraphEdge(
                    head_key=head_c.key,
                    tail_key=tail_c.key,
                    type=rel_type,
                    weight=0.0,
                    asset_ids=[str(asset.id)],
                )
                merged[ck] = edge
            edge.weight += 1.0
            chunk_key = r.get("chunk_key")
            unit = unit_by_order.get(int(chunk_key)) if chunk_key is not None else None
            if unit is not None and str(unit.id) not in edge.chunk_ids:
                edge.chunk_ids.append(str(unit.id))
        return list(merged.values())

    @staticmethod
    def _mention_index(relations: list[dict[str, Any]]) -> dict[str, str]:
        """原文写法 → 归一化写法（端点大小写/变体与实体表目不完全一致时的兜底映射）。"""
        index: dict[str, str] = {}
        for r in relations:
            for side in ("head", "tail"):
                name = str(r.get(side) or "").strip()
                if name:
                    index.setdefault(name.lower(), normalize_entity_name(name))
        return index

    def _resolve_endpoint(
        self,
        name: Any,
        mentions: dict[str, str],
        key_to_cluster: dict[tuple[str, str], _Cluster],
    ) -> _Cluster | None:
        text_name = str(name or "").strip()
        if not text_name:
            return None
        norm = normalize_entity_name(text_name)
        candidates = [c for c in key_to_cluster if c[0] == norm]
        if candidates:
            return key_to_cluster[candidates[0]]
        alt_norm = mentions.get(text_name.lower())
        if alt_norm:
            candidates = [c for c in key_to_cluster if c[0] == alt_norm]
            if candidates:
                return key_to_cluster[candidates[0]]
        return None

    # ---------- 主表与向量 ----------

    async def _upsert_master_rows(self, clusters: list[_Cluster], asset: Asset) -> None:
        session = self._session
        keys = [c.key for c in clusters]
        existing_rows = (
            await session.execute(
                select(Entity).where(Entity.entity_key.in_(keys), Entity.deleted_at.is_(None))
            )
        ).scalars()
        existing = {e.entity_key: e for e in existing_rows}
        for c in clusters:
            row = existing.get(c.key)
            if row is None:
                row = Entity(
                    tenant_id=asset.tenant_id,
                    space_id=asset.space_id,
                    entity_key=c.key,
                    name=c.name,
                    name_norm=c.norm,
                    type=c.type,
                    description=c.description,
                    aliases=c.aliases,
                )
                session.add(row)
                c.is_new = True
                await session.flush()
                c.resolved_id = row.id
            else:
                c.resolved_id = row.id
                if not row.description and c.description:
                    row.description = c.description
                for alias in c.aliases:
                    if alias not in (row.aliases or []) and alias != row.name:
                        row.aliases = [*row.aliases, alias]
        await session.flush()

    async def _index_new_entities(self, clusters: list[_Cluster], asset: Asset) -> None:
        if not clusters:
            return
        deps = self._deps
        try:
            texts = [entity_embed_text(c.name, c.type, c.description) for c in clusters]
            vectors = await deps.ai.embed(texts)
            rows = [
                {
                    "id": c.key,
                    "space_id": str(asset.space_id),
                    "tenant_id": str(asset.tenant_id) if asset.tenant_id else "",
                    "name": c.name,
                    "entity_type": c.type,
                    "vector": vec,
                }
                for c, vec in zip(clusters, vectors, strict=True)
            ]
            await deps.milvus.async_ensure_entities_collection()
            await deps.milvus.async_upsert_entities(rows)
        except Exception as e:  # 向量索引失败不阻断（可重建）
            logger.warning("entity_vector_index_failed", error=str(e))

    async def _finish(
        self, asset: Asset, version: AssetVersion, written_nodes: int, written_edges: int
    ) -> None:
        asset.graph_status = GraphStatus.READY
        graph_meta = version.version_meta.get("graph") or {}
        version.version_meta = {
            **version.version_meta,
            "graph": {
                **graph_meta,
                "status": "ready",
                "entities": written_nodes,
                "relations": written_edges,
            },
        }
        await self._session.flush()


# ---------------------------------------------------------------------------
# GraphStep：管线注册步骤（parse→chunk→graph→embed→index）
# ---------------------------------------------------------------------------


class GraphStep:
    """P3-WRK-01 图谱写入步骤：消费 chunk 步骤的抽取缓存，媒体资产现场抽取。

    失败降级：异常 → graph_status=pending_retry（独立会话回写），资产管线继续。
    """

    name = "graph"
    _deps: PipelineDeps

    async def run(self, session: AsyncSession, asset: Asset, version: AssetVersion) -> dict:
        deps = self._deps
        if not deps.settings.graph.enabled:
            asset.graph_status = GraphStatus.SKIPPED
            await session.flush()
            return {"skipped": True, "reason": "graph_disabled"}

        units = (
            (
                await session.execute(
                    select(SemanticUnit)
                    .where(SemanticUnit.version_id == version.id)
                    .order_by(SemanticUnit.order_index)
                )
            )
            .scalars()
            .all()
        )
        if is_media_mime(asset.mime_type):
            extraction = await self._media_extraction(asset, version, units)
        else:
            extraction = (version.version_meta.get("graph") or {}).get("extraction")
        if not extraction:
            asset.graph_status = GraphStatus.PENDING_RETRY
            await session.flush()
            return {"skipped": True, "reason": "no_extraction"}

        try:
            service = EntityGraphService(session, deps)
            return await service.write_asset_graph(asset, version, list(units), extraction)
        except Exception as e:
            # 事务可能已被语句失败污染：回滚后独立会话降级标记
            await session.rollback()
            logger.warning("graph_write_degraded", asset_id=str(asset.id), error=str(e))
            await self._mark_pending_retry(asset.id)
            return {"degraded": True, "reason": str(e)[:200]}

    async def _media_extraction(
        self, asset: Asset, version: AssetVersion, units: list[SemanticUnit]
    ) -> dict[str, list]:
        """媒体单元 → 伪行文本抽取（每单元一行；markers 忽略，场景即边界）。"""
        deps = self._deps
        if not units:
            return {}
        entities: list[dict] = []
        relations: list[dict] = []
        # 伪行坐标系：L{i+1} ↔ units[i]；按批字符上限分组调用
        batch_cap = max(1, deps.settings.pipeline.llm_batch_chars)
        batch: list[tuple[int, SemanticUnit]] = []
        batch_chars = 0

        async def _flush() -> None:
            nonlocal batch, batch_chars
            if not batch:
                return
            first, last = batch[0][0] + 1, batch[-1][0] + 1
            pseudo = [f"L{i + 1}: {u.title or ''} {u.content}".strip() for i, u in batch]
            merged = await self._extract_lines(pseudo, first, last)
            if merged is not None:
                entities.extend(merged["entities"])
                relations.extend(merged["relations"])
            batch, batch_chars = [], 0

        for i, u in enumerate(units):
            size = len(u.content) + 64
            if batch and batch_chars + size > batch_cap:
                await _flush()
            batch.append((i, u))
            batch_chars += size
        await _flush()

        if not entities:
            return {}
        return {"entities": entities, "relations": relations}

    async def _extract_lines(self, lines: list[str], start: int, end: int) -> dict | None:
        from loomvec.core.pipeline.extraction import build_extraction_messages, parse_merged

        messages = build_extraction_messages(
            [""] * (start - 1) + lines, start, end, min_chars=1, max_chars=10**6
        )
        try:
            raw = await self._deps.ai.complete_json(messages)
            merged = parse_merged(raw, batch_start=start, batch_end=end)
        except Exception as e:
            logger.warning("media_extraction_failed", error=str(e))
            return None
        if not merged.extraction_ok:
            return None
        # 关系归属：伪行 L{i+1} ↔ 单元 order_index=i
        relation_payloads = []
        for r in merged.relations:
            anchor = r.evidence_lines[0] if r.evidence_lines else None
            relation_payloads.append(
                {
                    "head": r.head,
                    "tail": r.tail,
                    "type": r.type,
                    "evidence_lines": r.evidence_lines,
                    "chunk_key": (anchor - 1) if anchor is not None else None,
                }
            )
        return {
            "entities": [
                {
                    "name": e.name,
                    "type": e.type,
                    "description": e.description,
                    "lines": e.lines,
                }
                for e in merged.entities
            ],
            "relations": relation_payloads,
        }

    async def _mark_pending_retry(self, asset_id: UUID) -> None:
        from loomvec.core.constants import EVENT_GRAPH_PENDING_RETRY

        session: AsyncSession = self._deps.session_factory()
        try:
            asset = (
                await session.execute(select(Asset).where(Asset.id == asset_id))
            ).scalar_one_or_none()
            if asset is not None:
                asset.graph_status = GraphStatus.PENDING_RETRY
                await session.commit()
            await publish_event(
                self._deps.redis,
                {"type": EVENT_GRAPH_PENDING_RETRY, "asset_id": str(asset_id)},
            )
        except Exception:
            await session.rollback()
            logger.error("graph_pending_retry_mark_failed", asset_id=str(asset_id))
        finally:
            await session.close()
