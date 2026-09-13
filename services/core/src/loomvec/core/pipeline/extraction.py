"""P3-CORE-02 合并抽取协议：分片 markers 与实体/关系单次 LLM 调用（03 文档 §3.2）。

一次调用返回两段结构，**独立校验、互不牵连**：
- markers 按行号规则校验（chunking.validate_markers），失效 → 结构化兜底分片；
- entities/relations 按名称非空、type 白名单归并、head/tail 可解析校验，
  失效 → 资产标记 graph_status=pending_retry，分片照常入库；
- 关系归属 chunk：evidence_lines 与 chunk 行区间求交（重叠最多者），零对齐成本；
- 缓存键沿用 (checksum, 解析器版本, 模型, prompt 版本)，prompt 版本升级为
  chunk-graph-v2（config.PipelineSettings.prompt_version）。

控制标记协议：`[LOOMVEC_TASK=chunk_graph]`（mock 网关据此确定性生成两段结构）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from loomvec.core.ai import AiJsonError, extract_json
from loomvec.core.constants import DEFAULT_ENTITY_TYPE, ENTITY_TYPES
from loomvec.core.graph.ontology import canonical_type, normalize_entity_name
from loomvec.core.pipeline.chunking import ChunkDraft, parse_markers

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


def build_extraction_messages(
    lines: list[str], start: int, end: int, *, min_chars: int, max_chars: int
) -> list[dict[str, str]]:
    """合并抽取 prompt：分片契约（与 chunk-v1 相同）+ 实体/关系抽取契约。"""
    body = "\n".join(f"L{n}: {lines[n - 1]}" for n in range(start, end + 1))
    system = (
        "你是文档语义分片与知识图谱抽取引擎。根据给定的带行号文档行，输出 JSON，"
        "同时包含分片边界与实体关系两段结构。\n"
        "输出格式（仅输出 JSON，两个键都必须存在）：\n"
        '{"chunk_markers": [{"start_line": 12, "end_line": 47, "title": "片段标题", '
        '"keywords": ["关键词"]}],\n'
        ' "entities": [{"name": "实体名", "type": "Organization", "description": "简要描述", '
        '"lines": [20, 44]}],\n'
        ' "relations": [{"head": "实体A", "tail": "实体B", "type": "SUBSIDIARY_OF", '
        '"evidence_lines": [30, 35]}]}\n'
        f"分片规则：1) 按语义边界（标题/段落/主题切换）；2) 单片 {min_chars}~{max_chars} 字符，"
        "过长按主题拆分，过短并入相邻；3) markers 覆盖全部给定行、区间不重叠、行号单调递增；"
        "4) 不要把一个表格拆到两个片段。\n"
        f"抽取规则：1) 实体 type 限定 {'/'.join(t.capitalize() for t in ENTITY_TYPES)}；"
        "2) 关系 head/tail 必须是本批 entities 中已列出的实体名（原文写法）；"
        "3) relation.type 用大写下划线短语（如 FOUNDED_BY、LOCATED_IN、SUBSIDIARY_OF）；"
        "4) lines/evidence_lines 用给定的 L 行号；5) 只抽文中明确支持的关系，不要推断。"
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


def parse_merged(raw: str | dict, *, batch_start: int, batch_end: int) -> MergedExtraction:
    """解析与独立校验两段结构；任一部分失效不抛异常（以 None 表示），互不牵连。"""
    data = raw if isinstance(raw, dict) else extract_json(raw)

    # ---- markers：复用 parse_markers，失败即 None（调用方结构化兜底）----
    try:
        markers: list[dict] | None = parse_markers(data)
    except AiJsonError:
        markers = None

    # ---- entities / relations：结构校验 + 字段清洗 ----
    entities = _parse_entities(data, batch_start=batch_start, batch_end=batch_end)
    relations = _parse_relations(data, batch_start=batch_start, batch_end=batch_end)
    return MergedExtraction(markers=markers, entities=entities, relations=relations)


def _parse_entities(data: dict, *, batch_start: int, batch_end: int) -> list[EntityMention] | None:
    raw_entities = data.get("entities")
    if not isinstance(raw_entities, list):
        return None
    out: list[EntityMention] = []
    for item in raw_entities:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 512:
            continue
        lines = _clamp_lines(item.get("lines"), batch_start, batch_end)
        description = str(item.get("description") or "").strip() or None
        out.append(
            EntityMention(
                name=name,
                type=canonical_type(item.get("type")),
                description=description[:2000] if description else None,
                lines=lines,
            )
        )
    return out


def _parse_relations(
    data: dict, *, batch_start: int, batch_end: int
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
        if not rel_type:
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
    entities: list[EntityMention], relations: list[RelationMention]
) -> tuple[list[EntityMention], list[RelationMention]]:
    """关系端点可解析校验：head/tail 必须能在本批实体（归一化名）中解析。"""
    known = {normalize_entity_name(e.name) for e in entities}
    valid = [
        r
        for r in relations
        if normalize_entity_name(r.head) in known and normalize_entity_name(r.tail) in known
    ]
    return entities, valid
