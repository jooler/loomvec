"""P4-API-08 Webhook 管理接口：订阅 CRUD、投递日志、失败重放。

事件流向：业务侧 publish_event → Redis 事件日志（EVENTS_LOG_KEY，LPUSH 持久
化兜底）→ worker 派发器（beat 轮询）按订阅生成投递行 → webhook.deliver 任务
签名投递；失败按梯度重试，走完置 dead，可人工重放。
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_celery, get_session, require_admin
from loomvec.core.constants import WEBHOOK_EVENT_TYPES
from loomvec.core.db.models import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from loomvec.core.errors import NotFoundError, ValidationError

router = APIRouter(tags=["admin-webhooks"])


class SubscriptionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=8, max_length=1024, description="https:// 回调地址")
    event_types: list[str] = Field(min_length=1)
    description: str | None = None

    @field_validator("event_types")
    @classmethod
    def _check_events(cls, v: list[str]) -> list[str]:
        bad = [e for e in v if e not in WEBHOOK_EVENT_TYPES]
        if bad:
            raise ValueError(f"不可订阅的事件类型: {bad}")
        return list(dict.fromkeys(v))


class SubscriptionPatchRequest(BaseModel):
    name: str | None = None
    url: str | None = None
    event_types: list[str] | None = None
    description: str | None = None
    paused: bool | None = None


def _sub_out(s: WebhookSubscription) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "name": s.name,
        "url": s.url,
        "event_types": s.event_types or [],
        "description": s.description,
        "paused": s.paused,
        "created_at": s.created_at.isoformat(),
    }


def _delivery_out(d: WebhookDelivery) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "subscription_id": str(d.subscription_id),
        "event_type": d.event_type,
        "status": d.status.value,
        "attempt": d.attempt,
        "response_status": d.response_status,
        "error": d.error,
        "next_retry_at": d.next_retry_at.isoformat() if d.next_retry_at else None,
        "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
        "created_at": d.created_at.isoformat(),
        "payload": d.payload,
    }


@router.post("/webhooks/subscriptions", status_code=201)
async def create_subscription(
    body: SubscriptionCreateRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not body.url.startswith(("http://", "https://")):
        raise ValidationError(reason="回调地址必须是 http(s) URL")
    sub = WebhookSubscription(
        name=body.name,
        url=body.url,
        secret=secrets.token_urlsafe(32),
        event_types=body.event_types,
        description=body.description,
        tenant_id=uuid.UUID(identity.tenant_id) if identity.tenant_id else None,
    )
    session.add(sub)
    await record_audit(
        session,
        identity=identity,
        action="admin.webhook.subscription_create",
        object_type="webhook_subscription",
        object_id=str(sub.id),
        after={"name": body.name, "url": body.url, "event_types": body.event_types},
    )
    await session.commit()
    out = _sub_out(sub)
    out["secret"] = sub.secret  # 明文仅此一次
    return out


@router.get("/webhooks/subscriptions")
async def list_subscriptions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(WebhookSubscription).where(WebhookSubscription.deleted_at.is_(None))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    subs = (
        (
            await session.execute(
                stmt.order_by(WebhookSubscription.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_sub_out(s) for s in subs], "total": total, "limit": limit, "offset": offset}


@router.patch("/webhooks/subscriptions/{sub_id}")
async def patch_subscription(
    sub_id: uuid.UUID,
    body: SubscriptionPatchRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    sub = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == sub_id, WebhookSubscription.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError(resource="webhook_subscription", id=str(sub_id))
    if body.name is not None:
        sub.name = body.name
    if body.url is not None:
        if not body.url.startswith(("http://", "https://")):
            raise ValidationError(reason="回调地址必须是 http(s) URL")
        sub.url = body.url
    if body.event_types is not None:
        bad = [e for e in body.event_types if e not in WEBHOOK_EVENT_TYPES]
        if bad:
            raise ValidationError(reason=f"不可订阅的事件类型: {bad}")
        sub.event_types = list(dict.fromkeys(body.event_types))
    if body.description is not None:
        sub.description = body.description
    if body.paused is not None:
        sub.paused = body.paused
    await record_audit(
        session,
        identity=identity,
        action="admin.webhook.subscription_update",
        object_type="webhook_subscription",
        object_id=str(sub_id),
        after={"paused": sub.paused, "url": sub.url},
    )
    await session.commit()
    return _sub_out(sub)


@router.post("/webhooks/subscriptions/{sub_id}/reset-secret")
async def reset_secret(
    sub_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    sub = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == sub_id, WebhookSubscription.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError(resource="webhook_subscription", id=str(sub_id))
    sub.secret = secrets.token_urlsafe(32)
    await record_audit(
        session,
        identity=identity,
        action="admin.webhook.subscription_reset_secret",
        object_type="webhook_subscription",
        object_id=str(sub_id),
    )
    await session.commit()
    return {"secret": sub.secret}


@router.delete("/webhooks/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    from datetime import UTC

    sub = (
        await session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == sub_id, WebhookSubscription.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        raise NotFoundError(resource="webhook_subscription", id=str(sub_id))
    sub.deleted_at = datetime.now(UTC)
    await record_audit(
        session,
        identity=identity,
        action="admin.webhook.subscription_delete",
        object_type="webhook_subscription",
        object_id=str(sub_id),
    )
    await session.commit()


@router.get("/webhooks/{sub_id}/deliveries")
async def list_deliveries(
    sub_id: uuid.UUID,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(WebhookDelivery).where(WebhookDelivery.subscription_id == sub_id)
    if status:
        stmt = stmt.where(WebhookDelivery.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(WebhookDelivery.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_delivery_out(d) for d in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/webhooks/deliveries/{delivery_id}/replay", status_code=202)
async def replay_delivery(
    delivery_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> dict[str, Any]:
    """失败/死信重放：状态重置为 pending 并重新派发投递任务。"""
    from loomvec.core.constants import QUEUE_PIPELINE_HIGH

    delivery = (
        await session.execute(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id))
    ).scalar_one_or_none()
    if delivery is None:
        raise NotFoundError(resource="webhook_delivery", id=str(delivery_id))
    if delivery.status not in (WebhookDeliveryStatus.FAILED, WebhookDeliveryStatus.DEAD):
        raise ValidationError(reason="仅失败/死信投递可重放", status=delivery.status.value)
    delivery.status = WebhookDeliveryStatus.PENDING
    delivery.next_retry_at = None
    delivery.error = None
    await record_audit(
        session,
        identity=identity,
        action="admin.webhook.delivery_replay",
        object_type="webhook_delivery",
        object_id=str(delivery_id),
    )
    await session.commit()
    celery.send_task("webhook.deliver", args=[str(delivery.id)], queue=QUEUE_PIPELINE_HIGH)
    return {"id": str(delivery_id), "replayed": True}
