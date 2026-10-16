"""单资产实体 / 关系上限：超限时按重要性保留，而不是按出现顺序截断。

直接测 EntityGraphService 的两个纯函数，不经 DB。
"""

from __future__ import annotations

from types import SimpleNamespace

from loomvec.core.pipeline.graph_flow import EntityGraphService


def _svc(max_entities: int, max_relations: int = 600) -> EntityGraphService:
    graph = SimpleNamespace(
        max_entities_per_asset=max_entities, max_relations_per_asset=max_relations
    )
    return EntityGraphService(
        session=None, deps=SimpleNamespace(settings=SimpleNamespace(graph=graph))
    )  # type: ignore[arg-type]


_ASSET = SimpleNamespace(space_id="space-1", id="asset-1")


def _mention(name: str, line: int = 1) -> dict:
    return {"name": name, "type": "product", "lines": [line]}


def test_under_cap_keeps_everything_in_the_same_order():
    svc = _svc(10)
    clusters, _ = svc._cluster_entities([_mention("B"), _mention("A"), _mention("C")], _ASSET, [])
    assert [c.name for c in clusters] == ["A", "B", "C"]  # 按归一化名排序，与以前一致


def test_over_cap_prefers_entities_with_more_relations_not_earlier_ones():
    # 文档顺序 E0..E9；只有靠后的 E8、E9 参与关系。顺序截断会把它们丢掉
    mentions = [_mention(f"E{i}", line=i + 1) for i in range(10)]
    relations = [
        {"head": "E8", "tail": "E9", "type": "USES"},
        {"head": "E8", "tail": "E9", "type": "PART_OF"},
        {"head": "E9", "tail": "E7", "type": "USES"},
    ]
    clusters, _ = _svc(3)._cluster_entities(mentions, _ASSET, relations)
    assert {c.name for c in clusters} == {"E9", "E8", "E7"}  # 度：E9=3、E8=2、E7=1


def test_over_cap_then_mention_count_then_earlier_first():
    # 无关系时按被提及次数；次数相同则靠前者优先
    mentions = [_mention("a"), _mention("b"), _mention("c"), _mention("c"), _mention("d")]
    clusters, _ = _svc(2)._cluster_entities(mentions, _ASSET, [])
    assert {c.name for c in clusters} == {"c", "a"}  # c 两次；a、b、d 各一次，a 最靠前


def test_relation_endpoints_dropped_with_their_entities():
    svc = _svc(2)
    mentions = [_mention("A"), _mention("B"), _mention("C")]
    relations = [
        {"head": "A", "tail": "B", "type": "USES", "chunk_key": None},
        {"head": "B", "tail": "C", "type": "USES", "chunk_key": None},
    ]
    clusters, key_map = svc._cluster_entities(mentions, _ASSET, relations)
    edges = svc._merge_relations(relations, key_map, {}, _ASSET)
    kept = {c.name for c in clusters}
    assert kept == {"A", "B"}  # B 度为 2 最重要；A、C 度同为 1、次数相同，靠前的 A 优先
    names = {c.key: c.name for c in clusters}
    assert [(names[e.head_key], names[e.tail_key]) for e in edges] == [
        ("A", "B")
    ]  # B→C 随 C 一起丢


def test_relations_over_cap_keep_higher_weight_and_original_order():
    svc = _svc(100, max_relations=2)
    mentions = [_mention(n) for n in "ABCDE"]
    relations = (
        [{"head": "A", "tail": "B", "type": "R1"}]  # 权重 1
        + [{"head": "C", "tail": "D", "type": "R2"}] * 3  # 权重 3
        + [{"head": "D", "tail": "E", "type": "R3"}] * 2  # 权重 2
    )
    _, key_map = svc._cluster_entities(mentions, _ASSET, relations)
    edges = svc._merge_relations(relations, key_map, {}, _ASSET)
    assert [e.type for e in edges] == ["R2", "R3"]  # 留权重最高的两条，顺序保持原样
