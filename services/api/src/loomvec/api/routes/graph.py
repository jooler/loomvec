"""P3-API-03 图谱管理接口（用户端 owner 视角；P4 收编运维端）。

- `GET    /spaces/{space_id}/graph/entities`        实体列表（成员可读）
- `GET    /spaces/{space_id}/graph/stats`           实体/关系统计
- `GET    /spaces/{space_id}/graph/merges`          合并日志（merge_log 查阅）
- `POST   /spaces/{space_id}/graph/merges/{lid}/rollback`  误合并回滚（owner）
- `POST   /spaces/{space_id}/graph/merge/run`       手动触发空间合并（owner）
- `POST   /spaces/{space_id}/graph/rebuild`         图谱重建触发（owner，低优队列）
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_celery,
    get_session,
    require_scope,
    resolve_space_access,
)
from loomvec.core.authz import SpaceRole
from loomvec.core.constants import QUEUE_PIPELINE_LOW
from loomvec.core.db.models import Entity, EntityMergeLog
from loomvec.core.errors import ValidationError

router = APIRouter(prefix="/api/v1/spaces/{space_id}/graph", tags=["graph"])


class EntityOut(BaseModel):
    entity_id: uuid.UUID
    entity_key: str
    name: str
    type: str
    description: str | None
    aliases: list[str]
    community_id: uuid.UUID | None
    merged_into: uuid.UUID | None
    created_at: Any = None


class EntityPage(BaseModel):
    items: list[EntityOut]
    total: int


class MergeLogOut(BaseModel):
    log_id: uuid.UUID
    winner_id: uuid.UUID
    loser_id: uuid.UUID | None
    status: str
    reason: str
    score: float | None
    created_by: str | None
    created_at: Any = None
    rolled_back_at: Any | None = None
    snapshot_names: dict[str, Any] = Field(default_factory=dict)


class MergeLogPage(BaseModel):
    items: list[MergeLogOut]
    total: int


@router.get("/entities", response_model=EntityPage)
async def list_entities(
    space_id: uuid.UUID,
    q: str | None = None,
    type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> EntityPage:
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    stmt = (
        select(Entity)
        .where(
            Entity.space_id == space_id,
            Entity.deleted_at.is_(None),
            Entity.merged_into.is_(None),
        )
        .order_by(Entity.created_at)
    )
    if q:
        stmt = stmt.where(Entity.name_norm.ilike(f"%{q}%"))
    if type:
        stmt = stmt.where(Entity.type == type)
    total = int(
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    )
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    items = [
        EntityOut(
            entity_id=e.id,
            entity_key=e.entity_key,
            name=e.name,
            type=e.type,
            description=e.description,
            aliases=list(e.aliases or []),
            community_id=e.community_id,
            merged_into=e.merged_into,
            created_at=e.created_at,
        )
        for e in rows
    ]
    return EntityPage(items=items, total=total)


@router.get("/stats")
async def graph_stats(
    space_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    from loomvec.core.graph.age import AgeStore

    entity_count = int(
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
    merges = int(
        (
            await session.execute(
                select(func.count())
                .select_from(EntityMergeLog)
                .where(EntityMergeLog.space_id == space_id)
            )
        ).scalar_one()
    )
    edge_count = 0
    try:
        edge_count = await AgeStore().space_edge_count(session, str(space_id))
    except Exception:  # AGE 不可用时统计降级
        edge_count = -1
    return {
        "space_id": str(space_id),
        "entities": entity_count,
        "edges": edge_count,
        "merges": merges,
    }


@router.get("/merges", response_model=MergeLogPage)
async def list_merges(
    space_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> MergeLogPage:
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    base = select(EntityMergeLog).where(EntityMergeLog.space_id == space_id)
    total = int(
        (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    )
    rows = (
        (
            await session.execute(
                base.order_by(EntityMergeLog.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    items = [
        MergeLogOut(
            log_id=log.id,
            winner_id=log.winner_id,
            loser_id=log.loser_id,
            status=log.status.value,
            reason=log.reason,
            score=log.score,
            created_by=log.created_by,
            created_at=log.created_at,
            rolled_back_at=log.rolled_back_at,
            snapshot_names={
                "loser": (log.snapshot or {}).get("loser", {}).get("name"),
                "winner": None,  # 胜者名单独查（避免 N+1 时全量快照下发）
            },
        )
        for log in rows
    ]
    # 胜者名称回填（列表页规模有限）
    winner_ids = {log.winner_id for log in rows}
    if winner_ids:
        names = {
            e.id: e.name
            for e in (await session.execute(select(Entity).where(Entity.id.in_(winner_ids))))
            .scalars()
            .all()
        }
        for item in items:
            item.snapshot_names["winner"] = names.get(item.winner_id)
    return MergeLogPage(items=items, total=total)


@router.post("/merges/{log_id}/rollback")
async def rollback_merge(
    space_id: uuid.UUID,
    log_id: uuid.UUID,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """误合并回滚（owner）：按 merge_log 快照逆向恢复实体与图边。"""
    from loomvec.core.graph.merge import rollback_merge as do_rollback

    await resolve_space_access(session, identity, space_id, SpaceRole.OWNER)
    try:
        log = await do_rollback(session, None, log_id)
    except LookupError as e:
        raise ValidationError(str(e)) from e
    except ValueError as e:
        raise ValidationError(str(e)) from e
    if str(log.space_id) != str(space_id):
        raise ValidationError("merge_log 与空间不匹配")
    await session.commit()
    return {
        "log_id": str(log.id),
        "status": log.status.value,
        "rolled_back_at": log.rolled_back_at,
    }


class MergeRunRequest(BaseModel):
    created_by: str | None = None


@router.post("/merge/run")
async def run_merge(
    space_id: uuid.UUID,
    body: MergeRunRequest | None = None,
    identity: Identity = Depends(require_scope("write")),
    request: Request = None,
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> dict:
    """手动触发空间合并任务（owner；低优队列异步执行）。"""
    await resolve_space_access(session, identity, space_id, SpaceRole.OWNER)
    celery.send_task(
        "graph.merge_space",
        args=[str(space_id), identity.user_id],
        queue=QUEUE_PIPELINE_LOW,
    )
    _ = body
    return {"status": "queued", "space_id": str(space_id)}


class RebuildRequest(BaseModel):
    reprocess_assets: bool = True  # 是否重跑各资产 graph 步骤（默认是）


@router.post("/rebuild")
async def rebuild(
    space_id: uuid.UUID,
    body: RebuildRequest | None = None,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> dict:
    """图谱重建（owner）：清空空间子图，逐资产重跑 graph 步骤（低优队列）。

    实体主表/merge_log 保留（审计与链接依据）；AGE 子图从事实源重建，
    Milvus entities 由 graph 步骤的幂等 upsert 收敛。
    """
    from loomvec.core.db.models import Asset
    from loomvec.core.graph.age import AgeStore

    await resolve_space_access(session, identity, space_id, SpaceRole.OWNER)
    reprocess = body.reprocess_assets if body else True

    asset_ids: list[str] = []
    if reprocess:
        asset_ids = [
            str(a)
            for a in (
                await session.execute(
                    select(Asset.id).where(Asset.space_id == space_id, Asset.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        ]
    try:
        await AgeStore().clear_space(session, str(space_id))
    except Exception:
        await session.rollback()
    await session.commit()

    for aid in asset_ids:
        celery.send_task("pipeline.process_asset", args=[aid, "graph"], queue=QUEUE_PIPELINE_LOW)
    return {"status": "queued", "space_id": str(space_id), "assets": len(asset_ids)}
