"""P3 检索共享服务：命中回表富集（search 路由与问答共用）。

- PG 回表：SemanticUnit / Asset / Space / 父块；community 单元（P3-WRK-02）
  走 Community 表（无资产归属）；
- 可见性兜底：向量与库不同步（已删资产）跳过；viewer 不见未过审资产；
- graph_evidence 原样透传（实体→关系→实体路径，可解释命中的证据链）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.authz import SpaceRole, decide_asset_visibility
from loomvec.core.db.models import Asset, Community, SemanticUnit, Space
from loomvec.core.retrieval import SemanticHit


async def enrich_hits(
    session: AsyncSession,
    hits: list[SemanticHit],
    roles: dict[uuid.UUID, SpaceRole],
) -> list[dict[str, Any]]:
    """SemanticHit → 响应结构 dict（含资产名/空间/locator/父块/证据链）。"""
    if not hits:
        return []
    unit_ids = [h.unit_id for h in hits]
    units = {
        u.id: u
        for u in (await session.execute(select(SemanticUnit).where(SemanticUnit.id.in_(unit_ids))))
        .scalars()
        .all()
    }
    communities: dict[uuid.UUID, Community] = {}
    if any(h.unit_type == "community" for h in hits):
        communities = {
            c.id: c
            for c in (
                await session.execute(
                    select(Community).where(Community.id.in_([h.unit_id for h in hits]))
                )
            )
            .scalars()
            .all()
        }

    asset_ids_hit = {u.asset_id for u in units.values()}
    assets = {
        a.id: a
        for a in (await session.execute(select(Asset).where(Asset.id.in_(asset_ids_hit))))
        .scalars()
        .all()
    }
    spaces: dict[uuid.UUID, Space] = {}
    if assets:
        rows = (
            await session.execute(
                select(Space).where(Space.id.in_({a.space_id for a in assets.values()}))
            )
        ).scalars()
        spaces = {s.id: s for s in rows.all()}

    parent_ids = {u.parent_id for u in units.values() if u.parent_id}
    parents = (
        {
            p.id: p
            for p in (
                await session.execute(select(SemanticUnit).where(SemanticUnit.id.in_(parent_ids)))
            )
            .scalars()
            .all()
        }
        if parent_ids
        else {}
    )

    items: list[dict[str, Any]] = []
    for h in hits:
        if h.unit_type == "community":
            community = communities.get(h.unit_id)
            if community is None:
                continue  # 社区已重建删除
            space = spaces.get(community.space_id) or await _load_space(session, community.space_id)
            items.append(
                {
                    "unit_id": community.id,
                    "asset_id": community.id,  # 无资产归属：自指占位
                    "asset_name": f"社区摘要：{community.label}",
                    "space_id": community.space_id,
                    "space_name": space.name if space else None,
                    "space_slug": space.slug if space else None,
                    "unit_type": "community",
                    "title": f"社区 {community.label}",
                    "text": (community.summary or "")[:2000],
                    "score": h.score,
                    "scores": h.rank_scores,
                    "locator": {},
                    "highlight": h.highlight,
                    "parent": None,
                    "graph_evidence": list(h.graph_evidence or []),
                }
            )
            continue

        unit = units.get(h.unit_id)
        if unit is None:  # 向量与库不同步（已删资产）：跳过
            continue
        asset = assets.get(unit.asset_id)
        if asset is None or asset.deleted_at is not None:
            continue
        role = roles.get(asset.space_id, SpaceRole.VIEWER)
        space = spaces.get(asset.space_id)
        if not decide_asset_visibility(
            review_required=space.review_required if space else False,
            review_status=asset.review_status,
            role=role,
        ):
            continue
        parent_payload = None
        if unit.parent_id and unit.parent_id in parents:
            p = parents[unit.parent_id]
            parent_payload = {"unit_id": str(p.id), "title": p.title, "text": p.content}
        items.append(
            {
                "unit_id": unit.id,
                "asset_id": unit.asset_id,
                "asset_name": asset.name,
                "space_id": asset.space_id,
                "space_name": space.name if space else None,
                "space_slug": space.slug if space else None,
                "unit_type": unit.unit_type.value,
                "title": unit.title,
                "text": unit.content[:2000],
                "score": h.score,
                "scores": h.rank_scores,
                "locator": unit.locator or {},
                "highlight": h.highlight,
                "parent": parent_payload,
                "graph_evidence": list(h.graph_evidence or []),
            }
        )
    return items


async def _load_space(session: AsyncSession, space_id: uuid.UUID) -> Space | None:
    return (await session.execute(select(Space).where(Space.id == space_id))).scalar_one_or_none()


async def graph_switch(session: AsyncSession, settings) -> bool:
    """图谱总开关（P3-API-02）：SystemConfig(graph.enabled) 运行时覆盖，
    未配置时回落 settings.search.graph_enabled；再与 settings.graph.enabled 相与。"""
    from loomvec.api.services.admin_settings import effective_value

    if not settings.graph.enabled:
        return False
    override = await effective_value(session, "graph.enabled")
    if override is None:
        return settings.search.graph_enabled
    return bool(override)
