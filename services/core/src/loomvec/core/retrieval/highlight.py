"""检索结果高亮：查询词在正文的首个命中邻域，<em> 包裹。

分词策略：空白分词优先；中文整句不命中时回退为含 CJK 的 2-gram
（英文词已由空白分词覆盖，纯 ASCII bigram 会误伤英文正文）。
"""

from __future__ import annotations

import re


def _query_terms(query: str, text: str, max_terms: int) -> list[str]:
    terms = [t for t in re.split(r"\s+", query.strip()) if len(t) >= 2]
    if any(t in text for t in terms):
        return terms[:max_terms]
    compact = re.sub(r"\s+", "", query)
    grams = [compact[i : i + 2] for i in range(len(compact) - 1)]
    grams = [g for g in grams if any("\u4e00" <= ch <= "\u9fff" for ch in g)]
    return [g for g in dict.fromkeys(grams) if g in text][:max_terms]


def make_highlight(query: str, text: str, *, radius: int = 80, max_terms: int = 5) -> str | None:
    """朴素高亮：查询词/2-gram 在正文的首个命中邻域，<em> 包裹命中片段。"""
    terms = _query_terms(query, text, max_terms)
    if not terms:
        return None
    positions: list[tuple[int, int]] = []
    for term in terms:
        start = 0
        for _ in range(20):
            idx = text.find(term, start)
            if idx < 0:
                break
            positions.append((idx, idx + len(term)))
            start = idx + len(term)
    if not positions:
        return None
    first = min(p[0] for p in positions)
    last = max(p[1] for p in positions)
    snippet_start = max(0, first - radius // 2)
    snippet_end = min(len(text), last + radius)
    snippet = text[snippet_start:snippet_end]
    for term in sorted({*terms}, key=len, reverse=True):
        snippet = snippet.replace(term, f"<em>{term}</em>")
    prefix = "…" if snippet_start > 0 else ""
    suffix = "…" if snippet_end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"
