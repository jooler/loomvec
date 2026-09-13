"""P3-CORE-04 L3 图谱召回：实体链接 → AGE 1~2 跳扩展 → chunk_ids 回收。

流程（03 文档 §四）：
1. 实体链接：查询向量（与 L1 共用，不重复计费）→ Milvus entities top-k
   （≥ entity_link_threshold）；
2. 图扩展：命中实体 1~2 跳邻居（Python 侧 BFS——边表经 AGE 同库读出，
   空间规模内往返成本可控，且路径证据可完整保留）；
3. chunk 回收：边的 chunk_ids ∪ → Milvus 按 id 取回行（过滤保证空间/租户
   合法），成为与 L1/L2 同形的候选行参与 RRF 融合与统一重排；
4. 邻接补全：融合后 top-N 命中作种子，1 跳邻居 chunk 并入候选池（重排前）；
5. 降级：链接为空 / 集合缺失 / AGE 故障 → 返回空 GraphRecall，检索退化为
   L1+L2（任何图谱故障不影响基础检索可用性）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import GraphSettings, SearchSettings
from loomvec.core.errors import UpstreamUnavailableError
from loomvec.core.graph.age import AgeStore, GraphEdge, GraphNode
from loomvec.core.retrieval.store import MilvusStore

logger = structlog.get_logger("loomvec.retrieval.graph")

AGE = AgeStore()

# Milvus id 查询单批上限（filter in [...] 过长会被拒）
_ID_BATCH = 80


@dataclass
class PathEvidence:
    """实体→关系→实体路径（graph_evidence 契约单元）。"""

    head: dict[str, str]  # {key, name, type}
    relation: dict[str, Any]  # {type, chunk_ids}
    tail: dict[str, str]
    hops: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"head": self.head, "relation": self.relation, "tail": self.tail, "hops": self.hops}


@dataclass
class GraphRecall:
    """L3 召回结果：候选行（与 Milvus 行同形）+ 证据链 + 命中实体。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    paths: dict[str, list[PathEvidence]] = field(default_factory=dict)  # unit_id → 路径
    linked_entities: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.rows)


class GraphRetriever:
    """L3 图谱召回器（API 检索服务与问答检索共用）。"""

    def __init__(
        self,
        milvus: MilvusStore,
        settings_graph: GraphSettings,
        settings_search: SearchSettings,
    ) -> None:
        self._milvus = milvus
        self._graph_settings = settings_graph
        self._search_settings = settings_search

    @property
    def adjacency_enabled(self) -> bool:
        return self._graph_settings.adjacency_enabled

    @property
    def adjacency_top_k(self) -> int:
        return self._graph_settings.adjacency_top_k

    async def recall(
        self,
        session: AsyncSession,
        *,
        space_ids: list[UUID],
        query_vector: list[float],
        tenant_id: UUID | None = None,
        unit_types: list[str] | None = None,
    ) -> GraphRecall:
        settings = self._graph_settings
        # 1) 实体链接
        try:
            hits = await self._milvus.async_search_entities(
                query_vector=query_vector, space_ids=space_ids, top_k=settings.link_top_k
            )
        except UpstreamUnavailableError as e:
            logger.info("graph_link_degraded", reason=str(e))
            return GraphRecall()
        threshold = settings.entity_link_threshold
        linked_keys = [k for k, score in hits if score >= threshold]
        if not linked_keys:
            return GraphRecall()

        # 2) 图扩展（同库读边表 → Python BFS）
        nodes: dict[str, GraphNode] = {}
        edges: list[GraphEdge] = []
        try:
            for sid in space_ids:
                nodes.update(await AGE.load_space_nodes(session, str(sid)))
                edges.extend(await AGE.load_space_edges(session, str(sid)))
        except Exception as e:
            logger.info("graph_expand_degraded", reason=str(e))
            return GraphRecall()
        adjacency = self._build_adjacency(edges)
        paths = self._expand_paths(nodes, adjacency, linked_keys, settings.expand_hops)
        if not paths:
            return GraphRecall(linked_entities=self._entity_infos(nodes, linked_keys))

        # 3) chunk 回收：路径触达的 chunk_ids → Milvus 取回候选行
        chunk_score: dict[str, float] = defaultdict(float)
        for path in paths:
            contribution = 1.0 / path.hops
            for cid in path.relation.get("chunk_ids") or []:
                chunk_score[cid] += contribution
        unit_ids = sorted(chunk_score, key=lambda c: -chunk_score[c])[: settings.graph_top_k]
        rows = await self._fetch_unit_rows(space_ids, tenant_id, unit_ids, unit_types)
        rows.sort(key=lambda r: -chunk_score.get(r["id"], 0.0))

        unit_paths: dict[str, list[PathEvidence]] = defaultdict(list)
        wanted = {str(r["id"]) for r in rows}
        for path in paths:
            for cid in path.relation.get("chunk_ids") or []:
                if cid in wanted:
                    unit_paths[cid].append(path)

        return GraphRecall(
            rows=rows,
            paths=dict(unit_paths),
            linked_entities=self._entity_infos(nodes, linked_keys),
        )

    async def adjacency_neighbors(
        self,
        session: AsyncSession,
        *,
        space_ids: list[UUID],
        seed_unit_ids: list[str],
    ) -> dict[str, list[str]]:
        """邻接补全：种子 chunk 的 1 跳邻居 chunk（不含种子本身）。"""
        if not seed_unit_ids:
            return {}
        seeds = set(seed_unit_ids)
        neighbors: dict[str, list[str]] = defaultdict(list)
        try:
            for sid in space_ids:
                edges = await AGE.load_space_edges(session, str(sid))
                for e in edges:
                    hit = seeds & set(e.chunk_ids)
                    if not hit:
                        continue
                    extras = [c for c in e.chunk_ids if c not in seeds]
                    for seed in hit:
                        neighbors[seed].extend(c for c in extras if c not in neighbors[seed])
        except Exception as e:
            logger.info("graph_adjacency_degraded", reason=str(e))
            return {}
        return dict(neighbors)

    # ---------- 内部 ----------

    @staticmethod
    def _build_adjacency(edges: list[GraphEdge]) -> dict[str, list[tuple[str, GraphEdge]]]:
        adj: dict[str, list[tuple[str, GraphEdge]]] = defaultdict(list)
        for e in edges:
            adj[e.head_key].append((e.tail_key, e))
            adj[e.tail_key].append((e.head_key, e))
        return adj

    def _expand_paths(
        self,
        nodes: dict[str, GraphNode],
        adjacency: dict[str, list[tuple[str, GraphEdge]]],
        linked_keys: list[str],
        hops: int,
    ) -> list[PathEvidence]:
        """1~2 跳路径枚举（节点不在图中的链接结果自然无路径）。"""
        paths: list[PathEvidence] = []
        seen: set[tuple[str, str, str]] = set()
        hops = max(1, min(2, hops))
        for start in linked_keys:
            for nxt, edge1 in adjacency.get(start, ()):  # 1 跳
                if self._push(paths, seen, nodes, start, edge1, nxt, 1) and hops >= 2:
                    for nn, edge2 in adjacency.get(nxt, ()):  # 2 跳
                        if nn == start:
                            continue
                        self._push(paths, seen, nodes, nxt, edge2, nn, 2)
        return paths

    def _push(
        self,
        paths: list[PathEvidence],
        seen: set[tuple[str, str, str]],
        nodes: dict[str, GraphNode],
        from_key: str,
        edge: GraphEdge,
        to_key: str,
        hops: int,
    ) -> bool:
        """有 chunk 溯源的边才构成证据路径；返回 to 节点是否可继续扩展。"""
        head, tail = nodes.get(from_key), nodes.get(to_key)
        if head is None or tail is None:
            return False
        dedupe = (from_key, to_key, edge.type)
        if dedupe in seen:
            return True
        seen.add(dedupe)
        if edge.chunk_ids:
            paths.append(
                PathEvidence(
                    head=self._info(head),
                    relation={"type": edge.type, "chunk_ids": list(edge.chunk_ids)},
                    tail=self._info(tail),
                    hops=hops,
                )
            )
        return True

    @staticmethod
    def _info(node: GraphNode) -> dict[str, str]:
        return {"key": node.key, "name": node.name, "type": node.type}

    @staticmethod
    def _entity_infos(nodes: dict[str, GraphNode], keys: list[str]) -> list[dict[str, str]]:
        return [GraphRetriever._info(nodes[k]) for k in keys if k in nodes]

    async def fetch_rows_for_adjacency(
        self,
        *,
        space_ids: list[UUID],
        tenant_id: UUID | None,
        unit_ids: list[str],
        unit_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """邻接补全候选行取回（与 L1/L2 行同形，参与统一重排）。"""
        return await self._fetch_unit_rows(space_ids, tenant_id, unit_ids, unit_types)

    async def _fetch_unit_rows(
        self,
        space_ids: list[UUID],
        tenant_id: UUID | None,
        unit_ids: list[str],
        unit_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按 id 取回候选行（空间/租户过滤兜底）；分批防 filter 超长。"""
        rows: list[dict[str, Any]] = []
        for i in range(0, len(unit_ids), _ID_BATCH):
            batch = unit_ids[i : i + _ID_BATCH]
            ids_expr = ", ".join(f'"{u}"' for u in batch)
            expr = f"id in [{ids_expr}]"
            if tenant_id is not None:
                expr += f' and tenant_id == "{tenant_id}"'
            if unit_types:
                expr += " and " + f"unit_type in [{', '.join(f'"{t}"' for t in unit_types)}]"
            try:
                rows.extend(await self._milvus.async_query_units(expr=expr, ids=batch))
            except UpstreamUnavailableError as e:
                logger.info("graph_chunk_fetch_degraded", reason=str(e))
                return []
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                unique.append(r)
        return unique
