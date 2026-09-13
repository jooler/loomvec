"""P2-API-06 通知服务：成员/审核事件落用户收件箱（同事务写入）。

事件同时经 Redis 广播（P1 事件通道）与 notifications 表持久化；
API 只负责列表/已读，事件产生方（成员/审核服务）调用 `notify`。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import Notification
from loomvec.core.db.repos import NotificationRepo


async def notify(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    type_: str,
    title: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """写用户通知（user_id 为空时跳过，如 API Key 触发的操作）。"""
    if user_id is None:
        return
    session.add(
        Notification(
            tenant_id=tenant_id,
            user_id=user_id,
            type=type_,
            title=title,
            payload=payload or {},
        )
    )
    await session.flush()


def notification_out(n: Notification) -> dict:
    return {
        "id": n.id,
        "type": n.type,
        "title": n.title,
        "payload": n.payload,
        "read": n.read_at is not None,
        "created_at": n.created_at,
    }


async def list_notifications(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50, unread_only: bool = False
) -> list[Notification]:
    return await NotificationRepo(session).list_for_user(
        user_id, limit=limit, unread_only=unread_only
    )
