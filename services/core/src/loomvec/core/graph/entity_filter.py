"""实体/关系噪音过滤（确定性规则，抽取入库前与存量审计共用）。

prompt 里已明令排除的噪音，LLM 不保证遵守，这里再做一道确定性兜底。规则分两层：

- **通用规则**（任何领域配置都生效，只收确定无误的拦截）：实体名过长（整句/标题）、
  "et al" 引文式署名的 person、描述明确为「本文作者/通讯作者/致谢对象/作者单位/
  本文参考文献条目/章节标题」的实体——这些只是文档自身的署名与结构信息；
- **论文元数据词表**（领域配置 `academic_noise=True` 时追加，即 academic / biomedical）：
  缩写署名（`N.C.` / `JFK` / `Nielsen,M.`；法律文书里匿名当事人「A」是正规实体，故不进通用层），
  以及描述里带「作者/员工/创始人/研究者/资助/供应商/厂商/被引/期刊」等词即丢弃。
  这些词在论文里几乎只用于作者、单位、基金、试剂厂商与被引文献；在法律、商业等文档中
  员工、供应商、引用恰是核心实体与关系，因此不进通用层。

关系拦截集合由领域配置的 `blocked_relations` 给出（`is_blocked_relation`）。
规则保守：宁可漏过也不误杀（只看类型 + 描述/名称的强信号，不按领域词表判断实体本身）。
`noise_reason` 返回命中的规则名（None=保留），审计脚本按规则名分桶统计。
"""

from __future__ import annotations

import re

from loomvec.core.graph.profiles import ExtractionProfile

# 实体名过长基本是整句/论文标题（正常实体名远短于此）
MAX_ENTITY_NAME_CHARS = 120

# "Perez et al." —— 引文式署名（通用层）
_PERSON_CITATION_RE = re.compile(r"\bet\s+al\b")
# "N.C." / "G.B." / "JFK" / "Nielsen,M." —— 缩写署名（仅论文配置：其它领域里 A、B 可以是匿名当事人）
_PERSON_INITIALS_RE = re.compile(
    r"^[A-Z]\.?\s?[A-Z]\.?$|^[A-Z]{1,3}$|^[A-Z][A-Za-z\-]+,\s?[A-Z]\.?$"
)

# ---- 通用层：描述明确指向「本文档自身」的署名与结构信息 ----
_PERSON_ROLE_RE = re.compile(
    r"本文作者|本文的作者|论文作者|通讯作者|致谢|corresponding author|acknowledg",
    re.IGNORECASE,
)
_ORG_ROLE_RE = re.compile(r"作者单位|作者所属单位|author affiliation", re.IGNORECASE)
# 「参考文献」只在描述明确说该实体是「本文参考文献列表里的条目」时才算噪音；单独出现这个词
# 或「参考文献中」不算（如《GB/T 7714-2015 信息与文献 参考文献著录规则》这类讲参考文献的标准
# 是正规实体）。更宽的「参考文献/被引/引用论文」词表只在论文配置（academic_noise）下启用。
_REFERENCE_RE = re.compile(
    r"cited in the references|(?:本文|文末|文后|该文)的?参考文献|^参考文献(?:列表|条目)|"
    r"^section (?:heading|of the paper)\b|^章节|节标题",
    re.IGNORECASE,
)

# ---- 论文元数据层（academic_noise）：作者/员工/资助/厂商/被引/期刊等描述词 ----
_ACADEMIC_PERSON_ROLE_RE = re.compile(
    r"作者|通讯|致谢|资助|创始人|联合创始|首席|员工|雇员|研究者|"
    r"\bauthors?\b|corresponding|affiliat|co-?founder|\bfounder\b|\bemployee\b|"
    r"acknowledg|funded by|\binvestigator\b",
    re.IGNORECASE,
)
_ACADEMIC_ORG_ROLE_RE = re.compile(
    r"作者单位|所属单位|隶属|affiliation|资助|funder|funding|sponsor|supplier|vendor|"
    r"manufacturer|制造商|生产厂|厂商|供应商|供应方|提供方|捐助",
    re.IGNORECASE,
)
_ACADEMIC_REFERENCE_RE = re.compile(
    r"cited in the references|参考文献|引用的?论文|被引|"
    r"^paper (?:title|on|about)\b|^论文[（(：:]|的论文[。.]?$|"
    r"^section (?:heading|of the paper)\b|^章节|节标题|"
    r"^(?:journal|scientific journal)\b|^期刊",
    re.IGNORECASE,
)

# 参考文献/章节标题规则只看这些「容器型」类型（具体对象类型如基因、疾病不会是文献或标题）
_REFERENCE_PRONE_TYPES = frozenset({"other", "concept", "product", "method", "dataset", "work"})


def noise_reason(
    name: str, entity_type: str, description: str | None, *, profile: ExtractionProfile
) -> str | None:
    """命中返回规则名，否则 None。entity_type 须为规范化类型（ontology.canonical_type）。"""
    name = (name or "").strip()
    desc = (description or "").strip()
    academic = profile.academic_noise
    if len(name) > MAX_ENTITY_NAME_CHARS:
        return "name_too_long"
    if entity_type == "person":
        if _PERSON_CITATION_RE.search(name) or (academic and _PERSON_INITIALS_RE.search(name)):
            return "person_citation_or_initials"
        if (_ACADEMIC_PERSON_ROLE_RE if academic else _PERSON_ROLE_RE).search(desc):
            return "person_author_or_staff"
    if entity_type == "organization" and (
        _ACADEMIC_ORG_ROLE_RE if academic else _ORG_ROLE_RE
    ).search(desc):
        return "org_affiliation_funder_vendor"
    if entity_type in _REFERENCE_PRONE_TYPES and (
        _ACADEMIC_REFERENCE_RE if academic else _REFERENCE_RE
    ).search(desc):
        return "reference_or_section"
    return None


def is_blocked_relation(rel_type: str, *, profile: ExtractionProfile) -> bool:
    return rel_type in profile.blocked_set
