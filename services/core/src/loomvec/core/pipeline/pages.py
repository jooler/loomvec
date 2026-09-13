"""解析产物页码定位：content_list 版面块 → Markdown 行号映射 + 清洗工具。"""

from __future__ import annotations

import re
from typing import Any

from loomvec.core.pipeline.chunking import ChunkDraft


def sanitize_text(text: str) -> str:
    """清洗 C0 控制字符（保留 \\n \\t）：PDF 解析产物可能混入 NUL，PG text 拒绝 0x00。"""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def draft_text(lines: list[str], draft: ChunkDraft) -> str:
    return "\n".join(lines[draft.start_line - 1 : draft.end_line])


def build_line_page_map(markdown: str, content_list: list[dict[str, Any]]) -> dict[int, dict]:
    """把 content_list 版面块按顺序匹配回 Markdown 行，得到 {行号: {page, bbox}}。

    MinerU 的 md 由 content_list 块顺序生成，顺序游标匹配即可覆盖绝大多数块；
    未命中的块跳过，其页码由相邻命中块插值。返回行号 1-based。
    """
    # 块文本在“去空白规范串”中定位；行偏移也在同一坐标系累计，映射精确
    norm_lines = [re.sub(r"\s+", "", ln) for ln in markdown.split("\n")]
    line_offsets: list[int] = []
    pos = 0
    for ln in norm_lines:
        line_offsets.append(pos)
        pos += len(ln)
    total_lines = len(line_offsets)

    def char_to_line(char_pos: int) -> int:
        lo, hi = 0, total_lines - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_offsets[mid] <= char_pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    md_norm = "".join(norm_lines)
    matches: list[tuple[int, dict]] = []  # (起始行号, block)
    cursor = 0
    for block in content_list:
        text = re.sub(r"\s+", "", str(block.get("text") or block.get("table_body") or ""))
        if len(text) < 4:  # 过短匹配不可靠（图注等）
            continue
        idx = md_norm.find(text, cursor)
        if idx < 0:
            idx = md_norm.find(text)
        if idx < 0:
            continue
        cursor = idx + 1
        matches.append((char_to_line(idx), block))

    page_map: dict[int, dict] = {}
    last_page = 0
    mi = 0
    for line_no in range(1, total_lines + 1):
        while mi < len(matches) and matches[mi][0] <= line_no:
            block = matches[mi][1]
            page_map[line_no] = {
                "page": int(block.get("page_idx", 0)),
                **({"bbox": block["bbox"]} if block.get("bbox") else {}),
            }
            last_page = int(block.get("page_idx", 0))
            mi += 1
        if line_no not in page_map:
            page_map[line_no] = {"page": last_page}
    return page_map


def unit_locator(draft: ChunkDraft, page_map: dict[int, dict]) -> dict[str, Any]:
    pages = sorted(
        {page_map.get(n, {}).get("page", 0) for n in range(draft.start_line, draft.end_line + 1)}
    )
    bbox = next(
        (
            page_map[n].get("bbox")
            for n in range(draft.start_line, draft.end_line + 1)
            if page_map.get(n, {}).get("bbox")
        ),
        None,
    )
    locator: dict[str, Any] = {
        "start_line": draft.start_line,
        "end_line": draft.end_line,
        "pages": pages,
    }
    if bbox:
        locator["bbox"] = bbox
    return locator
