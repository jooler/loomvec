"""论文的「不可抽取区段」识别：参考文献 / 致谢 / 资助 / 作者贡献 / 利益声明 / 数据可用性等。

这些区段只含引文、人名、机构、基金号、登录号，对跨论文知识聚合没有价值，却是图谱噪音的
主要来源（作者、单位、资助方、被引论文标题）。送 LLM 抽取前用本模块圈出这些行，
抽取 prompt 里把它们替换成占位符——分片仍覆盖全部行号，但不再在其上抽实体/关系。

这是学术论文的体裁规则：只有抽取领域配置 `skip_back_matter=True`（academic / biomedical）
时才启用（见 pipeline/extraction.build_extraction_messages）；一般文档的 Additional information、
法律文书的「声明」等章节照常抽取。

规则只看标题（`#` 开头的行）：命中「跳过标题」后，区段延续到下一个**非**跳过标题之前
（连续的跳过标题，如 References 之后紧跟 Acknowledgements，合并成一段）；
正文里的普通段落永远不会被误判。
"""

from __future__ import annotations

import re

from loomvec.core.pipeline.chunking import heading_title, is_heading

# 标题归一后命中任一模式即视为「不可抽取」。刻意用整词/前缀锚定，避免
# "Benchmarking against competing methods" 这类含 competing 的方法标题被误伤；
# 「声明」只认利益冲突 / 生成式 AI 使用声明，单独的 "Declarations" 标题不算（其下的
# Funding、Competing interests 等子标题各自命中）；单独的 "Ethics" 标题也不算（伦理学
# 论文的正文章节），只认 Ethics statement / Ethical approval 等声明性标题。
_SKIP_TITLE_RE = re.compile(
    r"^(?:"
    r"references?(?: and notes)?|bibliography|参考文献|"
    r"acknowledge?ments?(?: and (?:disclosure of )?funding)?|致谢|"
    r"funding(?: statement| and acknowledge?ments?)?|financial support|"
    r"author contributions?|authors?[’']? contributions?|作者贡献|"
    r"authors?[’']? (?:information|details|biograph(?:y|ies)|notes?)|"
    r"about the authors?|作者简介|作者信息|"
    r"competing (?:financial )?interests?(?: statement)?|"
    r"conflicts? of interests?(?: statement)?|"
    r"declarations? of (?:competing )?interests?|"
    r"declaration of (?:generative ai|llm).*|"
    r"ethics (?:statement|oversight|declarations?)|ethical statement|inclusion and ethics|"
    r"ethic(?:s|al) approval(?: and consent to participate)?|consent for publication|"
    r"data(?: and code)? availability(?: statement)?|code availability(?: statement)?|"
    r"availability of data and materials|"
    r"reporting summary|additional information|"
    r"supplement(?:ary|al)? (?:information|materials?|data|figures?)|"
    r"extended data(?: figures?| tables?)?"
    r")\s*[.:：]?$",
    re.IGNORECASE,
)

SKIPPED_LINE_PLACEHOLDER = "[略]"


# 标题编号前缀："6. References" / "6 References" / "6.1 Data availability" / "VII. 致谢"
# （阿拉伯数字后须有标点或空白；罗马数字须带标点，避免吃掉 "IV drip" 之类的词）
_NUMBER_PREFIX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*(?:[.)、]\s*|\s+)|[IVXivx]+[.)、]\s*)")


def _norm_title(title: str) -> str:
    t = _NUMBER_PREFIX_RE.sub("", title.strip())
    return re.sub(r"\s+", " ", t).strip()


def is_skip_heading(line: str) -> bool:
    title = heading_title(line) if is_heading(line) else None
    return bool(title) and bool(_SKIP_TITLE_RE.match(_norm_title(title)))


def find_skip_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """返回不可抽取区段的 1-based 闭区间行号 [(start, end), ...]（含标题行本身）。"""
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    for n, line in enumerate(lines, start=1):
        if not is_heading(line):
            continue
        if is_skip_heading(line):
            if start is None:
                start = n
        elif start is not None:
            ranges.append((start, n - 1))
            start = None
    if start is not None:
        ranges.append((start, len(lines)))
    return ranges


def skipped_line_set(ranges: list[tuple[int, int]]) -> set[int]:
    out: set[int] = set()
    for s, e in ranges:
        out.update(range(s, e + 1))
    return out
