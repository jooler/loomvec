"""P1-CORE-03 / P2-CORE-02 / P3-CORE-04 混合检索器。

召回通道：
- L1 dense + L2 sparse（Milvus 端 RRFRanker 融合，返回 fused distance）；
- clip_dense 以文搜图（P2，未配置自动降级）；
- L3 图谱路径（P3）：实体链接 → 1~2 跳扩展 → chunk 回收（GraphRetriever）；
- L4 community_summary（P3）：全局总结类问题通道；
融合：L1/L2 分数保留（Milvus RRF），clip/L3/L4/邻接补全各为一独立排名表，
应用侧按 1/(rrf_k + rank) 增量累加，统一进 cross-encoder 重排。
降级：图谱关闭/故障、L4 集合缺失 → 自动退化为 L1+L2（±clip），不影响可用性。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.ai import AiGateway
from loomvec.core.config import SearchSettings
from loomvec.core.retrieval.graph_retrieval import GraphRetriever
from loomvec.core.retrieval.highlight import make_highlight
from loomvec.core.retrieval.store import MilvusStore

logger = structlog.get_logger("loomvec.retrieval")


@dataclass(frozen=True)
class SemanticHit:
    """检索命中（契约：unit + asset + score + locator + graph_evidence，03 文档 §四）。"""

    unit_id: uuid.UUID
    asset_id: uuid.UUID
    asset_name: str
    space_id: uuid.UUID
    unit_type: str
    title: str | None
    text: str
    score: float
    rank_scores: dict[str, float] = field(default_factory=dict)  # rrf/rerank/clip/graph/community
    locator: dict[str, Any] = field(default_factory=dict)  # pages/bbox/time_start/...
    highlight: str | None = None  # 带 <em> 的摘要片段
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)  # 实体→关系→实体路径


def _in_expr(field: str, values: list) -> str:
    return f"{field} in [{', '.join(f'"{v}"' for v in values)}]"


class Retriever:
    """混合检索：L1+L2 文本路 → RRF；clip / L3 图谱 / L4 摘要通道并行并入。"""

    def __init__(
        self,
        milvus: MilvusStore,
        ai: AiGateway,
        settings: SearchSettings,
        graph: GraphRetriever | None = None,
    ) -> None:
        self._milvus = milvus
        self._ai = ai
        self._settings = settings
        self._graph = graph

    def _base_expr(
        self,
        *,
        space_ids: list[uuid.UUID],
        tenant_id: uuid.UUID | None,
        asset_ids: list[uuid.UUID] | None,
        unit_types: list[str] | None,
    ) -> str:
        """租户 × 空间 × 类型过滤（越权收敛的最后一道闸）。"""
        if not space_ids:
            return 'space_id in ["__none__"]'  # 无可见空间：恒空表达式
        expr = _in_expr("space_id", [str(s) for s in space_ids])
        if tenant_id is not None:
            expr += f' and tenant_id == "{tenant_id}"'
        if asset_ids:
            expr += " and " + _in_expr("asset_id", [str(a) for a in asset_ids])
        if unit_types:
            expr += " and " + _in_expr("unit_type", unit_types)
        return expr

    async def search(
        self,
        *,
        space_ids: list[uuid.UUID],
        tenant_id: uuid.UUID | None = None,
        query: str,
        top_k: int | None = None,
        asset_ids: list[uuid.UUID] | None = None,
        unit_types: list[str] | None = None,
        rerank: bool | None = None,
        image_search: bool | None = None,
        use_graph: bool | None = None,
        session: AsyncSession | None = None,
    ) -> list[SemanticHit]:
        settings = self._settings
        top_k = min(top_k or settings.default_top_k, settings.max_top_k)
        use_rerank = settings.rerank_enabled if rerank is None else rerank
        limit = settings.rerank_candidates if use_rerank else max(top_k, settings.dense_top_k)

        expr = self._base_expr(
            space_ids=space_ids,
            tenant_id=tenant_id,
            asset_ids=asset_ids,
            unit_types=unit_types,
        )

        want_image = image_search if image_search is not None else settings.image_search_enabled
        want_image = (
            want_image
            and self._ai.clip_available()
            and (unit_types is None or "image" in unit_types)
        )
        want_text = unit_types is None or set(unit_types) - {"image"}

        rows: list[dict[str, Any]] = []
        qvec: list[float] | None = None
        if want_text:
            qvec = (await self._ai.embed([query]))[0]
            rows = await asyncio.to_thread(
                self._milvus.guarded,
                lambda: self._milvus.hybrid_search(
                    query_vector=qvec,
                    query_text=query,
                    expr=expr,
                    limit=limit,
                    dense_top_k=settings.dense_top_k,
                    sparse_top_k=settings.sparse_top_k,
                    rrf_k=settings.rrf_k,
                ),
            )
        clip_rows: list[dict[str, Any]] = []
        if want_image:
            clip_qvec = (await self._ai.clip_embed_texts([query]))[0]
            clip_rows = await self._milvus.async_clip_search(
                query_vector=clip_qvec, expr=expr, limit=limit, top_k=settings.dense_top_k
            )

        # ---- P3：L3 图谱路径 + L4 社区摘要（失败降级为 L1+L2）----
        graph_rows, graph_paths = [], {}
        if self._want_graph(use_graph) and qvec is not None and self._graph and session:
            try:
                recall = await self._graph.recall(
                    session,
                    space_ids=space_ids,
                    query_vector=qvec,
                    tenant_id=tenant_id,
                    unit_types=unit_types,
                )
                graph_rows, graph_paths = recall.rows, recall.paths
            except Exception as e:  # 图谱故障不影响基础检索
                logger.info("graph_recall_degraded", error=str(e))
        comm_rows: list[dict[str, Any]] = []
        if self._want_graph(use_graph) and qvec is not None and self._want_l4(unit_types):
            try:
                comm_rows = await self._milvus.async_community_search(
                    query_vector=qvec, expr=expr, limit=max(limit // 2, 5), top_k=5
                )
            except Exception:
                comm_rows = []

        # ---- RRF 融合：L1/L2 分数保留 + 各通道独立排名表增量累加 ----
        by_id: dict[str, dict[str, Any]] = {}
        fused: dict[str, float] = {}
        for r in rows:  # L1/L2（Milvus 端 RRF 融合分）
            by_id[r["id"]] = r
            fused[r["id"]] = fused.get(r["id"], 0.0) + float(r.get("distance", 0.0))
        channel_ids: dict[str, set[str]] = {"clip": set()}
        for rank, r in enumerate(clip_rows, start=1):
            channel_ids["clip"].add(r["id"])
            by_id.setdefault(r["id"], r)
            fused[r["id"]] = fused.get(r["id"], 0.0) + 1.0 / (settings.rrf_k + rank)
        for name, ch_rows in (("graph", graph_rows), ("community", comm_rows)):
            channel_ids[name] = set()
            for rank, r in enumerate(ch_rows, start=1):
                channel_ids[name].add(r["id"])
                by_id.setdefault(r["id"], r)
                fused[r["id"]] = fused.get(r["id"], 0.0) + 1.0 / (settings.rrf_k + rank)

        # ---- 邻接补全：top-N 命中的 1 跳邻居 chunk 并入候选池（重排前）----
        if (
            self._want_graph(use_graph)
            and self._graph
            and self._graph.adjacency_enabled
            and session
            and by_id
        ):
            seeds = [
                r["id"]
                for r in sorted(by_id.values(), key=lambda r: -fused.get(r["id"], 0.0))[
                    : self._graph.adjacency_top_k
                ]
            ]
            try:
                neighbors = await self._graph.adjacency_neighbors(
                    session, space_ids=space_ids, seed_unit_ids=seeds
                )
            except Exception:
                neighbors = {}
            extra_ids = {c for cs in neighbors.values() for c in cs} - set(by_id)
            if extra_ids:
                extra_rows = await self._graph.fetch_rows_for_adjacency(
                    space_ids=space_ids,
                    tenant_id=tenant_id,
                    unit_ids=sorted(extra_ids),
                    unit_types=unit_types,
                )
                for rank, r in enumerate(extra_rows, start=1):
                    by_id.setdefault(r["id"], r)
                    channel_ids.setdefault("adjacency", set()).add(r["id"])
                    fused[r["id"]] = fused.get(r["id"], 0.0) + 1.0 / (settings.rrf_k + rank)

        results = sorted(by_id.values(), key=lambda r: -fused.get(r["id"], 0.0))
        if not results:
            return []

        # ---- rerank 精排（供方未配置时保持 RRF 序，见 AiGateway.rerank）----
        rerank_scores: dict[str, float] = {}
        if use_rerank:
            ranked = await self._ai.rerank(query, [r["entity"]["text"] for r in results])
            if ranked:
                order = [results[item["index"]]["id"] for item in ranked]
                rerank_scores = {
                    results[item["index"]]["id"]: item["relevance_score"] for item in ranked
                }
                results = sorted(results, key=lambda r: order.index(r["id"]))

        hits: list[SemanticHit] = []
        for r in results[:top_k]:
            entity = r["entity"]
            unit_id = r["id"]
            base = rerank_scores.get(unit_id) or fused.get(unit_id, 0.0)
            rank_scores = {
                "rrf": fused.get(unit_id, 0.0),
                **({"rerank": rerank_scores[unit_id]} if unit_id in rerank_scores else {}),
            }
            for name in ("clip", "graph", "community", "adjacency"):
                if unit_id in channel_ids.get(name, set()):
                    rank_scores[name] = fused.get(unit_id, 0.0)
            hits.append(
                SemanticHit(
                    unit_id=uuid.UUID(unit_id),
                    asset_id=uuid.UUID(entity["asset_id"]),
                    asset_name="",  # 由服务层从 PG 补全（Milvus 行不冗余资产名）
                    space_id=uuid.UUID(entity["space_id"]),
                    unit_type=entity["unit_type"],
                    title=None,
                    text=entity["text"],
                    score=base,
                    rank_scores=rank_scores,
                    locator={},
                    highlight=make_highlight(query, entity["text"]),
                    graph_evidence=[p.to_dict() for p in graph_paths.get(unit_id, [])[:6]],
                )
            )
        return hits

    def _want_graph(self, use_graph: bool | None) -> bool:
        return self._settings.graph_enabled if use_graph is None else use_graph

    @staticmethod
    def _want_l4(unit_types: list[str] | None) -> bool:
        """L4 参与条件：未显式排除 community 类型。"""
        return unit_types is None or "community" in unit_types
