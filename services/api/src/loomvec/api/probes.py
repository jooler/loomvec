"""组件健康探测单源：`/readyz`、`/admin/system/status` 与 `/metrics` gauge 刷新共用。

探测超时统一 2s；单条依赖失败只降级该项，PostgreSQL 失败整体 503（readyz 语义）。
worker 心跳（P1-WRK-01）为尽力探测：无心跳记录时视为降级而非失败。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from loomvec.core.config import Settings
from loomvec.core.constants import HEARTBEAT_KEY
from loomvec.core.storage import ObjectStorage

PROBE_TIMEOUT = 2.0

# 七组件：api 进程本身恒为 ok；其余探测
COMPONENT_KEYS = ("api", "worker", "postgres", "milvus", "storage", "redis", "mineru")


async def check_postgres(engine) -> dict[str, Any]:
    from sqlalchemy import text

    async with engine.connect() as conn:
        version = (await conn.execute(text("select version()"))).scalar_one()
    return {"ok": True, "version": version.split(",")[0]}


async def check_redis(redis_client) -> dict[str, Any]:
    return {"ok": True, "pong": await redis_client.ping()}


async def check_storage(storage: ObjectStorage) -> dict[str, Any]:
    def _probe() -> None:
        storage._client.head_bucket(Bucket=storage._settings.bucket_raw)

    await asyncio.to_thread(_probe)
    return {"ok": True}


async def check_milvus(milvus) -> dict[str, Any]:
    exists = await milvus.async_collection_exists()
    return {"ok": True, "collection": exists}


async def check_worker(redis_client) -> dict[str, Any]:
    """worker 心跳：beat 周期写 Redis（P1-WRK-01）；无记录视为降级而非失败。"""
    raw = await redis_client.get(HEARTBEAT_KEY)
    if not raw:
        return {"ok": False, "degraded": True, "error": "无心跳记录（worker 未运行或刚启动）"}
    ts = json.loads(raw).get("ts")
    age = max(0.0, time.time() - float(ts))
    return {"ok": True, "age_seconds": round(age, 1)}


async def check_mineru(settings: Settings) -> dict[str, Any]:
    """MinerU 解析服务连通性（轻量 GET /health 或根路径）。"""
    import httpx

    base = settings.mineru.base_url.rstrip("/")
    # 内网服务直连，不走环境代理（trust_env=False）
    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
        try:
            resp = await client.get(f"{base}/health")
        except httpx.HTTPStatusError:
            resp = await client.get(base)
    return {"ok": resp.status_code < 500, "status_code": resp.status_code}


async def safe(coro) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def probe_components(state) -> dict[str, dict[str, Any]]:
    """按 COMPONENT_KEYS 探测全部组件（api 恒 ok；依赖未初始化记 not initialised）。"""
    settings: Settings = state.settings
    engine = getattr(state, "engine", None)
    redis_client = getattr(state, "redis", None)
    storage = getattr(state, "storage", None)
    milvus = getattr(state, "milvus", None)

    checks: dict[str, Any] = {"api": {"ok": True}}
    if engine is not None:
        checks["postgres"] = await safe(check_postgres(engine))
    if redis_client is not None:
        checks["redis"] = await safe(check_redis(redis_client))
        checks["worker"] = await safe(check_worker(redis_client))
    if storage is not None:
        checks["storage"] = await safe(check_storage(storage))
    if milvus is not None:
        checks["milvus"] = await safe(check_milvus(milvus))
    checks["mineru"] = await safe(check_mineru(settings))
    return checks
