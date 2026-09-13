"""/metrics 运行时 gauge 刷新：队列深度、组件健康、空间配额用量。

每次 Prometheus 抓取（默认 15s）时尽力刷新；组件探测与配额查询带 TTL 缓存，
避免高频抓取打爆依赖。任何刷新失败只跳过对应 gauge，不阻塞指标导出。
"""

from __future__ import annotations

import time

from sqlalchemy import select

from loomvec.api.probes import COMPONENT_KEYS, probe_components
from loomvec.core import metrics
from loomvec.core.constants import (
    DEAD_LETTER_KEY,
    QUEUE_PIPELINE,
    QUEUE_PIPELINE_HIGH,
    QUEUE_PIPELINE_LOW,
)
from loomvec.core.db.models import Space, SpaceUsage

_COMPONENT_TTL_SECONDS = 30.0
_USAGE_TTL_SECONDS = 60.0

_cache: dict[str, tuple[float, object]] = {"components": (0.0, None), "usage": (0.0, None)}


def _fresh(key: str, ttl: float) -> object | None:
    ts, value = _cache[key]
    return value if value is not None and time.monotonic() - ts < ttl else None


async def _refresh_queues(redis) -> None:
    for queue in (QUEUE_PIPELINE, QUEUE_PIPELINE_HIGH, QUEUE_PIPELINE_LOW):
        depth = await redis.llen(f"queue:{queue}")
        metrics.queue_length.labels(queue=queue).set(depth)
    metrics.dead_letter_size.set(await redis.llen(DEAD_LETTER_KEY))


async def _refresh_components(state) -> None:
    cached = _fresh("components", _COMPONENT_TTL_SECONDS)
    if cached is None:
        cached = await probe_components(state)
        _cache["components"] = (time.monotonic(), cached)
    for key in COMPONENT_KEYS:
        ok = bool(cached.get(key, {}).get("ok", False))
        metrics.component_up.labels(component=key).set(1.0 if ok else 0.0)


async def _refresh_space_usage(session_factory) -> None:
    cached = _fresh("usage", _USAGE_TTL_SECONDS)
    if cached is None:
        session = session_factory()
        try:
            rows = (
                await session.execute(
                    select(
                        Space.slug, Space.quota_storage_bytes, Space.quota_file_count, SpaceUsage
                    )
                    .join(SpaceUsage, SpaceUsage.space_id == Space.id)
                    .where(
                        Space.deleted_at.is_(None),
                        (Space.quota_storage_bytes > 0) | (Space.quota_file_count > 0),
                    )
                )
            ).all()
        finally:
            await session.close()
        cached = rows
        _cache["usage"] = (time.monotonic(), rows)
    for slug, quota_bytes, quota_files, usage in cached:
        if quota_bytes:
            metrics.space_usage_ratio.labels(space=slug, kind="storage").set(
                usage.storage_bytes / quota_bytes
            )
        if quota_files:
            metrics.space_usage_ratio.labels(space=slug, kind="files").set(
                usage.file_count / quota_files
            )


async def refresh_runtime_metrics(state) -> None:
    """刷新运行时 gauge（尽力而为；依赖未初始化或不可达时跳过对应组）。"""
    redis = getattr(state, "redis", None)
    if redis is not None:
        await _refresh_queues(redis)
    if getattr(state, "settings", None) is not None:
        await _refresh_components(state)
    session_factory = getattr(state, "session_factory", None)
    if session_factory is not None:
        await _refresh_space_usage(session_factory)
