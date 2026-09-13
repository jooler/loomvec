"""P3-CORE-03 阶段二：空间级异步实体合并与 merge_log 回滚（03 文档 §3.3）。

阶段一（graph_flow 内联链接）阈值放不掉的近似重复由本服务在空间级收口：
- 候选发现：实体向量近邻（阈值 merge_threshold）+ 规范名编辑距离
  （merge_name_distance，同名不同写法）；
- 合并动作：保留主实体规范名，合并 description/aliases、union 边 chunk_ids、
  边端点重写（AGE 与 PG 同事务）；每次合并写 merge_log 快照，可回滚；
- 并发控制：per-space advisory lock（与图写入同一把锁，串行化同空间图变更）。

回滚语义：按 merge_log.snapshot 逆向恢复——重建败者实体行与 AGE 节点、
把合并期间被改写的边恢复为合并前状态（限定在本合并触及的
(head, tail, type) 三元组内；合并后新写入的同三元组边以恢复为准，
其 chunk_ids 可能被回卷，属可接受的审计边界，见 snapshot 结构注释）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import Settings
from loomvec.core.constants import (
    MERGE_REASON_NAME,
    MERGE_REASON_VECTOR,
)
from loomvec.core.db.models import Entity, EntityMergeLog, MergeLogStatus, Space
from loomvec.core.graph.age import AgeStore, GraphEdge, GraphNode

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


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein（带 cap 剪枝；输入为短归一化名，规模可控）。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


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


def vector_candidates(
    entities: list[Entity], vectors: dict[str, list[float]], threshold: float
) -> list[MergeCandidate]:
    """向量近邻候选：纯 Python 余弦（实体规模千级 × 1024 维可控）。"""
    out: list[MergeCandidate] = []
    for e in entities:
        vi = vectors.get(e.entity_key)
        if not vi:
            continue
        best_key, best_score = None, 0.0
        for other in entities:
            if other.entity_key == e.entity_key:
                continue
            vj = vectors.get(other.entity_key)
            if not vj:
                continue
            dot = sum(x * y for x, y in zip(vi, vj, strict=False))
            if dot > best_score:
                best_key, best_score = other.entity_key, dot
        if best_key and best_score >= threshold:
            out.append(
                MergeCandidate(
                    winner_key=best_key,
                    loser_key=e.entity_key,
                    score=best_score,
                    reason=MERGE_REASON_VECTOR,
                )
            )
    return out


def name_candidates(entities: list[Entity], max_distance: int) -> list[MergeCandidate]:
    """规范名编辑距离候选（同类型；min 长度 ≥4 防短名误并）。"""
    out: list[MergeCandidate] = []
    for i, a in enumerate(entities):
        if len(a.name_norm) < 4:
            continue
        for b in entities[i + 1 :]:
            if b.type != a.type or len(b.name_norm) < 4:
                continue
            d = _edit_distance(a.name_norm, b.name_norm, max_distance)
            if d <= max_distance:
                score = 1.0 - d / max(len(a.name_norm), len(b.name_norm))
                out.append(
                    MergeCandidate(
                        winner_key=a.entity_key,
                        loser_key=b.entity_key,
                        score=round(score, 4),
                        reason=MERGE_REASON_NAME,
                    )
                )
    return out


async def find_candidates(
    session: AsyncSession, deps_settings: Settings, space_id: UUID
) -> list[MergeCandidate]:
    """候选发现（向量 + 名称，去重；每败者仅保留最优候选）。"""
    entities = await load_active_entities(session, space_id)
    if len(entities) < 2:
        return []
    candidates: list[MergeCandidate] = []
    # 向量候选从 Milvus entities 全量拉取向量（缺集合则跳过该路）
    # 注：向量路径依赖调用方注入 fetch 函数（避免 core→worker 依赖），见 merge_task。
    candidates.extend(name_candidates(entities, deps_settings.graph.merge_name_distance))
    by_loser: dict[str, MergeCandidate] = {}
    for c in candidates:
        if c.loser_key not in by_loser or c.score > by_loser[c.loser_key].score:
            by_loser[c.loser_key] = c
    return list(by_loser.values())


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
# 空间级合并任务入口（worker 调用；向量候选经注入回调获取）
# ---------------------------------------------------------------------------


async def run_space_merge(
    session: AsyncSession,
    settings: Settings,
    space_id: UUID,
    *,
    vector_fetch=None,
    created_by: str | None = "system",
    max_merges: int = 50,
) -> dict:
    """执行一轮空间合并：候选发现 → 逐对合并（每对独立提交由调用方控制）。

    vector_fetch: async (entities) -> dict[entity_key, vector]（worker 注入
    Milvus 拉取；None 时跳过向量候选，仅规范名候选）。
    """
    entities = await load_active_entities(session, space_id)
    if len(entities) < 2:
        return {"candidates": 0, "merged": 0}
    candidates = name_candidates(entities, settings.graph.merge_name_distance)
    if vector_fetch is not None:
        try:
            vectors = await vector_fetch(entities)
            candidates.extend(vector_candidates(entities, vectors, settings.graph.merge_threshold))
        except Exception as e:
            logger.warning("merge_vector_candidates_unavailable", error=str(e))

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
