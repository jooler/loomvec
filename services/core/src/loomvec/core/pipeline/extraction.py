"""P3-CORE-02 合并抽取协议：分片 markers 与实体/关系单次 LLM 调用（03 文档 §3.2）。

一次调用返回两段结构，**独立校验、互不牵连**：
- markers 按行号规则校验（chunking.validate_markers），失效 → 结构化兜底分片；
- entities/relations 按名称非空、type 按领域配置白名单归并、head/tail 可解析校验，
  失效 → 该批没有实体关系产出，分片照常入库（批数记入 version_meta 的 extraction_failed_batches）；
- 关系归属 chunk：evidence_lines 与 chunk 行区间求交（重叠最多者），零对齐成本；
- 缓存键 (checksum, 解析器版本, 模型, prompt 版本, 分片策略, 抽取开关, 领域配置 key+指纹)，
  prompt 版本 chunk-graph-v5（config.PipelineSettings.prompt_version）。

prompt = 领域无关的底座（分片契约、跨文档「锚点实体」目标、每批数量预算、实体须参与关系、
aliases、命名与描述规则、只抽明确支持的关系）+ 领域配置插槽（graph/profiles：类型、
优先/不要抽、推荐与拦截关系、命名额外规则、示例）。配置开启 skip_back_matter 时，
参考文献/致谢等论文后置区段以 [略] 占位不送抽取。

控制标记协议：`[LOOMVEC_TASK=chunk_graph]`（mock 网关据此确定性生成两段结构）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from loomvec.core.ai import AiJsonError, extract_json
from loomvec.core.constants import DEFAULT_ENTITY_TYPE
from loomvec.core.graph.entity_filter import is_blocked_relation, noise_reason
from loomvec.core.graph.ontology import normalize_entity_name
from loomvec.core.graph.profiles import ExtractionProfile, profile_entity_type
from loomvec.core.pipeline.chunking import ChunkDraft, numbered_lines, parse_markers
from loomvec.core.pipeline.sections import find_skip_ranges, skipped_line_set

_TASK_TAG = "chunk_graph"

# 关系类型清洗：非字母数字压为下划线（中英混合保留 unicode 字母）
_REL_TYPE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")
_MAX_REL_TYPE_LEN = 64
_MAX_LINES_PER_MENTION = 32


@dataclass
class EntityMention:
    """批次内实体提及（行号锚定于全文坐标系）。"""

    name: str
    type: str = DEFAULT_ENTITY_TYPE
    description: str | None = None
    lines: list[int] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


@dataclass
class RelationMention:
    head: str
    tail: str
    type: str
    evidence_lines: list[int] = field(default_factory=list)
    # 抽取校验后由 attribute_relations 回填：归属 ChunkDraft.key
    chunk_key: int | None = None


@dataclass
class MergedExtraction:
    """单批合并抽取结果：markers 与 extraction 各自独立有效（None=该部分无效）。"""

    markers: list[dict] | None
    entities: list[EntityMention] | None
    relations: list[RelationMention] | None

    @property
    def extraction_ok(self) -> bool:
        return self.entities is not None and self.relations is not None

    @property
    def markers_ok(self) -> bool:
        return self.markers is not None


# 每批实体数上限：按本批「可抽取」字符量折算（约每 400 字 1 个），夹在 [5, 20]
_ENTITY_CHARS_PER_ITEM = 400
_ENTITY_CAP_RANGE = (5, 20)


# prompt 示例里各实体依次使用的行号区间（仅示意格式，不要求与示例关系的证据行对应）
_EXAMPLE_ENTITY_LINES = ([20, 44], [30, 35], [50, 52], [60, 61])


def entity_cap_for(extractable_chars: int) -> int:
    lo, hi = _ENTITY_CAP_RANGE
    return max(lo, min(hi, round(extractable_chars / _ENTITY_CHARS_PER_ITEM)))


def build_extraction_messages(
    lines: list[str],
    start: int,
    end: int,
    *,
    min_chars: int,
    max_chars: int,
    max_line_chars: int,
    profile: ExtractionProfile,
) -> list[dict[str, str]]:
    """合并抽取 prompt：分片契约（与 chunk-v1 相同）+ 跨文档「锚点实体」抽取契约。

    领域内容来自 profile；profile.skip_back_matter 时参考文献/致谢/声明等区段
    （pipeline.sections）的行以占位符 `[略]` 呈现：分片仍覆盖全部行号，但 LLM 看不到
    其内容，也就不会从中抽作者、单位、被引论文标题。
    """
    masked = skipped_line_set(find_skip_ranges(lines)) if profile.skip_back_matter else set()
    body = numbered_lines(lines, start, end, max_line_chars=max_line_chars, masked=masked)
    extractable = sum(len(lines[n - 1]) + 1 for n in range(start, end + 1) if n not in masked)
    cap = entity_cap_for(extractable)
    example_entities = ", ".join(
        json.dumps(
            {**ent, "lines": _EXAMPLE_ENTITY_LINES[i % len(_EXAMPLE_ENTITY_LINES)]},
            ensure_ascii=False,
        )
        for i, ent in enumerate(profile.example["entities"])
    )
    example_relation = json.dumps(
        {**profile.example["relation"], "evidence_lines": [30, 35]}, ensure_ascii=False
    )
    type_list = "；".join(f"{t}={label}" for t, label in profile.entity_types)
    masked_note = "（含 [略] 行）" if profile.skip_back_matter else ""
    exclude = "；".join(
        part
        for part in (
            "本文自造且只在本文使用的术语/变量/指标/缩写",
            "过于宽泛的通用词（data、model、method、system 等）",
            "章节/小节标题",
            "图表编号",
            "文档自身的页眉页脚、版权与出版信息",
            profile.exclude_hint,
            "标注 [略] 的行里的任何内容" if profile.skip_back_matter else "",
        )
        if part
    )
    name_rules = f"；{profile.name_rules}" if profile.name_rules else ""
    blocked = (
        f"不要抽 {'/'.join(profile.blocked_relations)} 等关系；"
        if profile.blocked_relations
        else ""
    )
    system = (
        "你是文档语义分片与知识图谱抽取引擎。根据给定的带行号文档行，输出 JSON，"
        "同时包含分片边界与实体关系两段结构。\n"
        "输出格式（仅输出 JSON，三个键都必须存在）：\n"
        '{"chunk_markers": [{"start_line": 12, "end_line": 47, "title": "片段标题", '
        '"keywords": ["关键词"]}],\n'
        f' "entities": [{example_entities}],\n'
        f' "relations": [{example_relation}]}}\n'
        f"分片规则：1) 按语义边界（标题/段落/主题切换）；2) 单片 {min_chars}~{max_chars} 字符，"
        f"过长按主题拆分，过短并入相邻；3) markers 覆盖全部给定行{masked_note}、区间不重叠、"
        "行号单调递增；4) 不要把一个表格拆到两个片段。\n"
        f"抽取目标：本文档属于「{profile.label}」，为**跨文档知识图谱**抽取「锚点实体」——"
        "换一份相关文档也会独立提到、能在多份文档之间对上号的对象。抽取规则：\n"
        f"1) type 只能取以下之一（小写）：{type_list}。优先抽：{profile.priority_hint}\n"
        f"2) **不要抽**：{exclude}。\n"
        f"3) **只输出参与了至少一条关系的实体**，没有关系的实体不要输出；本批最多 {cap} 个实体，"
        "宁缺毋滥，按对本批内容的重要性取舍。\n"
        f"4) name 用原文最常用的规范写法，不带括号注释{name_rules}；"
        "文中同时给出全称与缩写时 name 取全称、缩写放入 aliases；"
        "description 一句中文，不超过 40 字，说明它是什么。\n"
        "5) 关系 head/tail 必须是本批 entities 的 name；relation.type 优先从以下集合选："
        f"{'、'.join(profile.relation_types)}；集合表达不了才自拟大写下划线短语；{blocked}"
        "只抽文中明确支持的关系，不推断，不确定宁可不抽。\n"
        "6) lines/evidence_lines 用给定的 L 行号。"
    )
    user = (
        f"[LOOMVEC_TASK={_TASK_TAG}]\n[DOC_START_LINE={start}]\n[DOC_END_LINE={end}]\n"
        "文档行（L{行号} 表示该行内容）：\n"
        f"{body}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_merged(
    raw: str | dict, *, batch_start: int, batch_end: int, profile: ExtractionProfile
) -> MergedExtraction:
    """解析与独立校验两段结构；任一部分失效不抛异常（以 None 表示），互不牵连。

    profile 决定类型白名单、噪音规则层级与关系拦截集合。
    """
    data = raw if isinstance(raw, dict) else extract_json(raw)

    # ---- markers：复用 parse_markers，失败即 None（调用方结构化兜底）----
    try:
        markers: list[dict] | None = parse_markers(data)
    except AiJsonError:
        markers = None

    # ---- entities / relations：结构校验 + 字段清洗 ----
    entities, dropped = _parse_entities(
        data, batch_start=batch_start, batch_end=batch_end, profile=profile
    )
    relations = _parse_relations(
        data, batch_start=batch_start, batch_end=batch_end, profile=profile
    )
    if relations is not None and dropped:
        # 噪音实体被丢弃后，指向它们的关系一并去掉（media 路径不经 sanitize_extraction）
        relations = [
            r
            for r in relations
            if normalize_entity_name(r.head) not in dropped
            and normalize_entity_name(r.tail) not in dropped
        ]
    return MergedExtraction(markers=markers, entities=entities, relations=relations)


def _parse_entities(
    data: dict, *, batch_start: int, batch_end: int, profile: ExtractionProfile
) -> tuple[list[EntityMention] | None, set[str]]:
    """返回 (保留的实体, 被噪音规则丢弃的归一化实体名)；结构非法时实体为 None。"""
    raw_entities = data.get("entities")
    if not isinstance(raw_entities, list):
        return None, set()
    out: list[EntityMention] = []
    dropped: set[str] = set()
    for item in raw_entities:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 512:
            continue
        lines = _clamp_lines(item.get("lines"), batch_start, batch_end)
        description = str(item.get("description") or "").strip() or None
        etype = profile_entity_type(profile, item.get("type"))
        if noise_reason(name, etype, description, profile=profile):
            dropped.add(normalize_entity_name(name))
            continue
        out.append(
            EntityMention(
                name=name,
                type=etype,
                description=description[:2000] if description else None,
                lines=lines,
                aliases=_clean_aliases(item.get("aliases"), name),
            )
        )
    return out, dropped - {normalize_entity_name(e.name) for e in out}


def _clean_aliases(value, name: str) -> list[str]:
    """别名：非空、≤80 字符、与 name 归一后不同、去重，最多 5 个。"""
    if not isinstance(value, list):
        return []
    seen = {normalize_entity_name(name)}
    out: list[str] = []
    for v in value:
        a = str(v or "").strip()
        n = normalize_entity_name(a)
        if not a or len(a) > 80 or not n or n in seen:
            continue
        seen.add(n)
        out.append(a)
        if len(out) >= 5:
            break
    return out


def _parse_relations(
    data: dict, *, batch_start: int, batch_end: int, profile: ExtractionProfile
) -> list[RelationMention] | None:
    raw_relations = data.get("relations")
    if not isinstance(raw_relations, list):
        return None
    out: list[RelationMention] = []
    for item in raw_relations:
        if not isinstance(item, dict):
            continue
        head = str(item.get("head") or "").strip()
        tail = str(item.get("tail") or "").strip()
        if not head or not tail or head == tail:
            continue
        if len(head) > 512 or len(tail) > 512:
            continue
        rel_type = _REL_TYPE_RE.sub("_", str(item.get("type") or "").strip().upper())
        rel_type = rel_type.strip("_")[:_MAX_REL_TYPE_LEN]
        if not rel_type or is_blocked_relation(rel_type, profile=profile):
            continue
        out.append(
            RelationMention(
                head=head,
                tail=tail,
                type=rel_type,
                evidence_lines=_clamp_lines(item.get("evidence_lines"), batch_start, batch_end),
            )
        )
    return out


def _clamp_lines(value, batch_start: int, batch_end: int) -> list[int]:
    """行号夹取到批次内（LLM 位置输出不信任），去重、截断。"""
    if not isinstance(value, list):
        return []
    lines: list[int] = []
    for v in value:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if batch_start <= n <= batch_end and n not in lines:
            lines.append(n)
        if len(lines) >= _MAX_LINES_PER_MENTION:
            break
    return lines


def attribute_relations(relations: list[RelationMention], drafts: list[ChunkDraft]) -> None:
    """关系归属 chunk：evidence_lines 与 chunk 行区间求交，重叠最多者胜出。

    无 evidence_lines 的关系归入首个与其实体提及行重叠的 chunk；完全无交集时
    归入时间上最近的 chunk（仍保证溯源不丢）。
    """
    if not drafts:
        return
    for rel in relations:
        anchors = rel.evidence_lines
        if not anchors:
            anchors = _entity_lines_for(rel, drafts)
        best: tuple[int, int] | None = None  # (overlap, key)
        fallback_key = drafts[0].key
        for d in drafts:
            overlap = len(set(anchors) & set(range(d.start_line, d.end_line + 1)))
            if best is None or overlap > best[0]:
                best = (overlap, d.key)
            if d.start_line <= (anchors[0] if anchors else d.start_line) <= d.end_line:
                fallback_key = d.key
        rel.chunk_key = best[1] if best and best[0] > 0 else fallback_key


def _entity_lines_for(rel: RelationMention, drafts: list[ChunkDraft]) -> list[int]:
    """关系无证据行时，用 head/tail 提及行作锚（由 parse 阶段回填前尽力而为）。"""
    # 抽取阶段未保留 name→lines 索引，这里退化为空（attribute_relations 兜底首个 chunk）
    return []


def sanitize_extraction(
    entities: list[EntityMention],
    relations: list[RelationMention],
    *,
    drop_isolated: bool = False,
) -> tuple[list[EntityMention], list[RelationMention]]:
    """关系端点可解析校验：head/tail 必须能在本批实体（归一化名）中解析。

    drop_isolated=True 时再丢弃不参与任何关系的实体：L3 召回只经「边的 chunk_ids」取回内容，
    孤立实体对检索没有贡献，却占存储、向量与合并成本。
    """
    known = {normalize_entity_name(e.name) for e in entities}
    valid = [
        r
        for r in relations
        if normalize_entity_name(r.head) in known and normalize_entity_name(r.tail) in known
    ]
    if drop_isolated:
        used = {normalize_entity_name(r.head) for r in valid} | {
            normalize_entity_name(r.tail) for r in valid
        }
        entities = [e for e in entities if normalize_entity_name(e.name) in used]
    return entities, valid
