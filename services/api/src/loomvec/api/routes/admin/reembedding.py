"""P4-ADM-05 模型与检索管理接口：重嵌入任务（model_version 过渡 + 原子切换）。

重嵌入流程（docs/04 §5.7）：
1. 创建任务（按空间）：登记 reembed_task，重置该空间语义单元的
   embed_model_version（使其与目标模型不一致）；
2. worker 逐资产重跑 embed/index（任务化，进度可见，检索服务不受影响——
   各单元向量自带 model_version）；
3. 全部完成后原子切换 space.embedding_model。
图谱重建/实体合并（AGE/merge_log）依赖 P3 图谱管线，接口位随 P3 补齐。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_celery, get_session, require_admin
from loomvec.core.constants import EMBEDDING_MODELS, QUEUE_PIPELINE_HIGH
from loomvec.core.db.models import ReembedTask, ReembedTaskStatus, SemanticUnit, Space
from loomvec.core.errors import NotFoundError, ValidationError

router = APIRouter(tags=["admin-models"])


class ReembedCreateRequest(BaseModel):
    space_id: uuid.UUID
    target_model: str = Field(min_length=1, max_length=128)


class ReembedCancelRequest(BaseModel):
    """取消理由：运营端 UI 强制填写并记入审计（docs/04 §六.1）；API 层宽容可选。"""

    reason: str | None = Field(default=None, max_length=500)


def _task_out(t: ReembedTask) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "space_id": str(t.space_id) if t.space_id else None,
        "target_model": t.target_model,
        "status": t.status.value,
        "total_assets": t.total_assets,
        "done_assets": t.done_assets,
        "failed_assets": t.failed_assets,
        "error": t.error,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


@router.post("/reembed", status_code=202)
async def create_reembed(
    body: ReembedCreateRequest,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> dict[str, Any]:
    if body.target_model not in EMBEDDING_MODELS:
        raise ValidationError(reason="目标模型不在白名单", allowed=list(EMBEDDING_MODELS))
    space = (
        await session.execute(
            select(Space).where(Space.id == body.space_id, Space.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if space is None:
        raise NotFoundError(resource="space", id=str(body.space_id))
    running = (
        await session.execute(
            select(func.count())
            .select_from(ReembedTask)
            .where(
                ReembedTask.space_id == space.id,
                ReembedTask.status.in_([ReembedTaskStatus.PENDING, ReembedTaskStatus.RUNNING]),
            )
        )
    ).scalar_one()
    if running:
        raise ValidationError(reason="该空间已有进行中的重嵌入任务")
    task = ReembedTask(
        space_id=space.id,
        target_model=body.target_model,
        created_by=uuid.UUID(identity.user_id),
    )
    session.add(task)
    await session.flush()
    await record_audit(
        session,
        identity=identity,
        action="admin.reembed.create",
        object_type="reembed_task",
        object_id=str(task.id),
        after={"space_id": str(space.id), "target_model": body.target_model},
    )
    await session.commit()
    celery.send_task("pipeline.reembed_space", args=[str(task.id)], queue=QUEUE_PIPELINE_HIGH)
    return _task_out(task)


@router.get("/reembed")
async def list_reembed(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(ReembedTask)
    if status:
        stmt = stmt.where(ReembedTask.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    tasks = (
        (
            await session.execute(
                stmt.order_by(ReembedTask.created_at.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_task_out(t) for t in tasks],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/reembed/{task_id}")
async def get_reembed(
    task_id: uuid.UUID,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = (
        await session.execute(select(ReembedTask).where(ReembedTask.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(resource="reembed_task", id=str(task_id))
    return _task_out(task)


@router.post("/reembed/{task_id}/cancel", status_code=202)
async def cancel_reembed(
    task_id: uuid.UUID,
    body: ReembedCancelRequest | None = None,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """取消：仅 pending 可直接取消；running 置标记由 worker 协作停止（尽力语义）。"""
    task = (
        await session.execute(select(ReembedTask).where(ReembedTask.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(resource="reembed_task", id=str(task_id))
    if task.status not in (ReembedTaskStatus.PENDING, ReembedTaskStatus.RUNNING):
        raise ValidationError(reason="任务已结束，无法取消")
    task.status = ReembedTaskStatus.FAILED
    task.error = "已由运营取消"
    await record_audit(
        session,
        identity=identity,
        action="admin.reembed.cancel",
        object_type="reembed_task",
        object_id=str(task_id),
        reason=body.reason if body else None,
    )
    await session.commit()
    return _task_out(task)


@router.get("/reindex/status")
async def reindex_status(
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """索引一致性概览：各空间未嵌入/多版本单元计数（索引维护决策依据）。"""
    rows = (
        await session.execute(
            select(
                SemanticUnit.space_id,
                func.count(),
                func.count(SemanticUnit.embed_model_version),
            ).group_by(SemanticUnit.space_id)
        )
    ).all()
    # 各空间嵌入模型版本分布
    version_rows = (
        await session.execute(
            select(
                SemanticUnit.space_id,
                SemanticUnit.embed_model_version,
                func.count(),
            ).group_by(SemanticUnit.space_id, SemanticUnit.embed_model_version)
        )
    ).all()
    version_map: dict[str, list[dict[str, Any]]] = {}
    for sid, ver, cnt in version_rows:
        version_map.setdefault(str(sid), []).append({"model_version": ver, "units": cnt})
    return {
        "spaces": [
            {
                "space_id": str(sid),
                "units": total,
                "embedded_units": embedded,
                "pending_units": total - embedded,
                "model_versions": version_map.get(str(sid), []),
            }
            for sid, total, embedded in rows
        ]
    }
