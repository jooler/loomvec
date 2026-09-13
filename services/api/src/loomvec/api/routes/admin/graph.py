"""P4-API-04 图谱治理收编：跨空间合并日志查阅与平台级回滚。

空间 owner 视角的图谱管理在用户域 `routes/graph.py`；本文件提供运维端
跨空间视图（merge_log 全局检索 + 平台回滚），写操作强制审计。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_admin
from loomvec.core.db.models import Entity, EntityMergeLog, MergeLogStatus, Space
from loomvec.core.errors import NotFoundError, ValidationError

router = APIRouter(prefix="/graph", tags=["admin-graph"])


class AdminMergeLogOut(BaseModel):
    log_id: uuid.UUID
    space_id: uuid.UUID
    space_slug: str | None = None
    winner_id: uuid.UUID
    loser_id: uuid.UUID | None
    status: str
    reason: str
    score: float | None
    created_by: str | None
    created_at: Any = None
    rolled_back_at: Any | None = None
    winner_name: str | None = None
    loser_name: str | None = None


class AdminMergeLogPage(BaseModel):
    items: list[AdminMergeLogOut]
    total: int


@router.get("/merges", response_model=AdminMergeLogPage)
async def list_merge_logs(
    space_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> AdminMergeLogPage:
    """跨空间合并日志（merge_log 全局检索；space_id/status 可选过滤）。"""
    base = select(EntityMergeLog)
    if space_id is not None:
        base = base.where(EntityMergeLog.space_id == space_id)
    if status:
        base = base.where(EntityMergeLog.status == status)
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
    # 批量回填空间 slug 与实体名（列表页规模有限）
    space_ids = {log.space_id for log in rows}
    slugs = (
        {
            s.id: s.slug
            for s in (await session.execute(select(Space).where(Space.id.in_(space_ids)))).scalars()
        }
        if space_ids
        else {}
    )
    entity_ids = {log.winner_id for log in rows} | {log.loser_id for log in rows if log.loser_id}
    names = (
        {
            e.id: e.name
            for e in (
                await session.execute(select(Entity).where(Entity.id.in_(entity_ids)))
            ).scalars()
        }
        if entity_ids
        else {}
    )
    items = [
        AdminMergeLogOut(
            log_id=log.id,
            space_id=log.space_id,
            space_slug=slugs.get(log.space_id),
            winner_id=log.winner_id,
            loser_id=log.loser_id,
            status=log.status.value,
            reason=log.reason,
            score=log.score,
            created_by=log.created_by,
            created_at=log.created_at,
            rolled_back_at=log.rolled_back_at,
            winner_name=names.get(log.winner_id),
            loser_name=names.get(log.loser_id),
        )
        for log in rows
    ]
    return AdminMergeLogPage(items=items, total=total)


class RollbackRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post("/merges/{log_id}/rollback")
async def rollback_merge(
    log_id: uuid.UUID,
    body: RollbackRequest,
    request: Request,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """平台级误合并回滚：按 merge_log 快照逆向恢复实体与图边（强制审计）。"""
    from loomvec.core.graph.merge import rollback_merge as do_rollback

    log = (
        await session.execute(select(EntityMergeLog).where(EntityMergeLog.id == log_id))
    ).scalar_one_or_none()
    if log is None:
        raise NotFoundError(resource="merge_log", id=str(log_id))
    if log.status != MergeLogStatus.APPLIED:
        raise ValidationError(reason="该合并已回滚")
    try:
        rolled = await do_rollback(session, None, log_id)
    except (LookupError, ValueError) as e:
        raise ValidationError(str(e)) from e
    await record_audit(
        session,
        identity=identity,
        action="admin.graph.merge_rollback",
        object_type="merge_log",
        object_id=str(log_id),
        reason=body.reason,
        before={"status": log.status.value, "space_id": str(log.space_id)},
        after={"status": rolled.status.value},
        request=request,
    )
    await session.commit()
    return {
        "log_id": str(rolled.id),
        "space_id": str(rolled.space_id),
        "status": rolled.status.value,
        "rolled_back_at": rolled.rolled_back_at,
    }
