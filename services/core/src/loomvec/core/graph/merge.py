"""P3-CORE-03 阶段二：空间级异步实体合并与 merge_log 回滚（03 文档 §3.3）。

阶段一（graph_flow 内联链接）只做规范名精确匹配；同名不同写法由本服务在空间级收口：
- 候选发现：只收「确定是同一个东西的不同写法」——同类型且 `variant_key` 相同
  （大小写/分隔符/全半角已由 name_norm 吸收；此外只容忍末尾小写复数 s 与能由括号前各词
  首字母推出的括号缩写）。向量近邻与编辑距离不作为自动合并依据；
- 合并动作：保留主实体规范名，合并 description/aliases、union 边 chunk_ids、
  边端点重写（AGE 与 PG 同事务）；每次合并写 merge_log 快照，可回滚；
- 并发控制：per-space advisory lock（与图写入同一把锁，串行化同空间图变更）。

回滚语义：按 merge_log.snapshot 逆向恢复——重建败者实体行与 AGE 节点、
把合并期间被改写的边恢复为合并前状态（限定在本合并触及的
(head, tail, type) 三元组内；合并后新写入的同三元组边以恢复为准，
其 chunk_ids 可能被回卷，属可接受的审计边界，见 snapshot 结构注释）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import Settings
from loomvec.core.constants import MERGE_REASON_NAME
from loomvec.core.db.models import Entity, EntityMergeLog, MergeLogStatus, Space
from loomvec.core.graph.age import AgeStore, GraphEdge, GraphNode
from loomvec.core.graph.ontology import normalize_entity_name

logger = structlog.get_logger("loomvec.graph.merge")

AGE = AgeStore()


def _advisory_key(space_id: str) -> str:
    return f"loomvec:graph:{space_id}"


@dataclass(frozen=True)
class MergeCandidate:
    winner_key: str
    loser_key: str
    score: float
    reason: str  # MERGE_REASON_*


# ---------------------------------------------------------------------------
# 候选发现
# ---------------------------------------------------------------------------


# 括号里的纯字母大写缩写：「cancer stem cells (CSC)」「…(CSCs)」。含数字/符号的括注
# （(CD4)、(M1)、(CD25+)、(3B)）与小写括注（(human)）是限定语，一律保留
_ABBREV_PAREN_RE = re.compile(r"[(（]\s*([A-Za-z]{2,8})\s*[)）]")
_WORD_SPLIT_RE = re.compile(r"[\s\-–—_/]+")
_STOPWORDS = frozenset({"of", "and", "the", "for", "in", "on", "to", "a", "an"})
_MIN_VARIANT_LEN = 4  # 过短的名字（缩写、代号）不做自动归并


def _is_initialism_of(abbr: str, preceding: str) -> bool:
    """abbr 是否是括号前各词首字母的缩写（忽略大小写；连接词计不计均可；缩写可带复数 s）。"""
    words = [w for w in _WORD_SPLIT_RE.split(preceding) if w]
    candidates = {
        "".join(w[0] for w in words).lower(),
        "".join(w[0] for w in words if w.lower() not in _STOPWORDS).lower(),
    }
    a = abbr.lower()
    return any(len(i) >= 2 and (a == i or (a.endswith("s") and a[:-1] == i)) for i in candidates)


def variant_key(name: str) -> str:
    """「同一个东西的不同写法」判等键：name_norm 之上再吸收括号缩写与末尾复数 s。

    刻意保守：差一个字母/数字/符号的名字必须得到不同的键（SEMA3A≠SEMA5A、
    CD4+ T≠CD8+ T、GK+≠GK-、T cells (CD4)≠T cells (CD8)、TP53 (WT)≠TP53 (KO)、
    CytoFLEX S≠CytoFlex），宁可漏并不可错并：
    - 括号缩写只有在能由括号前的词推出时才剥离（Chain-of-Thought (CoT) ✓，Cells (HeLa) ✗）；
    - 复数只认原写法里小写的词尾 s（PBMCs ✓，型号后缀 CytoFLEX S ✗）。
    """
    text = unicodedata.normalize("NFKC", str(name or ""))
    text = _ABBREV_PAREN_RE.sub(
        lambda m: "" if _is_initialism_of(m.group(1), m.string[: m.start()]) else m.group(0),
        text,
    )
    stripped = text.strip()
    plural = stripped.endswith("s") and not stripped.endswith("ss")
    norm = normalize_entity_name(stripped)
    if plural and len(norm) > _MIN_VARIANT_LEN:
        norm = norm[:-1]
    return norm


async def load_active_entities(session: AsyncSession, space_id: UUID) -> list[Entity]:
    return list(
        (
            await session.execute(
                select(Entity)
                .where(
                    Entity.space_id == space_id,
                    Entity.deleted_at.is_(None),
                    Entity.merged_into.is_(None),
                )
                .order_by(Entity.created_at)
            )
        )
        .scalars()
        .all()
    )


def name_candidates(entities: list[Entity]) -> list[MergeCandidate]:
    """同类型且 variant_key 相同的实体归并（入参按创建时间升序：最早者为主实体）。"""
    groups: dict[tuple[str, str], list[Entity]] = {}
    for e in entities:
        key = variant_key(e.name)
        if len(key) >= _MIN_VARIANT_LEN:
            groups.setdefault((e.type, key), []).append(e)
    out: list[MergeCandidate] = []
    for members in groups.values():
        winner = members[0]
        out.extend(
            MergeCandidate(
                winner_key=winner.entity_key,
                loser_key=loser.entity_key,
                score=1.0,
                reason=MERGE_REASON_NAME,
            )
            for loser in members[1:]
        )
    return out


# ---------------------------------------------------------------------------
# 合并与回滚
# ---------------------------------------------------------------------------


async def merge_pair(
    session: AsyncSession,
    settings: Settings,
    *,
    space_id: UUID,
    winner: Entity,
    loser: Entity,
    reason: str,
    score: float | None,
    created_by: str | None,
) -> EntityMergeLog:
    """合并 loser → winner（AGE 端点重写 + PG 主表 + merge_log 快照，同事务）。"""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _advisory_key(str(space_id))}
    )
    if loser.merged_into is not None or winner.merged_into is not None:
        raise ValueError("实体已被合并（请刷新候选）")

    loser_edges = await AGE.load_node_edges(session, str(space_id), loser.entity_key)
    winner_edges = await AGE.load_node_edges(session, str(space_id), winner.entity_key)
    # 快照先于任何边变更生成（回滚依据 = 合并前状态）
    winner_edges_before = [_edge_snap(e) for e in winner_edges]
    loser_edges_before = [_edge_snap(e) for e in loser_edges]

    # ---- AGE：边端点重写（loser→winner），同三元组 union chunk_ids ----
    winner_index = {(e.head_key, e.tail_key, e.type): e for e in winner_edges}
    creates: list[GraphEdge] = []
    updates: dict[tuple[str, str, str], tuple[list[str], list[str]]] = {}
    for e in loser_edges:
        loser_is_head = e.head_key == loser.entity_key
        other = e.tail_key if loser_is_head else e.head_key
        if other == winner.entity_key:
            continue  # loser↔winner 边随败者摘边一并删除（成自环，无信息）
        nh, nt = (winner.entity_key, other) if loser_is_head else (other, winner.entity_key)
        key = (nh, nt, e.type)
        existing = winner_index.get(key)
        if existing is None:
            creates.append(
                GraphEdge(
                    head_key=nh,
                    tail_key=nt,
                    type=e.type,
                    weight=e.weight,
                    chunk_ids=list(e.chunk_ids),
                    asset_ids=list(e.asset_ids),
                )
            )
            winner_index[key] = GraphEdge(
                head_key=nh, tail_key=nt, type=e.type, chunk_ids=list(e.chunk_ids)
            )
        else:
            existing.chunk_ids = _union(existing.chunk_ids, e.chunk_ids)
            existing.asset_ids = _union(existing.asset_ids, e.asset_ids)
            updates[key] = (existing.chunk_ids, existing.asset_ids)

    await AGE.delete_node_edges(session, str(space_id), loser.entity_key)
    for (h, t, etype), (chunks, assets) in updates.items():
        await AGE.update_edge_chunks(session, str(space_id), h, t, etype, chunks, assets)
    await AGE.write_edges(session, creates)
    await AGE.delete_nodes(session, str(space_id), [loser.entity_key])

    # ---- PG 主表：败者标记合并 + 胜者吸收描述/别名 ----
    winner_before = {
        "description": winner.description,
        "aliases": list(winner.aliases or []),
    }
    loser_snapshot = {
        "id": str(loser.id),
        "entity_key": loser.entity_key,
        "name": loser.name,
        "name_norm": loser.name_norm,
        "type": loser.type,
        "description": loser.description,
        "aliases": list(loser.aliases or []),
        "community_id": str(loser.community_id) if loser.community_id else None,
    }
    if not winner.description and loser.description:
        winner.description = loser.description
    new_aliases = [loser.name, *(loser.aliases or [])]
    winner.aliases = _union(
        list(winner.aliases or []), [a for a in new_aliases if a != winner.name]
    )
    loser.merged_into = winner.id
    loser.deleted_at = datetime.now(UTC)

    log = EntityMergeLog(
        tenant_id=winner.tenant_id,
        space_id=space_id,
        winner_id=winner.id,
        loser_id=loser.id,
        status=MergeLogStatus.APPLIED,
        reason=reason,
        score=score,
        created_by=created_by or "system",
        snapshot={
            "loser": loser_snapshot,
            "winner_before": winner_before,
            "winner_edges_before": winner_edges_before,
            "loser_edges": loser_edges_before,
        },
    )
    session.add(log)
    await session.flush()
    logger.info("entity_merged", space=str(space_id), loser=loser.name, winner=winner.name)
    return log


def _edge_snap(e: GraphEdge) -> dict[str, Any]:
    return {
        "h": e.head_key,
        "t": e.tail_key,
        "type": e.type,
        "weight": e.weight,
        "chunk_ids": list(e.chunk_ids),
        "asset_ids": list(e.asset_ids),
    }


async def rollback_merge(session: AsyncSession, settings: Settings, log_id: UUID) -> EntityMergeLog:
    """回滚一次合并：恢复败者实体行/AGE 节点/边到合并前状态。"""
    log = (
        await session.execute(select(EntityMergeLog).where(EntityMergeLog.id == log_id))
    ).scalar_one_or_none()
    if log is None:
        raise LookupError(f"merge_log {log_id} 不存在")
    if log.status != MergeLogStatus.APPLIED:
        raise ValueError("该合并已回滚")
    winner = (
        await session.execute(select(Entity).where(Entity.id == log.winner_id))
    ).scalar_one_or_none()
    snap = log.snapshot or {}
    loser_snap = snap.get("loser") or {}
    loser = (
        await session.execute(select(Entity).where(Entity.id == UUID(loser_snap["id"])))
    ).scalar_one_or_none()

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": _advisory_key(str(log.space_id))},
    )

    # ---- AGE：清掉触及三元组的现边，按快照精确重建 ----
    # 三元组集合 = 胜者合并前的边 ∪ 败者原边 ∪ 合并时被改写到胜者的边
    # （否则改写边会在回滚后残留为重复边）
    space_id = str(log.space_id)
    winner_key = winner.entity_key if winner is not None else None
    loser_key = loser_snap.get("entity_key")
    triples: set[tuple[str, str, str]] = {
        (e["h"], e["t"], e["type"]) for e in snap.get("winner_edges_before", [])
    } | {(e["h"], e["t"], e["type"]) for e in snap.get("loser_edges", [])}
    if winner_key and loser_key:
        for e in snap.get("loser_edges", []):
            other = e["t"] if e["h"] == loser_key else e["h"]
            if other != winner_key:
                # 改写后可能的两个方向都纳入清理
                triples.add((winner_key, other, e["type"]))
                triples.add((other, winner_key, e["type"]))
    # 先重建败者节点：边的恢复以 MATCH 两端节点为前提（节点不存在时 CREATE 静默跳过）
    if loser_snap:
        await AGE.upsert_nodes(
            session,
            [
                GraphNode(
                    key=loser_snap["entity_key"],
                    space_id=space_id,
                    tenant_id=str(winner.tenant_id) if winner else None,
                    name=loser_snap["name"],
                    type=loser_snap["type"],
                    description=loser_snap.get("description"),
                )
            ],
        )
    if winner is not None:
        current = await AGE.load_node_edges(session, space_id, winner.entity_key)
        for e in current:
            if (e.head_key, e.tail_key, e.type) in triples or (
                e.tail_key,
                e.head_key,
                e.type,
            ) in triples:
                await AGE.delete_edge(session, space_id, e.head_key, e.tail_key, e.type)
    for e in snap.get("winner_edges_before", []) + snap.get("loser_edges", []):
        await AGE.write_edges(
            session,
            [
                GraphEdge(
                    head_key=e["h"],
                    tail_key=e["t"],
                    type=e["type"],
                    weight=float(e.get("weight") or 1.0),
                    chunk_ids=list(e.get("chunk_ids") or []),
                    asset_ids=list(e.get("asset_ids") or []),
                )
            ],
        )

    # ---- PG：恢复败者行与胜者合并前字段 ----
    if loser is not None:
        loser.merged_into = None
        loser.deleted_at = None
        loser.description = loser_snap.get("description")
        loser.aliases = list(loser_snap.get("aliases") or [])
    if winner is not None:
        before = snap.get("winner_before") or {}
        winner.description = before.get("description")
        winner.aliases = list(before.get("aliases") or [])
    log.status = MergeLogStatus.ROLLED_BACK
    log.rolled_back_at = datetime.now(UTC)
    await session.flush()
    logger.info("entity_merge_rolled_back", merge_log=str(log.id), space=space_id)
    return log


def _union(a: list[str], b: list[str]) -> list[str]:
    return list(dict.fromkeys([*(a or []), *(b or [])]))


# ---------------------------------------------------------------------------
# 空间级合并任务入口（worker 调用）
# ---------------------------------------------------------------------------


async def run_space_merge(
    session: AsyncSession,
    settings: Settings,
    space_id: UUID,
    *,
    created_by: str | None = "system",
    max_merges: int = 50,
) -> dict:
    """执行一轮空间合并：候选发现 → 逐对合并（每对独立提交由调用方控制）。"""
    entities = await load_active_entities(session, space_id)
    if len(entities) < 2:
        return {"candidates": 0, "merged": 0}
    candidates = name_candidates(entities)

    by_loser: dict[str, MergeCandidate] = {}
    for c in candidates:
        if c.loser_key == c.winner_key:
            continue
        if c.loser_key not in by_loser or c.score > by_loser[c.loser_key].score:
            by_loser[c.loser_key] = c
    ordered = sorted(by_loser.values(), key=lambda c: -c.score)[:max_merges]

    entity_by_key = {e.entity_key: e for e in entities}
    merged = 0
    for c in ordered:
        winner = entity_by_key.get(c.winner_key)
        loser = entity_by_key.get(c.loser_key)
        if winner is None or loser is None:
            continue
        try:
            await merge_pair(
                session,
                settings,
                space_id=space_id,
                winner=winner,
                loser=loser,
                reason=c.reason,
                score=c.score,
                created_by=created_by,
            )
            merged += 1
        except ValueError as e:
            logger.debug("merge_pair_skipped", reason=str(e))
    space = (await session.execute(select(Space).where(Space.id == space_id))).scalar_one_or_none()
    if space is not None:
        space.graph_merged_at = datetime.now(UTC)
        space.graph_entities_at_merge = await count_active_entities(session, space_id)
    await session.flush()
    return {"candidates": len(ordered), "merged": merged}


async def count_active_entities(session: AsyncSession, space_id: UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Entity)
                .where(
                    Entity.space_id == space_id,
                    Entity.deleted_at.is_(None),
                    Entity.merged_into.is_(None),
                )
            )
        ).scalar_one()
    )


async def should_trigger_merge(session: AsyncSession, settings: Settings, space_id: UUID) -> bool:
    """触发判定：距上次合并新增/变更实体数 ≥ merge_trigger_delta。"""
    space = (await session.execute(select(Space).where(Space.id == space_id))).scalar_one_or_none()
    if space is None:
        return False
    current = await count_active_entities(session, space_id)
    baseline = space.graph_entities_at_merge
    if baseline is None:
        return current >= settings.graph.merge_trigger_delta
    return current - baseline >= settings.graph.merge_trigger_delta
