"""P3-QA-02/P3-CORE-02 图谱抽取与合并协议单元测试（无外部依赖）。"""

from __future__ import annotations

from loomvec.core.ai.mock import MockBackend
from loomvec.core.config import AiSettings
from loomvec.core.graph.age import GraphEdge
from loomvec.core.graph.communities import detect_communities
from loomvec.core.graph.merge import name_candidates, vector_candidates
from loomvec.core.graph.ontology import (
    entity_embed_text,
    entity_key,
    normalize_entity_name,
)
from loomvec.core.pipeline.chunking import (
    ChunkDraft,
    TableRange,
    structural_fallback,
    validate_markers,
)
from loomvec.core.pipeline.extraction import (
    attribute_relations,
    parse_merged,
    sanitize_extraction,
)

# ---------------------------------------------------------------------------
# 合并抽取协议：独立校验互不牵连
# ---------------------------------------------------------------------------


def test_parse_merged_both_valid():
    raw = {
        "chunk_markers": [{"start_line": 1, "end_line": 5, "title": "T", "keywords": []}],
        "entities": [{"name": "A 公司", "type": "Organization", "description": "d", "lines": [2]}],
        "relations": [
            {"head": "A 公司", "tail": "B 公司", "type": "SUBSIDIARY_OF", "evidence_lines": [3]}
        ],
    }
    merged = parse_merged(raw, batch_start=1, batch_end=5)
    assert merged.markers_ok and merged.extraction_ok
    assert merged.entities[0].type == "organization"  # 白名单归并小写
    assert merged.relations[0].type == "SUBSIDIARY_OF"


def test_parse_merged_markers_invalid_extraction_ok():
    raw = {
        "chunk_markers": [{"start_line": 5, "end_line": 2, "title": "倒置区间"}],
        "entities": [{"name": "张三", "type": "Person", "lines": [1]}],
        "relations": [],
    }
    merged = parse_merged(raw, batch_start=1, batch_end=5)
    assert not merged.markers_ok  # markers 失效 → 兜底分片
    assert merged.extraction_ok  # 抽取不受牵连


def test_parse_merged_markers_ok_extraction_invalid():
    raw = {
        "chunk_markers": [{"start_line": 1, "end_line": 3, "title": None, "keywords": []}],
        "relations": [{"head": "孤儿", "tail": "另一孤儿", "type": "X"}],  # entities 缺失
    }
    merged = parse_merged(raw, batch_start=1, batch_end=3)
    assert merged.markers_ok
    assert not merged.extraction_ok  # → graph_status=pending_retry


def test_parse_merged_unknown_type_coerced_and_bad_dropped():
    raw = {
        "chunk_markers": [{"start_line": 1, "end_line": 2}],
        "entities": [
            {"name": "甲", "type": "Alien", "lines": [1]},  # 未知类型归并 other
            {"name": "", "lines": [2]},  # 空名丢弃
        ],
        "relations": [],
    }
    merged = parse_merged(raw, batch_start=1, batch_end=2)
    types = sorted(e.type for e in merged.entities)
    names = [e.name for e in merged.entities]
    assert types == ["other"] and names == ["甲"]


def test_sanitize_extraction_drops_unresolvable_relations():
    entities = [{"name": "A", "type": "person"}]
    relations = [
        {"head": "A", "tail": "B", "type": "KNOWS"},
    ]
    merged = parse_merged(
        {"chunk_markers": [], "entities": entities, "relations": relations},
        batch_start=1,
        batch_end=1,
    )
    _entities_parsed, relations_parsed = sanitize_extraction(merged.entities, merged.relations)
    assert len(relations_parsed) == 0  # B 不在实体清单 → 关系丢弃


def test_attribute_relations_overlap_wins():
    from loomvec.core.pipeline.extraction import RelationMention

    drafts = [
        ChunkDraft(1, 3, key=0),
        ChunkDraft(4, 8, key=1),
    ]
    r = RelationMention(head="A", tail="B", type="X", evidence_lines=[5, 6])
    attribute_relations([r], drafts)
    assert r.chunk_key == 1  # 与第二块重叠最多


# ---------------------------------------------------------------------------
# 实体规范化与确定性 ID
# ---------------------------------------------------------------------------


def test_normalize_entity_name():
    assert normalize_entity_name("ＡＢＣ 公司") == normalize_entity_name("abc公司")
    assert normalize_entity_name("Open AI，Inc.") == normalize_entity_name("openaiinc")


def test_entity_key_deterministic_and_type_sensitive():
    k1 = entity_key("space-1", "A 公司", "organization")
    k2 = entity_key("space-1", "Ａ 公司 ", "organization")  # 归一化后同名
    k3 = entity_key("space-1", "A 公司", "person")
    k4 = entity_key("space-2", "A 公司", "organization")
    assert k1 == k2
    assert k1 != k3 and k1 != k4


def test_entity_embed_text_type_prefix():
    text = entity_embed_text("A 公司", "ORGANIZATION", "描述")
    assert text.startswith("[organization]")


# ---------------------------------------------------------------------------
# 合并候选（阶段二）
# ---------------------------------------------------------------------------


class _FakeEntity:
    def __init__(self, key: str, norm: str, type: str):
        self.entity_key = key
        self.name_norm = norm
        self.type = type


def test_name_candidates_same_type_only():
    entities = [
        _FakeEntity("k1", "阿里巴巴集团", "organization"),
        _FakeEntity("k2", "阿里巴巴集团", "person"),
        _FakeEntity("k3", "阿里巴巴集图", "organization"),  # 距离 1
        _FakeEntity("k4", "腾讯", "organization"),  # 距离远
    ]
    out = name_candidates(entities, max_distance=2)
    pairs = {(c.winner_key, c.loser_key) for c in out}
    assert ("k1", "k3") in pairs
    assert all(c.loser_key != "k2" for c in out)  # 类型不同不并


def test_vector_candidates_threshold():
    v1 = [1.0, 0.0]
    v2 = [0.99, 0.141]  # 与 v1 余弦 ~0.99
    v3 = [0.0, 1.0]
    entities = [_FakeEntity("a", "a", "t"), _FakeEntity("b", "b", "t"), _FakeEntity("c", "c", "t")]
    cands = vector_candidates(entities, {"a": v1, "b": v2, "c": v3}, threshold=0.94)
    assert any(c.loser_key == "b" and c.winner_key == "a" for c in cands)
    assert not any(c.loser_key == "c" for c in cands)


# ---------------------------------------------------------------------------
# 社区检测
# ---------------------------------------------------------------------------


def test_detect_communities_two_clusters():
    edges = [
        GraphEdge("a", "b", "R"),
        GraphEdge("b", "c", "R"),
        GraphEdge("a", "c", "R"),
        GraphEdge("d", "e", "R"),
    ]
    mapping = detect_communities(["a", "b", "c", "d", "e"], edges)
    assert mapping["a"] == mapping["b"] == mapping["c"]
    assert mapping["d"] == mapping["e"]
    assert mapping["a"] != mapping["d"]


def test_detect_communities_empty():
    assert detect_communities([], []) == {}


# ---------------------------------------------------------------------------
# mock 网关的合并抽取（CI 全链路依赖）
# ---------------------------------------------------------------------------


def test_mock_complete_chunk_graph():
    settings = AiSettings(mock=True)
    lines = [f"L{i}: 阿里巴巴集团 与 腾讯控股 是科技公司" for i in range(1, 30)]
    from loomvec.core.pipeline.extraction import build_extraction_messages

    messages = build_extraction_messages(lines, 1, 29, min_chars=10, max_chars=100)
    mock = MockBackend(settings)
    raw = mock.complete("\n".join(m["content"] for m in messages))
    merged = parse_merged(raw, batch_start=1, batch_end=29)
    assert merged.markers_ok
    assert merged.extraction_ok and len(merged.entities) >= 2


# ---------------------------------------------------------------------------
# markers 兜底分片与抽取的行区间归属（03 文档 §3.2 失效组合）
# ---------------------------------------------------------------------------


def test_structural_fallback_chunks_cover_batch():
    lines = ["# 标题"] + [f"第{i}行内容，足够长一些以便形成分片。" for i in range(1, 40)]
    tables = [TableRange(10, 11, "<table><tr><td>x</td></tr></table>")]
    drafts = structural_fallback(
        lines, batch_start=1, batch_end=39, tables=tables, min_chars=10, max_chars=120
    )
    assert drafts
    covered = set()
    for d in drafts:
        covered.update(range(d.start_line, d.end_line + 1))
    assert {1, 39}.issubset(covered)
    tables_covered = any(d.is_table and d.start_line <= 10 and d.end_line >= 11 for d in drafts)
    assert tables_covered


def test_validate_markers_merges_small_chunks():
    lines = [f"短行{i}" for i in range(1, 12)]
    tables: list[TableRange] = []
    markers = [
        {"start_line": 1, "end_line": 3, "title": "a", "keywords": []},
        {"start_line": 4, "end_line": 11, "title": "b", "keywords": []},
    ]
    drafts = validate_markers(
        markers,
        lines,
        batch_start=1,
        batch_end=11,
        tables=tables,
        min_chars=5,
        max_chars=1000,
    )
    assert drafts and drafts[0].start_line == 1 and drafts[-1].end_line == 11
