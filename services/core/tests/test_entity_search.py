"""MilvusStore.search_entities 回归：返回真实 IP 相似度，而不是 RRF 排名分。

曾经经 hybrid_search + RRFRanker 包装单路检索，返回值恒约 0.016（1/(k+rank)），
与相似度阈值比较永远不命中 → 图谱召回从未生效。
"""

from __future__ import annotations

import uuid

from loomvec.core.config import AiSettings, MilvusSettings
from loomvec.core.retrieval.store import COLLECTION_ENTITIES, MilvusStore


class _Client:
    def __init__(self, has_collection: bool = True) -> None:
        self._has = has_collection
        self.search_kwargs: dict | None = None

    def has_collection(self, name: str) -> bool:
        return self._has and name == COLLECTION_ENTITIES

    def search(self, collection: str, **kwargs):
        self.search_kwargs = {"collection": collection, **kwargs}
        return [[{"id": "k1", "distance": 0.79, "entity": {}}, {"id": "k2", "distance": 0.68}]]

    def hybrid_search(self, *args, **kwargs):  # pragma: no cover — 不应再走融合排序
        raise AssertionError("search_entities 不应使用 hybrid_search/RRF")


def _store(client: _Client) -> MilvusStore:
    store = MilvusStore(MilvusSettings(), AiSettings())
    store._ensure_client = lambda: client  # type: ignore[method-assign]
    return store


def test_search_entities_returns_raw_similarity_with_space_filter():
    client = _Client()
    sid = uuid.uuid4()
    out = _store(client).search_entities(query_vector=[0.1, 0.2], space_ids=[sid], top_k=4)
    assert out == [("k1", 0.79), ("k2", 0.68)]
    kw = client.search_kwargs
    assert kw is not None
    assert kw["limit"] == 4
    assert kw["search_params"]["metric_type"] == "IP"
    assert str(sid) in kw["filter"] and "space_id" in kw["filter"]


def test_search_entities_missing_collection_is_empty():
    out = _store(_Client(has_collection=False)).search_entities(
        query_vector=[0.1], space_ids=[uuid.uuid4()], top_k=4
    )
    assert out == []


def test_query_units_rows_have_hybrid_search_shape():
    """L3 chunk 回收/邻接补全的行要和 L1/L2 同形（{id, distance, entity}），才能进融合与重排。"""

    class _Q(_Client):
        def query(self, collection: str, **kwargs):
            return [
                {"id": "u1", "asset_id": "a", "space_id": "s", "unit_type": "text", "text": "t"}
            ]

    rows = _store(_Q()).query_units(expr='id in ["u1"]', limit=1)
    assert rows == [
        {
            "id": "u1",
            "distance": 0.0,
            "entity": {"asset_id": "a", "space_id": "s", "unit_type": "text", "text": "t"},
        }
    ]
