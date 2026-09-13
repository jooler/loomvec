"""P2-API-06 通知接口：用户通知列表与已读（复用成员/审核事件）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_scope
from loomvec.api.identity import user_uuid
from loomvec.api.services import notifications as notification_service
from loomvec.core.db.models import Notification
from loomvec.core.db.repos import NotificationRepo
from loomvec.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    payload: dict
    read: bool
    created_at: datetime


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int


@router.get("/notifications", response_model=NotificationListOut)
async def list_notifications(
    unread_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> NotificationListOut:
    user_id = user_uuid(identity)
    rows = await notification_service.list_notifications(
        session, user_id, limit=limit, unread_only=unread_only
    )
    unread = await NotificationRepo(session).unread_count(user_id)
    return NotificationListOut(
        items=[NotificationOut(**notification_service.notification_out(n)) for n in rows],
        unread_count=unread,
    )


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> NotificationOut:
    n: Notification | None = await NotificationRepo(session).get(notification_id)
    if n is None or n.user_id != user_uuid(identity):
        raise NotFoundError(resource="notification", id=str(notification_id))
    from datetime import UTC

    if n.read_at is None:
        n.read_at = datetime.now(UTC)
        await session.commit()
    return NotificationOut(**notification_service.notification_out(n))


@router.post("/notifications/read-all", status_code=204)
async def mark_all_read(
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> None:
    from datetime import UTC

    from sqlalchemy import update

    user_id = user_uuid(identity)
    await session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    await session.commit()
