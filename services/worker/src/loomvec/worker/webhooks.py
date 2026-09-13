"""P4-API-08 Webhook 投递执行器（worker 侧）。

- `webhook.dispatch`（beat 每 30s）：轮询 Redis 事件日志游标 →
  按订阅（event_types 匹配、未暂停）生成投递行并派发 `webhook.deliver`；
- `webhook.deliver`：HMAC-SHA256 签名投递；失败按 WEBHOOK_RETRY_SCHEDULE_SECONDS
  梯度重试（send_task 延迟派发），走完梯度置 dead（等待人工重放）；
- `webhook.retry_due`（beat）：扫到期的 failed 行重新入队（进程重启后的补投）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import redis as sync_redis
from celery import Task
from sqlalchemy import select

from loomvec.core.config import get_settings
from loomvec.core.constants import (
    EVENTS_LOG_KEY,
    QUEUE_PIPELINE_HIGH,
    WEBHOOK_CURSOR_KEY,
    WEBHOOK_EVENT_HEADER,
    WEBHOOK_RETRY_SCHEDULE_SECONDS,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_TIMEOUT_SECONDS,
)
from loomvec.core.db.base import create_engine_and_sessionmaker
from loomvec.core.db.models import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from loomvec.core.logging import get_logger
from loomvec.worker.celery_app import celery_app

logger = get_logger("loomvec.worker.webhooks")

DISPATCH_BATCH = 200


def _sync_redis() -> sync_redis.Redis:
    return sync_redis.from_url(get_settings().redis.url, decode_responses=True)


@celery_app.task(name="webhook.dispatch")
def dispatch_events() -> dict:
    """事件日志 → 投递行：按游标读取未派发事件，匹配订阅后生成投递任务。"""

    async def _run() -> dict:
        settings = get_settings()
        engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
        client = _sync_redis()
        dispatched = 0
        try:
            # LRANGE 从游标向左读（LPUSH 新事件在头部；游标为已读位置索引）
            total = client.llen(EVENTS_LOG_KEY)
            cursor = int(client.get(WEBHOOK_CURSOR_KEY) or 0)
            if total <= cursor:
                client.set(WEBHOOK_CURSOR_KEY, total)
                return {"dispatched": 0}
            # 读取游标之后的事件（新→旧存储，倒序读出正序处理）
            events = client.lrange(EVENTS_LOG_KEY, cursor, total - 1)
            events = list(reversed(events))
            async with session_factory() as session:
                subs = (
                    (
                        await session.execute(
                            select(WebhookSubscription).where(
                                WebhookSubscription.deleted_at.is_(None),
                                WebhookSubscription.paused.is_(False),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for payload in events[:DISPATCH_BATCH]:
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    etype = event.get("type")
                    for sub in subs:
                        if etype in (sub.event_types or []):
                            delivery = WebhookDelivery(
                                subscription_id=sub.id,
                                event_type=etype,
                                payload=event,
                                status=WebhookDeliveryStatus.PENDING,
                            )
                            session.add(delivery)
                            await session.flush()
                            celery_app.send_task(
                                "webhook.deliver",
                                args=[str(delivery.id)],
                                queue=QUEUE_PIPELINE_HIGH,
                            )
                            dispatched += 1
                await session.commit()
            client.set(WEBHOOK_CURSOR_KEY, cursor + min(len(events), DISPATCH_BATCH))
        finally:
            client.close()
            await engine.dispose()
        return {"dispatched": dispatched}

    return asyncio.run(_run())


@celery_app.task(
    name="webhook.deliver",
    bind=True,
    max_retries=len(WEBHOOK_RETRY_SCHEDULE_SECONDS),
    autoretry_for=(),
)
def deliver_webhook(self: Task, delivery_id: str) -> dict:
    """签名投递：HMAC-SHA256(secret, body)；失败按梯度排期重试或置 dead。"""

    async def _run() -> dict:
        settings = get_settings()
        engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
        try:
            async with session_factory() as session:
                delivery = (
                    await session.execute(
                        select(WebhookDelivery).where(WebhookDelivery.id == uuid.UUID(delivery_id))
                    )
                ).scalar_one_or_none()
                if delivery is None:
                    return {"status": "missing"}
                sub = (
                    await session.execute(
                        select(WebhookSubscription).where(
                            WebhookSubscription.id == delivery.subscription_id
                        )
                    )
                ).scalar_one_or_none()
                if sub is None or sub.deleted_at is not None:
                    delivery.status = WebhookDeliveryStatus.DEAD
                    delivery.error = "订阅已删除"
                    await session.commit()
                    return {"status": "dead", "reason": "subscription removed"}

                body = json.dumps(
                    {"id": delivery_id, "type": delivery.event_type, "data": delivery.payload},
                    ensure_ascii=False,
                    default=str,
                ).encode()
                signature = hmac.new(sub.secret.encode(), body, hashlib.sha256).hexdigest()
                delivery.attempt += 1
                try:
                    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as http:
                        resp = await http.post(
                            sub.url,
                            content=body,
                            headers={
                                "Content-Type": "application/json",
                                WEBHOOK_SIGNATURE_HEADER: f"sha256={signature}",
                                WEBHOOK_EVENT_HEADER: delivery.event_type,
                            },
                        )
                    delivery.response_status = resp.status_code
                    if 200 <= resp.status_code < 300:
                        delivery.status = WebhookDeliveryStatus.SUCCESS
                        delivery.delivered_at = datetime.now(UTC)
                        delivery.next_retry_at = None
                        delivery.error = None
                        await session.commit()
                        return {"status": "success"}
                    raise RuntimeError(f"HTTP {resp.status_code}")
                except Exception as e:
                    delivery.status = WebhookDeliveryStatus.FAILED
                    delivery.error = str(e)[:2000]
                    schedule = WEBHOOK_RETRY_SCHEDULE_SECONDS
                    if delivery.attempt > len(schedule):
                        delivery.status = WebhookDeliveryStatus.DEAD
                        delivery.next_retry_at = None
                        await session.commit()
                        return {"status": "dead", "error": str(e)}
                    delay = schedule[delivery.attempt - 1]
                    delivery.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                    await session.commit()
                    celery_app.send_task(
                        "webhook.deliver",
                        args=[delivery_id],
                        queue=QUEUE_PIPELINE_HIGH,
                        countdown=delay,
                    )
                    return {"status": "retry_scheduled", "delay": delay}
        finally:
            await engine.dispose()

    return asyncio.run(_run())


@celery_app.task(name="webhook.retry_due")
def retry_due_deliveries() -> dict:
    """补投：扫 next_retry_at 到期的 failed 行（进程重启丢失延迟任务的兜底）。"""

    async def _run() -> dict:
        settings = get_settings()
        engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
        requeued = 0
        try:
            async with session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(WebhookDelivery).where(
                                WebhookDelivery.status == WebhookDeliveryStatus.FAILED,
                                WebhookDelivery.next_retry_at.isnot(None),
                                WebhookDelivery.next_retry_at <= datetime.now(UTC),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for d in rows:
                    d.next_retry_at = None
                    celery_app.send_task(
                        "webhook.deliver",
                        args=[str(d.id)],
                        queue=QUEUE_PIPELINE_HIGH,
                    )
                    requeued += 1
                await session.commit()
        finally:
            await engine.dispose()
        if requeued:
            logger.info("webhook_retry_due", requeued=requeued)
        return {"requeued": requeued}

    return asyncio.run(_run())
