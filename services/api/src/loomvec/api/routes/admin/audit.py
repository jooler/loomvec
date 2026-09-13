"""P4-ADM-08 审计日志接口：全量筛选、详情、导出（auditor 可用）。

导出为 JSONL（流式拼装，行级 JSON）；写操作 + 登录事件全量留痕由
admin_audit 兜底依赖与各端点显式记录共同保证。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_admin
from loomvec.core.db.models import AuditLog

router = APIRouter(tags=["admin-audit"])


def _out(a: AuditLog) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "tenant_id": str(a.tenant_id) if a.tenant_id else None,
        "actor_user_id": str(a.actor_user_id) if a.actor_user_id else None,
        "action": a.action,
        "object_type": a.object_type,
        "object_id": a.object_id,
        "reason": a.reason,
        "before_value": a.before_value,
        "after_value": a.after_value,
        "request_id": a.request_id,
        "ip": a.ip,
        "created_at": a.created_at.isoformat(),
    }


def _apply_filters(
    stmt,
    *,
    actor: uuid.UUID | None,
    action: str | None,
    object_type: str | None,
    object_id: str | None,
    tenant_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
):
    if actor:
        stmt = stmt.where(AuditLog.actor_user_id == actor)
    if action:
        stmt = stmt.where(AuditLog.action.ilike(f"{action}%"))
    if object_type:
        stmt = stmt.where(AuditLog.object_type == object_type)
    if object_id:
        stmt = stmt.where(AuditLog.object_id == object_id)
    if tenant_id:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)
    return stmt


@router.get("/audit-logs")
async def list_audit_logs(
    actor: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None, max_length=128),
    object_type: str | None = Query(default=None, max_length=64),
    object_id: str | None = Query(default=None, max_length=64),
    tenant_id: uuid.UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = _apply_filters(
        select(AuditLog),
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        tenant_id=tenant_id,
        since=since,
        until=until,
    )
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_out(a) for a in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/audit-logs/export")
async def export_audit_logs(
    actor: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None, max_length=128),
    object_type: str | None = Query(default=None, max_length=64),
    tenant_id: uuid.UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=10000, ge=1, le=100000),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """JSONL 导出（auditor 日常用途；量级受 limit 上限约束）。"""
    stmt = _apply_filters(
        select(AuditLog),
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=None,
        tenant_id=tenant_id,
        since=since,
        until=until,
    )
    rows = (
        (await session.execute(stmt.order_by(AuditLog.created_at.asc()).limit(limit)))
        .scalars()
        .all()
    )
    body = "\n".join(json.dumps(_out(a), ensure_ascii=False, default=str) for a in rows)
    return Response(
        content=body or "\n",
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=audit-logs.jsonl"},
    )
