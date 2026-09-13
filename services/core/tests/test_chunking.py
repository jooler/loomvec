"""P1-WRK-03 分片逻辑单元测试：marker 校验、表格独立、钳制、结构化兜底。"""

from __future__ import annotations

import pytest

from loomvec.core.ai import AiJsonError
from loomvec.core.db.models import ChunkMethod
from loomvec.core.pipeline.chunking import (
    build_chunk_messages,
    detect_table_ranges,
    parse_markers,
    split_batches,
    structural_fallback,
    validate_markers,
)


def _lines(n: int) -> list[str]:
    return [f"第{i}行内容，语义检索测试文本。" for i in range(1, n + 1)]


def test_detect_table_ranges_multiline():
    lines = ["a", "<table>", "<tr><td>x</td></tr>", "</table>", "b"]
    ranges = detect_table_ranges(lines)
    assert len(ranges) == 1
    assert (ranges[0].start_line, ranges[0].end_line) == (2, 4)
    assert "<td>x</td>" in ranges[0].html


def test_validate_markers_merges_gaps_and_covers_batch():
    lines = _lines(60)
    markers = [
        {"start_line": 1, "end_line": 20, "title": "A", "keywords": ["k1"]},
        {"start_line": 30, "end_line": 40, "title": "B", "keywords": []},
    ]
    drafts = validate_markers(
        markers, lines, batch_start=1, batch_end=60, tables=[], min_chars=10, max_chars=10_000
    )
    # 不重叠、覆盖全部行（间隙并入前一 chunk）
    assert drafts[0].start_line == 1
    assert drafts[-1].end_line == 60
    for i in range(len(drafts) - 1):
        a, b = drafts[i], drafts[i + 1]
        assert a.end_line < b.start_line
    assert all(d.method == ChunkMethod.LLM_MARKERS for d in drafts)


def test_validate_markers_table_excluded_and_independent():
    lines = _lines(10)
    lines[4] = "<table><tr><td>Q3 营收</td></tr></table>"
    tables = detect_table_ranges(lines)
    markers = [{"start_line": 1, "end_line": 10, "title": "全文", "keywords": []}]
    drafts = validate_markers(
        markers, lines, batch_start=1, batch_end=10, tables=tables, min_chars=10, max_chars=10_000
    )
    table_drafts = [d for d in drafts if d.is_table]
    text_drafts = [d for d in drafts if not d.is_table]
    assert len(table_drafts) == 1 and table_drafts[0].start_line == 5
    for d in text_drafts:
        assert not (d.start_line <= 5 <= d.end_line)


def test_validate_markers_oversize_split_creates_parent_child():
    lines = [f"段落 {i}。" + "长" * 50 for i in range(1, 20)]
    markers = [{"start_line": 1, "end_line": 19, "title": "大段", "keywords": []}]
    drafts = validate_markers(
        markers, lines, batch_start=1, batch_end=19, tables=[], min_chars=10, max_chars=200
    )
    assert len(drafts) > 1
    parents = [d for d in drafts if d.parent_key is None]
    children = [d for d in drafts if d.parent_key is not None]
    assert parents and children
    child_keys = {d.parent_key for d in children}
    assert child_keys <= {d.key for d in parents}


def test_structural_fallback_by_heading():
    lines = ["# 章一", *_lines(10), "# 章二", *_lines(10)]
    drafts = structural_fallback(
        lines,
        batch_start=1,
        batch_end=len(lines),
        tables=[],
        min_chars=10,
        max_chars=10_000,
    )
    assert all(d.method == ChunkMethod.STRUCTURAL_FALLBACK for d in drafts)
    assert drafts[0].start_line == 1 and drafts[-1].end_line == len(lines)


def test_parse_markers_rejects_garbage():
    with pytest.raises(AiJsonError):
        parse_markers("not json at all")
    with pytest.raises(AiJsonError):
        # 非法区间（end < start）判废后为空 → 抛错由调用方兜底
        parse_markers('{"chunk_markers": [{"start_line": 3, "end_line": 1}]}')


def test_parse_markers_coerces_and_drops_invalid():
    raw = (
        '{"chunk_markers": [{"start_line": "2", "end_line": "5", "title": "T", "keywords": ["a"]},'
        ' {"bad": 1}]}'
    )
    markers = parse_markers(raw)
    assert len(markers) == 1
    assert markers[0]["start_line"] == 2 and markers[0]["title"] == "T"


def test_build_chunk_messages_contains_control_tags():
    lines = _lines(20)
    msgs = build_chunk_messages(lines, 1, 20, min_chars=100, max_chars=800)
    user = msgs[1]["content"]
    assert "[LOOMVEC_TASK=chunk_markers]" in user
    assert "[DOC_START_LINE=1]" in user and "[DOC_END_LINE=20]" in user
    assert "L20:" in user


def test_split_batches_aligned_to_headings():
    lines = ["# A", *_lines(10), "# B", *_lines(10)]
    batches = split_batches(lines, 100)
    assert batches[0][0] == 1
    assert batches[-1][1] == len(lines)
    # 边界连续无重叠
    for i in range(len(batches) - 1):
        assert batches[i + 1][0] == batches[i][1] + 1


def test_draft_char_count_clamp_min_merge():
    lines = ["短"] * 6
    markers = [
        {"start_line": 1, "end_line": 2, "title": "小", "keywords": []},
        {"start_line": 3, "end_line": 4, "title": "小", "keywords": []},
        {"start_line": 5, "end_line": 6, "title": "小", "keywords": []},
    ]
    drafts = validate_markers(
        markers, lines, batch_start=1, batch_end=6, tables=[], min_chars=100, max_chars=1000
    )
    # 过小片段与相邻合并
    assert len(drafts) == 1
