"""领域事件发布单源：Redis 广播（EVENTS_CHANNEL）+ 持久化事件日志（P4）。

- 广播：SSE 等实时消费方（发布后即逝）；
- 事件日志：LPUSH 至 EVENTS_LOG_KEY（有界列表），Webhook 派发器
  （worker beat 轮询游标）据此生成投递——pub/sub 无持久化，Webhook 需要它。
通知的持久化（notifications 表）由调用方负责；事件失败不影响业务（记 warning）。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from loomvec.core.constants import EVENTS_CHANNEL, EVENTS_LOG_KEY, EVENTS_LOG_MAX_LEN

logger = structlog.get_logger("loomvec.events")


async def publish_event(redis: Any | None, event: dict[str, Any]) -> None:
    if redis is None:
        return
    try:
        payload = json.dumps(event, default=str)
        await redis.publish(EVENTS_CHANNEL, payload)
        # P4-API-08：持久化事件日志（Webhook 派发数据源；有界，丢老保新）
        await redis.lpush(EVENTS_LOG_KEY, payload)
        await redis.ltrim(EVENTS_LOG_KEY, 0, EVENTS_LOG_MAX_LEN - 1)
    except Exception as e:  # 事件失败不影响业务
        logger.warning("event_publish_failed", event=event.get("type"), error=str(e))
