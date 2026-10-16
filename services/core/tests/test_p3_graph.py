"""P3-QA-02/P3-CORE-02 图谱抽取与合并协议单元测试（无外部依赖）。"""

from __future__ import annotations

import pytest

from loomvec.core.ai.mock import MockBackend
from loomvec.core.config import AiSettings
from loomvec.core.graph.age import GraphEdge
from loomvec.core.graph.communities import detect_communities
from loomvec.core.graph.merge import name_candidates
from loomvec.core.graph.ontology import (
    entity_embed_text,
    entity_key,
    normalize_entity_name,
)
from loomvec.core.graph.profiles import BIOMEDICAL
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
    merged = parse_merged(raw, batch_start=1, batch_end=5, profile=BIOMEDICAL)
    assert merged.markers_ok and merged.extraction_ok
    assert merged.entities[0].type == "organization"  # 白名单归并小写
    assert merged.relations[0].type == "SUBSIDIARY_OF"


def test_parse_merged_markers_invalid_extraction_ok():
    raw = {
        "chunk_markers": [{"start_line": 5, "end_line": 2, "title": "倒置区间"}],
        "entities": [{"name": "张三", "type": "Person", "lines": [1]}],
        "relations": [],
    }
    merged = parse_merged(raw, batch_start=1, batch_end=5, profile=BIOMEDICAL)
    assert not merged.markers_ok  # markers 失效 → 兜底分片
    assert merged.extraction_ok  # 抽取不受牵连


def test_parse_merged_markers_ok_extraction_invalid():
    raw = {
        "chunk_markers": [{"start_line": 1, "end_line": 3, "title": None, "keywords": []}],
        "relations": [{"head": "孤儿", "tail": "另一孤儿", "type": "X"}],  # entities 缺失
    }
    merged = parse_merged(raw, batch_start=1, batch_end=3, profile=BIOMEDICAL)
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
    merged = parse_merged(raw, batch_start=1, batch_end=2, profile=BIOMEDICAL)
    types = sorted(e.type for e in merged.entities)
    names = [e.name for e in merged.entities]
    assert types == ["other"] and names == ["甲"]


def test_sanitize_extraction_drops_unresolvable_relations():
    entities = [{"name": "Alice", "type": "person"}]
    relations = [
        {"head": "Alice", "tail": "B", "type": "KNOWS"},
    ]
    merged = parse_merged(
        {"chunk_markers": [], "entities": entities, "relations": relations},
        batch_start=1,
        batch_end=1,
        profile=BIOMEDICAL,
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
    def __init__(self, key: str, name: str, type: str = "t"):
        self.entity_key = key
        self.name = name
        self.type = type


def _pairs(entities: list[_FakeEntity]) -> set[tuple[str, str]]:
    return {(c.winner_key, c.loser_key) for c in name_candidates(entities)}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("genetic interactions", "genetic interaction"),  # 复数
        ("Hematopoietic stem and progenitor cell", "hematopoietic stem and progenitor cells"),
        ("cancer stem cells (CSC)", "cancer stem cells (CSCs)"),  # 括号大写缩写
        ("Chain-of-Thought (CoT)", "Chain-of-Thought"),  # 缩写含连接词首字母
        ("copy number variation (CNV)", "copy number variation"),
        (
            "Senescence-associated secretory phenotype (SASP)",
            "Senescence-associated secretory phenotype",
        ),
        ("Cell foundation model", "cell foundation models"),
        ("NF-κB pathway", "NFκB pathway"),  # 分隔符/大小写由 name_norm 吸收
        ("PBMC", "PBMCs"),
    ],
)
def test_name_candidates_merges_pure_writing_variants(a, b):
    assert _pairs([_FakeEntity("k1", a), _FakeEntity("k2", b)]) == {("k1", "k2")}


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # 线上实测的错并样例：差一个字母/数字/符号的是不同实体
        ("UBE2A", "UBE2B"),
        ("SEMA3A", "SEMA5A"),
        ("STAG1", "STAG2"),
        ("CD8+ T cells", "CD4+ T cells"),
        ("Th1 cells", "Th2 cells"),
        ("GK+ Treg cells", "GK- Treg"),
        ("Conventional protein secretion", "Unconventional protein secretion"),
        ("Retrieval (No LLM)", "LLM (No retrieval)"),
        ("Supplementary Table S4", "Supplementary Table 11"),
        ("Wnt pathway", "AKT pathway"),
        ("IL-6 (human)", "IL-6 (mouse)"),  # 小写括注是限定语，不是缩写
        ("Genecorpus-1M", "Genecorpus-30M"),
        # 括号里的限定语不是缩写：只有能由括号前各词首字母推出的才剥离
        ("T cells (CD4)", "T cells (CD8)"),
        ("Macrophages (M1)", "Macrophages (M2)"),
        ("TP53 (WT)", "TP53 (KO)"),
        ("Cells (HeLa)", "Cells (HEK293)"),
        ("Treg (CD25+) cells", "Treg (CD25-) cells"),
        ("E. coli BL21(DE3)", "E. coli BL21"),
        ("ESM-2", "ESM-2 (3B)"),
        ("PBMC", "PBMC (PBS)"),
        ("Gemini 3 Pro", "Gemini 3 Pro (Few-shot)"),
        ("DIPG-XIII(K27M-KO)", "DIPG-XIII"),
        # 型号后缀的大写 S 不是复数
        ("CytoFLEX S", "CytoFlex"),
    ],
)
def test_name_candidates_never_merges_near_miss_names(a, b):
    assert _pairs([_FakeEntity("k1", a), _FakeEntity("k2", b)]) == set()


def test_name_candidates_same_type_only_and_earliest_wins():
    entities = [
        _FakeEntity("k1", "virtual tissue", "concept"),
        _FakeEntity("k2", "virtual tissues", "concept"),
        _FakeEntity("k3", "virtual tissues", "method"),  # 类型不同不并
        _FakeEntity("k4", "Virtual Tissues", "concept"),
    ]
    assert _pairs(entities) == {("k1", "k2"), ("k1", "k4")}


def test_name_candidates_skips_short_names():
    assert _pairs([_FakeEntity("k1", "p53"), _FakeEntity("k2", "P-53")]) == set()


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

    messages = build_extraction_messages(
        lines, 1, 29, min_chars=10, max_chars=100, max_line_chars=6000, profile=BIOMEDICAL
    )
    mock = MockBackend(settings)
    raw = mock.complete("\n".join(m["content"] for m in messages))
    merged = parse_merged(raw, batch_start=1, batch_end=29, profile=BIOMEDICAL)
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


def test_parse_merged_drops_noise_entities_and_dangling_relations():
    raw = {
        "chunk_markers": [{"start_line": 1, "end_line": 5}],
        "entities": [
            {"name": "TP53", "type": "gene_protein", "description": "抑癌基因", "lines": [1]},
            {"name": "Zoe Wefers", "type": "person", "description": "论文作者，斯坦福大学"},
            {"name": "Perez et al.", "type": "person", "description": "文献作者组"},
            {"name": "Qingdao University", "type": "organization", "description": "作者单位之一"},
            {"name": "Online Methods", "type": "other", "description": "Section heading."},
            {"name": "x" * 130, "type": "other", "description": "整句"},
        ],
        "relations": [
            {"head": "TP53", "tail": "Zoe Wefers", "type": "STUDIED_BY"},  # 端点被丢弃
            {"head": "TP53", "tail": "Qingdao University", "type": "AFFILIATED_WITH"},
        ],
    }
    merged = parse_merged(raw, batch_start=1, batch_end=5, profile=BIOMEDICAL)
    assert [e.name for e in merged.entities] == ["TP53"]
    assert merged.entities[0].type == "gene_protein"
    assert merged.relations == []


def test_parse_merged_blocks_administrative_relation_types():
    raw = {
        "chunk_markers": [],
        "entities": [
            {"name": "scGPT", "type": "method"},
            {"name": "Cell Atlas", "type": "dataset"},
        ],
        "relations": [
            {"head": "scGPT", "tail": "Cell Atlas", "type": "trained on"},
            {"head": "scGPT", "tail": "Cell Atlas", "type": "FUNDED_BY"},
        ],
    }
    merged = parse_merged(raw, batch_start=1, batch_end=2, profile=BIOMEDICAL)
    assert [r.type for r in merged.relations] == ["TRAINED_ON"]


def test_noise_reason_keeps_real_entities():
    from loomvec.core.graph.entity_filter import noise_reason

    def reason(name: str, etype: str, desc: str) -> str | None:
        return noise_reason(name, etype, desc, profile=BIOMEDICAL)

    assert reason("Tahoe Therapeutics", "organization", "Biotechnology company") is None
    assert reason("Geneformer", "method", "单细胞基础模型") is None
    assert reason("PD-L2", "gene_protein", "PD-1 配体") is None
    assert reason("Alan Turing", "person", "计算机科学家") is None
    assert reason("N.C.", "person", "获得资助") == "person_citation_or_initials"
