"""P4-API-01 审计服务：管理域写操作的统一留痕。

- `record_audit`：在既有会话内追加 AuditLog（操作者/动作/对象/前后值/理由/
  请求 ID/IP），由调用方决定提交时机（与管理域写操作同事务提交）；
- 管理域路由经 `admin_audit` 依赖强制兜底留痕（方法+路由+路径参数），
  危险操作端点再显式记录 before/after/reason（docs/04 §六.1）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.core.db.models import AuditLog


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def record_audit(
    session: AsyncSession,
    *,
    identity: Identity | None,
    action: str,
    object_type: str,
    object_id: str | None = None,
    reason: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: Request | None = None,
    commit: bool = False,
) -> AuditLog:
    """追加一条审计记录；commit=True 时立即提交（独立于业务事务的兜底留痕）。"""
    entry = AuditLog(
        tenant_id=uuid.UUID(identity.tenant_id) if identity and identity.tenant_id else None,
        actor_user_id=(
            uuid.UUID(identity.user_id)
            if identity and not identity.user_id.startswith("apikey:")
            else None
        ),
        action=action,
        object_type=object_type,
        object_id=object_id,
        reason=reason,
        before_value=before,
        after_value=after,
        request_id=getattr(request.state, "request_id", None) if request else None,
        ip=_client_ip(request),
    )
    session.add(entry)
    if commit:
        await session.commit()
    return entry


def audit_snapshot(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """ORM 对象 → 审计 before/after 快照（仅取关注字段，datetime 转 ISO）。"""
    out: dict[str, Any] = {}
    for f in fields:
        v = getattr(obj, f, None)
        if isinstance(v, datetime):
            v = v.astimezone(UTC).isoformat()
        out[f] = v
    return out
