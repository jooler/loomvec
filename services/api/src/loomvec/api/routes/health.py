"""健康检查：`/healthz`（存活）+ `/readyz`（就绪，含依赖探测）。

探测逻辑单源于 api/probes.py（与 /admin/system/status、/metrics gauge 共用）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from loomvec.api.probes import probe_components

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针：进程活着即 200，不探测依赖。"""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    """就绪探针：PG 必须可用；Redis/存储/Milvus/worker 降级仅告警。"""
    checks = await probe_components(request.app.state)
    postgres_ok = checks.get("postgres", {}).get("ok", False)
    response.status_code = 200 if postgres_ok else 503
    return {"status": "ok" if postgres_ok else "degraded", "checks": checks}
