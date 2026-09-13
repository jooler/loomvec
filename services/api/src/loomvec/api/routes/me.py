"""用户域路由：`/api/v1/me` 返回当前身份与个人中心摘要（P2-WEB-07）。

摘要部分（未读通知/租户名）需要 DB；DB 不可用时退回纯身份信息
（契约字段不缺），与 P0 无状态 /me 行为兼容。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from loomvec.api.context import Identity
from loomvec.api.deps import get_identity

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.get("/me")
async def me(
    identity: Identity = Depends(get_identity),
    request: Request = None,
) -> dict:
    data = {
        "user_id": identity.user_id,
        "username": identity.username,
        "tenant_id": identity.tenant_id,
        "roles": list(identity.roles),
    }
    if identity.user_id.startswith("apikey:"):
        return data
    try:
        from sqlalchemy import select

        from loomvec.core.db.models import Tenant
        from loomvec.core.db.repos import NotificationRepo

        session_factory = request.app.state.session_factory
        async with session_factory() as session:
            data["unread_notifications"] = await NotificationRepo(session).unread_count(
                uuid.UUID(identity.user_id)
            )
            if identity.tenant_id:
                tenant = (
                    await session.execute(select(Tenant).where(Tenant.id == identity.tenant_id))
                ).scalar_one_or_none()
                if tenant:
                    data["tenant_name"] = tenant.name
    except Exception:  # DB 不可用时退回纯身份信息（lifespan 未跑/连库失败）
        pass
    return data
