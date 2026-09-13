"""P3-WRK-02 社区层：Leiden 社区检测（per space）→ LLM 社区摘要。

- 检测：leidenalg（可选依赖 `loomvec-core[communities]`）优先；未安装时退化为
  networkx 贪心模块度（greedy modularity）；图构建基于 AGE 边表（同库读取）；
- 摘要：每社区一次 LLM 调用（LOOMVEC_TASK=community_summary），摘要文本 +
  摘要向量以 unit_type=community 语义单元写入 Milvus（L4 全局总结召回通道）；
  摘要单元 id = community.id，asset_id 同值（检索回表走 Community 表）；
- 确定性：社区编号按成员排序生成，重跑时成员未变的社区复用已有行
  （id 稳定 → Milvus 幂等 upsert）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import Settings
from loomvec.core.db.models import Community, Entity
from loomvec.core.graph.age import AgeStore, GraphEdge
from loomvec.core.storage_keys import embed_vectors_key  # noqa: F401 — 语义对齐注释用

logger = structlog.get_logger("loomvec.graph.communities")

AGE = AgeStore()


# ---------------------------------------------------------------------------
# 社区检测（纯算法，可单测）
# ---------------------------------------------------------------------------


def detect_communities(node_keys: list[str], edges: list[GraphEdge]) -> dict[str, int]:
    """节点 → 社区序号映射。Leiden 优先，退化 greedy modularity，再退化连通分量。"""
    if not node_keys:
        return {}
    index = {k: i for i, k in enumerate(sorted(node_keys))}
    import importlib.util

    if importlib.util.find_spec("leidenalg") is not None:
        return _leiden(index, edges)
    if importlib.util.find_spec("networkx") is not None:
        return _greedy_modularity(index, edges)
    return _connected_components(index, edges)  # pragma: no cover — 防御分支


def _edge_index_pairs(edges: list[GraphEdge], index: dict[str, int]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    for e in edges:
        a, b = index.get(e.head_key), index.get(e.tail_key)
        if a is not None and b is not None and a != b:
            pairs.append((a, b))
    return pairs


def _leiden(index: dict[str, int], edges: list[GraphEdge]) -> dict[str, int]:
    import igraph as ig
    import leidenalg

    pairs = _edge_index_pairs(edges, index)
    g = ig.Graph(n=len(index), edges=pairs, directed=False)
    partition = leidenalg.find_partition(g, leidenalg.RBConfigurationVertexPartition, seed=42)
    return {key: int(partition.membership[i]) for key, i in index.items()}


def _greedy_modularity(index: dict[str, int], edges: list[GraphEdge]) -> dict[str, int]:
    import networkx as nx

    g = nx.Graph()
    g.add_nodes_from(index.values())
    g.add_edges_from(_edge_index_pairs(edges, index))
    communities = nx.algorithms.community.greedy_modularity_communities(g)
    inverse = {i: key for key, i in index.items()}
    return {
        key: cid for cid, members in enumerate(communities) for key in (inverse[i] for i in members)
    }


def _connected_components(index: dict[str, int], edges: list[GraphEdge]) -> dict[str, int]:
    parent = {i: i for i in index.values()}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in _edge_index_pairs(edges, index):
        parent[find(a)] = find(b)
    roots: dict[int, int] = {}
    out: dict[str, int] = {}
    for key, i in index.items():
        r = find(i)
        roots.setdefault(r, len(roots))
        out[key] = roots[r]
    return out


# ---------------------------------------------------------------------------
# 空间社区任务（worker 调用）
# ---------------------------------------------------------------------------


def _stable_label(space_batches: dict[str, int], cid: int) -> str:
    return f"C{space_batches.get(str(cid), 0)}-{cid}"


async def run_space_communities(
    session: AsyncSession,
    settings: Settings,
    space_id: UUID,
    tenant_id: UUID | None,
    *,
    summarize=None,
) -> dict:
    """社区检测 + 摘要（summarize: async (Community, members) -> str，worker 注入 LLM）。

    稳定性约定：按"成员集合指纹"复用既有 community 行（id 稳定 → Milvus
    upsert 幂等，L4 检索不因重跑抖动）；成员有变的社区旧行删除、新行重建。
    """
    nodes = await AGE.load_space_nodes(session, str(space_id))
    edges = await AGE.load_space_edges(session, str(space_id))
    # 孤立节点不进社区（无边的实体对 L4 无意义）
    linked_keys = {k for e in edges for k in (e.head_key, e.tail_key)}
    keys = sorted(k for k in nodes if k in linked_keys)
    if len(keys) < 3:
        return {"communities": 0, "skipped": "graph_too_small"}

    membership = detect_communities(keys, edges)
    groups: dict[int, list[str]] = {}
    for key, cid in membership.items():
        groups.setdefault(cid, []).append(key)

    existing = (
        (await session.execute(select(Community).where(Community.space_id == space_id)))
        .scalars()
        .all()
    )
    entity_rows = (
        (
            await session.execute(
                select(Entity).where(
                    Entity.space_id == space_id,
                    Entity.deleted_at.is_(None),
                    Entity.entity_key.in_(keys),
                )
            )
        )
        .scalars()
        .all()
    )
    key_to_entity = {e.entity_key: e for e in entity_rows}

    old_by_fp = {_fingerprint(_members_of(c, key_to_entity)): c for c in existing}
    seen_fps: set[str] = set()
    created = updated = 0
    now = datetime.now(UTC)
    for cid in sorted(groups):
        members = sorted(groups[cid])
        fp = _fingerprint(members)
        seen_fps.add(fp)
        community = old_by_fp.get(fp)
        if community is None:
            community = Community(
                tenant_id=tenant_id,
                space_id=space_id,
                label=f"C-{cid}",
                member_count=len(members),
                summary_status="pending",
            )
            session.add(community)
            await session.flush()
            created += 1
        else:
            community.member_count = len(members)
            updated += 1
        community.updated_at = now
        # 实体回写 community_id（含旧社区迁出者清空）
        for e in entity_rows:
            if e.community_id == community.id:
                e.community_id = None
        for key in members:
            ent = key_to_entity.get(key)
            if ent is not None:
                ent.community_id = community.id
        if summarize is not None and settings.graph.enabled:
            try:
                summary = await summarize(
                    community, [key_to_entity[k] for k in members if k in key_to_entity]
                )
                if summary:
                    community.summary = summary
                    community.summary_status = "ready"
                    community.summary_model_version = (
                        "mock" if settings.ai.mock else (settings.ai.llm.model or "unknown")
                    )
            except Exception as e:
                logger.warning("community_summary_failed", cid=cid, error=str(e))
                community.summary_status = "failed"
    # 成员集合已变化的旧社区行删除（指纹未再现）
    for fp, c in old_by_fp.items():
        if fp not in seen_fps:
            for e in entity_rows:
                if e.community_id == c.id:
                    e.community_id = None
            await session.delete(c)
    await session.flush()
    logger.info(
        "communities_updated",
        space=str(space_id),
        communities=len(groups),
        created=created,
        updated=updated,
    )
    return {"communities": len(groups), "created": created, "updated": updated}


def _members_of(community: Community, key_to_entity: dict[str, Entity]) -> list[str]:
    return sorted(k for k, e in key_to_entity.items() if e.community_id == community.id)


def _fingerprint(members: list[str]) -> str:
    import hashlib

    return hashlib.sha256("|".join(members).encode()).hexdigest()[:32]


def community_summary_messages(community: Community, members: list[Entity]) -> list[dict[str, str]]:
    """社区摘要 prompt（LOOMVEC_TASK=community_summary 供 mock 路由）。"""
    lines = "\n".join(
        f"- {e.name}（{e.type}）: {e.description or '（无描述）'}" for e in members[:50]
    )
    return [
        {
            "role": "system",
            "content": (
                "你是知识图谱社区摘要引擎。根据社区成员实体与其关系，写一段"
                "100~200 字的中文摘要，概括该社区共同的主题与成员间的主要关系，"
                '输出 JSON：{"summary": "..."}。'
            ),
        },
        {
            "role": "user",
            "content": (
                f"[LOOMVEC_TASK=community_summary]\n社区 {community.label} 成员：\n{lines}"
            ),
        },
    ]


# L4 摘要向量：写入 Milvus semantic_units 的行构造（unit_type=community）
def community_unit_row(
    community: Community, vector: list[float], *, model_version: str, text_dim: int, clip_dim: int
) -> dict[str, Any]:
    return {
        "id": str(community.id),
        "space_id": str(community.space_id),
        "tenant_id": str(community.tenant_id) if community.tenant_id else "",
        "asset_id": str(community.id),  # 摘要单元无资产归属，自指以保持 schema 非空
        "unit_type": "community",
        "model_version": model_version,
        "text": (community.summary or community.label)[:30000],
        "text_dense": vector,
        "clip_dense": [0.0] * clip_dim,
    }
