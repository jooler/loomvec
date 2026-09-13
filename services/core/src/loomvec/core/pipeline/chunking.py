"""P1-WRK-03 LLM 语义分片：marker 契约、确定性校验/钳制、结构化兜底（03 文档 §二）。

设计要点：
- 不使用固定长度分片：LLM 决定边界、应用确定性执行（不信任 LLM 的位置输出）；
- 行号坐标系：MinerU Markdown 按 `\n` 切行，行号 1-based；
- 表格 HTML 块整体独立成语义单元，不参与 marker 切分；
- 尺寸钳制：超大 chunk 按段落边界二次切分（父子块关系），过小与相邻合并；
- 校验失败/LLM 超时 → 结构化兜底分片，chunk_method=structural_fallback；
- prompt 头部带 LOOMVEC 控制标记（mock 网关据此确定性生成 markers，见 core/ai.py）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from loomvec.core.ai import AiJsonError, extract_json
from loomvec.core.db.models import ChunkMethod

_HEADING_RE = re.compile(r"^(#{1,6})\s+")


@dataclass
class ChunkDraft:
    """一个待入库的语义单元草稿（行区间闭区间，1-based）。"""

    start_line: int
    end_line: int
    title: str | None = None
    keywords: list[str] = field(default_factory=list)
    unit_type: str = "text"  # text / table
    method: ChunkMethod = ChunkMethod.LLM_MARKERS
    # 父子块：本 draft 是由 parent 拆出的子块时记录父 draft 的序号
    parent_key: int | None = None
    key: int = -1

    @property
    def is_table(self) -> bool:
        return self.unit_type == "table"


@dataclass
class TableRange:
    start_line: int
    end_line: int
    html: str


# ---------------------------------------------------------------------------
# 文档结构探测
# ---------------------------------------------------------------------------


def detect_table_ranges(lines: list[str]) -> list[TableRange]:
    """扫描 <table ... </table> HTML 块（MinerU Markdown 中表格为 HTML）。"""
    ranges: list[TableRange] = []
    start: int | None = None
    buffer: list[str] = []
    for i, line in enumerate(lines, start=1):
        if start is None:
            if "<table" in line.lower():
                start, buffer = i, [line]
        else:
            buffer.append(line)
        if start is not None and "</table>" in "\n".join(buffer).lower():
            ranges.append(TableRange(start, i, "\n".join(buffer)))
            start, buffer = None, []
    if start is not None and buffer:  # 未闭合：视为普通文本，不产出表格
        pass
    return ranges


def is_heading(line: str) -> bool:
    return bool(_HEADING_RE.match(line))


def heading_title(line: str) -> str | None:
    m = _HEADING_RE.match(line)
    return line[m.end() :].strip() if m else None


def split_batches(lines: list[str], max_chars: int) -> list[tuple[int, int]]:
    """按章节边界把全文切成 ≤ max_chars 的批次（供分批调用 LLM）。"""
    batches: list[tuple[int, int]] = []
    start = 1
    chars = 0
    for i, line in enumerate(lines, start=1):
        # 章节边界（一级标题）优先开新批次
        if chars >= max_chars or (chars > 0 and is_heading(line) and line.startswith("# ")):
            batches.append((start, i - 1))
            start, chars = i, 0
        chars += len(line) + 1
    if start <= len(lines):
        batches.append((start, len(lines)))
    return batches


def build_chunk_messages(
    lines: list[str], start: int, end: int, *, min_chars: int, max_chars: int
) -> list[dict[str, str]]:
    body = "\n".join(f"L{n}: {lines[n - 1]}" for n in range(start, end + 1))
    system = (
        "你是文档语义分片引擎。根据给定的带行号文档行，输出语义完整的分片边界 JSON。"
        '输出格式（仅输出 JSON）：{"chunk_markers": ['
        '{"start_line": 12, "end_line": 47, "title": "片段标题", "keywords": ["关键词"]}]}. '
        f"规则：1) 分片按语义边界（标题/段落/主题切换）；2) 单片长度 {min_chars}~{max_chars} 字符，"
        "过长按主题拆分，过短并入相邻片段；3) markers 覆盖全部给定行、区间不重叠、"
        "行号单调递增；4) 不要把一个表格拆到两个片段。"
    )
    user = (
        f"[LOOMVEC_TASK=chunk_markers]\n[DOC_START_LINE={start}]\n[DOC_END_LINE={end}]\n"
        "文档行（L{行号} 表示该行内容）：\n"
        f"{body}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# marker 校验与钳制（确定性，不信任 LLM 位置输出）
# ---------------------------------------------------------------------------


def parse_markers(raw: str | dict) -> list[dict]:
    """从 LLM 输出（JSON 串或已解析对象）提取 chunk_markers；非法抛 AiJsonError。"""
    data = raw if isinstance(raw, dict) else extract_json(raw)
    markers = data.get("chunk_markers")
    if not isinstance(markers, list):
        raise AiJsonError("缺少 chunk_markers 数组")
    cleaned: list[dict] = []
    for m in markers:
        if not isinstance(m, dict):
            continue
        try:
            start, end = int(m["start_line"]), int(m["end_line"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:  # 非法区间直接判废（调用方兜底结构化分片）
            continue
        title = str(m.get("title") or "").strip() or None
        keywords = [str(k) for k in (m.get("keywords") or []) if str(k).strip()][:10]
        cleaned.append({"start_line": start, "end_line": end, "title": title, "keywords": keywords})
    if not cleaned:
        raise AiJsonError("chunk_markers 为空或全部非法")
    return cleaned


def _subtract_tables(start: int, end: int, tables: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, end] 去除表格行区间后的文本子区间。"""
    segments = [(start, end)]
    for ts, te in tables:
        nxt: list[tuple[int, int]] = []
        for s, e in segments:
            if te < s or ts > e:
                nxt.append((s, e))
                continue
            if s < ts:
                nxt.append((s, min(ts - 1, e)))
            if e > te:
                nxt.append((max(te + 1, s), e))
        segments = [(s, e) for s, e in nxt if s <= e]
    return segments


def _split_oversize(
    lines: list[str], start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    """超大区间按段落（空行）边界二次切分；单段仍超长则按行硬切。"""
    pieces: list[tuple[int, int]] = []
    cur_start = start
    chars = 0
    last_para_break = start - 1
    for n in range(start, end + 1):
        chars += len(lines[n - 1]) + 1
        if not lines[n - 1].strip():
            last_para_break = n
        if chars >= max_chars and n < end:
            cut = last_para_break if last_para_break >= cur_start else n
            pieces.append((cur_start, cut))
            cur_start, chars, last_para_break = cut + 1, 0, cut
    pieces.append((cur_start, end))
    return [(s, e) for s, e in pieces if e >= s]


def validate_markers(
    markers: list[dict],
    lines: list[str],
    *,
    batch_start: int,
    batch_end: int,
    tables: list[TableRange],
    min_chars: int,
    max_chars: int,
    method: ChunkMethod = ChunkMethod.LLM_MARKERS,
) -> list[ChunkDraft]:
    """marker → 合法 ChunkDraft 列表：校验/并间隙/扣表格/钳制尺寸。"""
    table_spans = [(t.start_line, t.end_line) for t in tables]

    # 1) 行号合法化：夹取到批次内、单调、去重叠（允许间隙，间隙并入前一 chunk）
    valid = sorted(
        (
            {
                "start_line": max(batch_start, m["start_line"]),
                "end_line": min(batch_end, m["end_line"]),
                "title": m["title"],
                "keywords": m["keywords"],
            }
            for m in markers
            if m["end_line"] >= m["start_line"]
        ),
        key=lambda m: m["start_line"],
    )
    merged: list[dict] = []
    for m in valid:
        if merged and m["start_line"] <= merged[-1]["end_line"]:
            # 重叠：截断并入前一 marker
            prev = merged[-1]
            prev["end_line"] = max(prev["end_line"], m["end_line"])
            prev["keywords"] = list(dict.fromkeys(prev["keywords"] + m["keywords"]))[:10]
        else:
            if merged and m["start_line"] > merged[-1]["end_line"] + 1:
                # 间隙内容并入前一 chunk（03 文档 §二.1），紧邻 marker 保留为独立片
                merged[-1]["end_line"] = m["start_line"] - 1
            merged.append(dict(m))
    if not merged:
        return []
    merged[0]["start_line"] = batch_start  # 首个 marker 向上补齐批次起点
    merged[-1]["end_line"] = batch_end

    # 2) 扣除表格区间（表格独立成片）+ 3) 尺寸钳制
    drafts: list[ChunkDraft] = []
    serial = 0
    for m in merged:
        title, keywords = m["title"], m["keywords"]
        for seg_start, seg_end in _subtract_tables(m["start_line"], m["end_line"], table_spans):
            chars = sum(len(lines[n - 1]) + 1 for n in range(seg_start, seg_end + 1))
            if chars > max_chars:
                pieces = _split_oversize(lines, seg_start, seg_end, max_chars)
                parent_key = serial  # 首片充当父块，其余为子块
                for j, (s, e) in enumerate(pieces):
                    drafts.append(
                        ChunkDraft(
                            s,
                            e,
                            title=title if j == 0 else (f"{title}（续{j}）" if title else None),
                            keywords=keywords if j == 0 else [],
                            method=method,
                            parent_key=None if j == 0 else parent_key,
                            key=serial,
                        )
                    )
                    serial += 1
            else:
                drafts.append(
                    ChunkDraft(
                        seg_start,
                        seg_end,
                        title=title,
                        keywords=keywords,
                        method=method,
                        key=serial,
                    )
                )
                serial += 1

    # 表格独立成片：批次内表格区间整块产出（不参与 marker 切分）
    for t in tables:
        if t.start_line >= batch_start and t.end_line <= batch_end:
            drafts.append(
                ChunkDraft(t.start_line, t.end_line, unit_type="table", method=method, key=serial)
            )
            serial += 1
    drafts.sort(key=lambda d: (d.start_line, d.end_line))

    # 过小合并（text 且与前一同为 text、中间不隔表格）
    merged_drafts: list[ChunkDraft] = []
    for d in drafts:
        if (
            merged_drafts
            and not d.is_table
            and not merged_drafts[-1].is_table
            and _char_count(lines, merged_drafts[-1]) < min_chars
            and not _table_between(merged_drafts[-1].end_line, d.start_line, table_spans)
        ):
            prev = merged_drafts[-1]
            prev.end_line = max(prev.end_line, d.end_line)
            prev.keywords = list(dict.fromkeys(prev.keywords + d.keywords))[:10]
        else:
            merged_drafts.append(d)
    return merged_drafts


def _table_between(a: int, b: int, table_spans: list[tuple[int, int]]) -> bool:
    """表格严格位于 (a, b) 之间（合并会跨越它）。"""
    return any(a < ts and te < b for ts, te in table_spans)


def _char_count(lines: list[str], d: ChunkDraft) -> int:
    return sum(len(lines[n - 1]) + 1 for n in range(d.start_line, d.end_line + 1))


def structural_fallback(
    lines: list[str],
    *,
    batch_start: int,
    batch_end: int,
    tables: list[TableRange],
    min_chars: int,
    max_chars: int,
) -> list[ChunkDraft]:
    """结构化兜底分片：按标题层级切节，节内按段落钳制，过小并入相邻。"""
    sections: list[tuple[int, int, str | None]] = []
    cur_start, cur_title = batch_start, None
    for n in range(batch_start, batch_end + 1):
        title = heading_title(lines[n - 1])
        if title and n > cur_start:
            sections.append((cur_start, n - 1, cur_title))
            cur_start, cur_title = n, title
    sections.append((cur_start, batch_end, cur_title))

    table_spans = [(t.start_line, t.end_line) for t in tables]
    drafts: list[ChunkDraft] = []
    serial = 0
    for s, e, title in sections:
        for seg_start, seg_end in _subtract_tables(s, e, table_spans):
            if _char_count(lines, ChunkDraft(seg_start, seg_end)) > max_chars:
                pieces = _split_oversize(lines, seg_start, seg_end, max_chars)
                parent_key = serial  # 首片充当父块，其余为子块
                for j, (ps, pe) in enumerate(pieces):
                    drafts.append(
                        ChunkDraft(
                            ps,
                            pe,
                            title=title if j == 0 else (f"{title}（续{j}）" if title else None),
                            method=ChunkMethod.STRUCTURAL_FALLBACK,
                            parent_key=None if j == 0 else parent_key,
                            key=serial,
                        )
                    )
                    serial += 1
            else:
                drafts.append(
                    ChunkDraft(
                        seg_start,
                        seg_end,
                        title=title,
                        method=ChunkMethod.STRUCTURAL_FALLBACK,
                        key=serial,
                    )
                )
                serial += 1
    # 表格独立成片（与 validate_markers 同规则）
    for t in tables:
        if t.start_line >= batch_start and t.end_line <= batch_end:
            drafts.append(
                ChunkDraft(
                    t.start_line,
                    t.end_line,
                    unit_type="table",
                    method=ChunkMethod.STRUCTURAL_FALLBACK,
                    key=serial,
                )
            )
            serial += 1
    drafts.sort(key=lambda d: (d.start_line, d.end_line))

    # 过小合并（与 validate_markers 尾部同规则；中间不隔表格）
    merged: list[ChunkDraft] = []
    for d in drafts:
        if (
            merged
            and not d.is_table
            and not merged[-1].is_table
            and _char_count(lines, merged[-1]) < min_chars
            and not _table_between(merged[-1].end_line, d.start_line, table_spans)
        ):
            merged[-1].end_line = max(merged[-1].end_line, d.end_line)
        else:
            merged.append(d)
    return merged


def strip_html(html: str, limit: int = 8000) -> str:
    """表格 HTML → 纯文本（用于嵌入输入）。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]
