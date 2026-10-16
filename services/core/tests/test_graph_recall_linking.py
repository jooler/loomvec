"""GraphRetriever.recall 的实体链接：Milvus 里残留的、图中已无节点的向量不能挤掉真实实体。

线上事实：合并败者、重抽后被替换的旧实体、其他空间未清理的旧实体，向量都还在 Milvus entities 里，
图中已没有对应节点；只取 link_top_k 个候选时它们占满名额，图谱召回为空。
"""

from __future__ import annotations

import uuid

from loomvec.core.config import GraphSettings, SearchSettings
from loomvec.core.graph.age import GraphEdge, GraphNode
from loomvec.core.retrieval import graph_retrieval
from loomvec.core.retrieval.graph_retrieval import LINK_OVERFETCH, GraphRetriever

SPACE = uuid.uuid4()


def _node(key: str) -> GraphNode:
    return GraphNode(key=key, space_id=str(SPACE), tenant_id=None, name=key.upper(), type="concept")


class _Milvus:
    def __init__(self, hits: list[tuple[str, float]]) -> None:
        self.hits = hits
        self.asked_top_k: int | None = None

    async def async_search_entities(self, *, query_vector, space_ids, top_k):
        self.asked_top_k = top_k
        return self.hits[:top_k]

    async def async_query_units(self, *, expr, ids):
        return []


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def begin_nested(self):
        return _Nested()


def _retriever(milvus, monkeypatch, nodes: list[str], edges: list[GraphEdge]):
    async def load_nodes(session, space_id):
        return {k: _node(k) for k in nodes}

    async def load_edges(session, space_id):
        return edges

    monkeypatch.setattr(graph_retrieval.AGE, "load_space_nodes", load_nodes)
    monkeypatch.setattr(graph_retrieval.AGE, "load_space_edges", load_edges)
    return GraphRetriever(milvus, GraphSettings(link_top_k=2), SearchSettings())


async def test_stale_vectors_do_not_crowd_out_real_entities(monkeypatch):
    # 前 2 名都是图里已不存在的旧向量，真实实体排在第 3、4 名
    milvus = _Milvus(
        [("stale1", 0.9), ("stale2", 0.85), ("real1", 0.8), ("real2", 0.7), ("low", 0.3)]
    )
    edges = [GraphEdge("real1", "other", "USES", chunk_ids=["c1"], asset_ids=["a"])]
    r = _retriever(milvus, monkeypatch, ["real1", "real2", "other"], edges)
    out = await r.recall(_Session(), space_ids=[SPACE], query_vector=[0.1])  # type: ignore[arg-type]
    assert milvus.asked_top_k == 2 * LINK_OVERFETCH
    assert [e["key"] for e in out.linked_entities] == ["real1", "real2"]


async def test_all_candidates_stale_returns_empty(monkeypatch):
    milvus = _Milvus([("stale1", 0.9), ("stale2", 0.85)])
    r = _retriever(milvus, monkeypatch, ["real1"], [])
    out = await r.recall(_Session(), space_ids=[SPACE], query_vector=[0.1])  # type: ignore[arg-type]
    assert out.linked_entities == [] and out.rows == []


async def test_below_threshold_candidates_are_ignored(monkeypatch):
    milvus = _Milvus([("real1", 0.3)])  # 低于默认阈值 0.65
    r = _retriever(milvus, monkeypatch, ["real1"], [])
    out = await r.recall(_Session(), space_ids=[SPACE], query_vector=[0.1])  # type: ignore[arg-type]
    assert out.linked_entities == []
