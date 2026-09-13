"""P4-API-02 状态聚合接口：七组件健康、队列深度、管线态势、平台规模、备份状态。

数据来源约定（docs/04 §七）：组件健康复用 readyz 探测逻辑，管线态势聚合
processing_job，队列深度/死信读 Redis，备份状态读 system_config（由备份脚本
回写，见 scripts/backup.sh）。细粒度指标由 Prometheus/Grafana 承担，此处为
运维端总览页的聚合视图。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_redis, get_session, require_admin
from loomvec.api.probes import (
    COMPONENT_KEYS,
    check_milvus,
    check_mineru,
    check_postgres,
    check_redis,
    check_storage,
    check_worker,
    safe,
)
from loomvec.core.config import Settings
from loomvec.core.constants import (
    DEAD_LETTER_KEY,
    QUEUE_PIPELINE,
    QUEUE_PIPELINE_HIGH,
)
from loomvec.core.db.models import Asset, ProcessingJob, SemanticUnit, Space, Tenant, User

router = APIRouter(tags=["admin-system"])


async def _queue_depths(redis) -> dict[str, int]:
    depths = {
        QUEUE_PIPELINE: await redis.llen(f"queue:{QUEUE_PIPELINE}") or 0,
        QUEUE_PIPELINE_HIGH: await redis.llen(f"queue:{QUEUE_PIPELINE_HIGH}") or 0,
    }
    return depths


@router.get("/system/status")
async def system_status(
    request: Request,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
) -> dict[str, Any]:
    state = request.app.state
    settings: Settings = state.settings
    engine = getattr(state, "engine", None)
    storage = getattr(state, "storage", None)
    milvus = getattr(state, "milvus", None)

    # ---- 七组件健康 ----
    checks: dict[str, Any] = {"api": {"ok": True}}
    if engine is not None:
        checks["postgres"] = await safe(check_postgres(engine))
    if redis is not None:
        checks["redis"] = await safe(check_redis(redis))
        checks["worker"] = await safe(check_worker(redis))
    if storage is not None:
        checks["storage"] = await safe(check_storage(storage))
    if milvus is not None:
        checks["milvus"] = await safe(check_milvus(milvus))
    checks["mineru"] = await safe(check_mineru(settings))
    components = [
        {
            "name": key,
            "ok": bool(checks.get(key, {}).get("ok", False)),
            "detail": checks.get(key, {"ok": False, "error": "not initialised"}),
        }
        for key in COMPONENT_KEYS
    ]

    # ---- 管线态势（24h）----
    # asyncpg 对 naive timestamp 列绑定 aware 参数会报错，这里统一用 naive UTC
    since = (datetime.now(UTC) - timedelta(hours=24)).replace(tzinfo=None)

    throughput_rows = (
        await session.execute(
            select(ProcessingJob.status, func.count())
            .where(ProcessingJob.created_at >= since)
            .group_by(ProcessingJob.status)
        )
    ).all()
    by_status = {status.value: count for status, count in throughput_rows}
    total_24h = sum(by_status.values())
    failed_24h = by_status.get("failed", 0)
    dur = await session.execute(
        text(
            """
            SELECT job_type,
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))
                   ) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (finished_at - started_at))
                   ) AS p95
            FROM processing_job
            WHERE started_at IS NOT NULL AND finished_at IS NOT NULL
              AND created_at >= :since
            GROUP BY job_type
            """
        ),
        {"since": since},
    )
    stage_latency = {
        # 原生 SQL 返回字符串而非枚举，需兼容两种形态
        getattr(row.job_type, "value", row.job_type): {
            "p50": round(float(row.p50), 3),
            "p95": round(float(row.p95), 3),
        }
        for row in dur
    }

    # ---- 队列与死信 ----
    queue_depths = await _queue_depths(redis)
    dead_letter = await redis.llen(DEAD_LETTER_KEY)

    # ---- 平台规模 ----
    scale_row = (
        await session.execute(
            select(
                select(func.count()).select_from(Tenant).scalar_subquery(),
                select(func.count()).select_from(User).scalar_subquery(),
                select(func.count()).select_from(Space).scalar_subquery(),
                select(func.count()).select_from(Asset).scalar_subquery(),
                select(func.count()).select_from(SemanticUnit).scalar_subquery(),
            )
        )
    ).first()
    counts = dict(
        zip(
            ("tenants", "users", "spaces", "assets", "semantic_units"),
            scale_row,
            strict=True,
        )
    )
    # SpaceUsage 是计量单源
    from loomvec.core.db.models import SpaceUsage

    used = (
        await session.execute(select(func.coalesce(func.sum(SpaceUsage.storage_bytes), 0)))
    ).scalar_one()
    file_count = (
        await session.execute(select(func.coalesce(func.sum(SpaceUsage.file_count), 0)))
    ).scalar_one()

    # ---- 备份状态（scripts/backup.sh 回写 system_config）----
    from loomvec.core.db.models import SystemConfig

    backup_row = (
        await session.execute(select(SystemConfig).where(SystemConfig.key == "backup.last_status"))
    ).scalar_one_or_none()
    backup_status = backup_row.value if backup_row else {"state": "unknown"}

    # ---- 待办聚合 ----
    pending_review = (
        await session.execute(
            select(func.count())
            .select_from(Asset)
            .where(Asset.review_status == "pending_review", Asset.deleted_at.is_(None))
        )
    ).scalar_one()
    failed_jobs = by_status.get("failed", 0)

    return {
        "components": components,
        "pipeline": {
            "queue_depths": queue_depths,
            "dead_letter": dead_letter,
            "last_24h": {
                "total": total_24h,
                "succeeded": by_status.get("succeeded", 0),
                "failed": failed_24h,
                "running": by_status.get("running", 0),
                "pending": by_status.get("pending", 0),
                "failure_rate": round(failed_24h / total_24h, 4) if total_24h else 0.0,
            },
            "stage_latency_seconds": stage_latency,
        },
        "scale": {
            **counts,
            "storage_bytes": int(used),
            "file_count": int(file_count),
        },
        "todo": {
            "pending_review_assets": pending_review,
            "failed_jobs": failed_jobs,
            "dead_letter": dead_letter,
        },
        "backup": backup_status,
        "generated_at": datetime.now(UTC).isoformat(),
    }
