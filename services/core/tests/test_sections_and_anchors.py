"""论文「不可抽取区段」识别 + v4 锚点抽取契约（预算 / 占位 / 孤立实体 / aliases）。"""

from loomvec.core.graph.profiles import BIOMEDICAL
from loomvec.core.pipeline.extraction import (
    build_extraction_messages,
    entity_cap_for,
    parse_merged,
    sanitize_extraction,
)
from loomvec.core.pipeline.sections import find_skip_ranges, is_skip_heading

PAPER = [
    "# Title",  # 1
    "## Introduction",  # 2
    "TGFB1 regulates EMT.",  # 3
    "## Benchmarking against competing methods",  # 4  ← 含 competing，不能误伤
    "scGPT outperforms Geneformer.",  # 5
    "## References",  # 6
    "1. Smith J. A big paper title. Nature 2020.",  # 7
    "## Acknowledgements",  # 8
    "Funded by NIH grant R01.",  # 9
    "## Methods",  # 10  ← 非跳过标题，区段到此结束
    "Cells were cultured.",  # 11
    "## Data availability",  # 12
    "GEO: GSE12345.",  # 13
]


def test_find_skip_ranges_merges_consecutive_and_stops_at_normal_heading():
    assert find_skip_ranges(PAPER) == [(6, 9), (12, 13)]


def test_skip_heading_variants_and_false_positives():
    assert is_skip_heading("## 6. References")
    assert is_skip_heading("# REFERENCES AND NOTES")
    assert is_skip_heading("## Acknowledgments and Disclosure of Funding")
    assert is_skip_heading("## 致谢")
    assert not is_skip_heading("## Benchmarking against competing methods")
    assert not is_skip_heading("## Cross-species reference mapping via kNN")
    assert not is_skip_heading("References are listed below")  # 非标题行


def test_prompt_masks_skipped_lines_and_states_budget():
    msgs = build_extraction_messages(
        PAPER,
        1,
        len(PAPER),
        min_chars=100,
        max_chars=1000,
        max_line_chars=500,
        profile=BIOMEDICAL,
    )
    user = msgs[1]["content"]
    assert "L3: TGFB1 regulates EMT." in user
    assert "L7: [略]" in user and "Smith" not in user
    assert "L9: [略]" in user and "NIH" not in user
    assert "L11: Cells were cultured." in user
    system = msgs[0]["content"]
    assert "锚点实体" in system and "aliases" in system and "最多 5 个实体" in system


def test_entity_cap_scales_and_clamps():
    assert entity_cap_for(0) == 5
    assert entity_cap_for(4000) == 10
    assert entity_cap_for(10**6) == 20


def test_parse_merged_reads_and_cleans_aliases():
    merged = parse_merged(
        {
            "chunk_markers": [],
            "entities": [
                {
                    "name": "Epithelial-to-mesenchymal transition",
                    "type": "concept",
                    "aliases": ["EMT", "emt", "", " EMT ", "epithelial to mesenchymal transition"],
                }
            ],
            "relations": [],
        },
        batch_start=1,
        batch_end=3,
        profile=BIOMEDICAL,
    )
    # 去空、去与 name 归一后相同者、去重
    assert merged.entities[0].aliases == ["EMT"]


def test_sanitize_drops_isolated_entities_only_when_asked():
    raw = {
        "chunk_markers": [],
        "entities": [
            {"name": "TGFB1", "type": "gene_protein"},
            {"name": "EMT", "type": "concept"},
            {"name": "Lonely", "type": "concept"},
        ],
        "relations": [{"head": "TGFB1", "tail": "EMT", "type": "REGULATES"}],
    }
    merged = parse_merged(raw, batch_start=1, batch_end=2, profile=BIOMEDICAL)
    kept, rels = sanitize_extraction(merged.entities, merged.relations)
    assert {e.name for e in kept} == {"TGFB1", "EMT", "Lonely"} and len(rels) == 1
    kept, rels = sanitize_extraction(merged.entities, merged.relations, drop_isolated=True)
    assert {e.name for e in kept} == {"TGFB1", "EMT"} and len(rels) == 1


def test_author_information_headings_are_masked():
    # 收窄 Declaration 后补回：作者信息/简介类小节只含作者、单位、邮箱，不是可抽取内容
    for heading in (
        "## Authors' information",
        "## Authors’ information",
        "## Author information",
        "## Author Information",
        "## Authors' details",
        "## About the authors",
        "## 作者简介",
        "## 作者信息",
        "### Author contributions",
        "## 3. Author information",
    ):
        assert is_skip_heading(heading), heading
    # 不误伤正文标题
    for heading in (
        "## Author attribution methods",
        "## Authors of the dataset are anonymized",
        "## Information extraction",
        "## Authorship verification",
    ):
        assert not is_skip_heading(heading), heading


def test_author_information_section_masked_in_prompt_only_when_back_matter_skipped():
    from loomvec.core.graph.profiles import ACADEMIC, GENERAL
    from loomvec.core.pipeline.extraction import build_extraction_messages

    lines = [
        "# Title",  # 1
        "## Results",  # 2
        "We trained a model.",  # 3
        "## Authors' information",  # 4
        "Jane Doe is at Example University.",  # 5
    ]

    def user_text(profile) -> str:
        msgs = build_extraction_messages(
            lines, 1, len(lines), min_chars=10, max_chars=1000, max_line_chars=500, profile=profile
        )
        return msgs[1]["content"]

    assert "L5: [略]" in user_text(ACADEMIC) and "Jane Doe" not in user_text(ACADEMIC)
    assert "L5: Jane Doe is at Example University." in user_text(GENERAL)


def test_skip_heading_tolerates_numeric_prefix_and_statement_suffix():
    for title in (
        "# 6 References",
        "# 6.1 Data availability",
        "## 6.Funding",
        "# Data Availability Statement",
        "# Code availability statement",
        "# Funding and acknowledgements",
        "# Acknowledgements and Disclosure of Funding",
        "# Competing interests statement",
        "# Conflict of Interest Statement",
        "# Ethical Approval",
        "# Ethics approval and consent to participate",
        "# Ethics statement",
        "## VII. 致谢",
    ):
        assert is_skip_heading(title), title


def test_skip_heading_keeps_body_sections_with_similar_words():
    # 伦理学论文的正文章节、数字开头的普通标题、含关键词的方法标题都不能误屏蔽
    for title in (
        "# Ethics",
        "# 3 Ethics",
        "# Ethics and AI",
        "# Ethical considerations in machine learning",
        "# 2024 Annual report",
        "# 1.2 Methods",
        "# IV drip protocol",
        "# 6 Funding mechanisms in public health",
        "# Data availability in cloud systems",
    ):
        assert not is_skip_heading(title), title
