"""P2-CORE-03 配额服务：租户 → 空间两级切分 + 上传闸门 + 用量计量。

- 计量：space_usage 行随资产登记/删除在事务内更新（登记先行预检，提交即计量）；
- 闸门：租户级（tenant.quota_*）与空间级（space.quota_*）双重校验，
  0 = 不限；存量资产检索不受配额影响（配额只在写入路径生效）；
- 纯判定函数 `decide_upload` 供单测覆盖超限矩阵。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import Space, SpaceUsage, Tenant
from loomvec.core.errors import PermissionDeniedError

# 违规码（API 契约 details.reason 值；用户端据此提示）
QUOTA_TENANT_STORAGE = "tenant_storage_exceeded"
QUOTA_TENANT_FILES = "tenant_file_count_exceeded"
QUOTA_SPACE_STORAGE = "space_storage_exceeded"
QUOTA_SPACE_FILES = "space_file_count_exceeded"


@dataclass(frozen=True)
class QuotaSnapshot:
    """两级配额快照（0 = 不限）。"""

    tenant_storage_bytes: int
    tenant_file_count: int
    tenant_used_bytes: int
    tenant_used_files: int
    space_storage_bytes: int
    space_file_count: int
    space_used_bytes: int
    space_used_files: int


def decide_upload(snap: QuotaSnapshot, incoming_bytes: int) -> str | None:
    """返回违规码或 None（允许）。文件数按 +1 计。"""
    if (
        snap.tenant_storage_bytes
        and snap.tenant_used_bytes + incoming_bytes > snap.tenant_storage_bytes
    ):
        return QUOTA_TENANT_STORAGE
    if snap.tenant_file_count and snap.tenant_used_files + 1 > snap.tenant_file_count:
        return QUOTA_TENANT_FILES
    if (
        snap.space_storage_bytes
        and snap.space_used_bytes + incoming_bytes > snap.space_storage_bytes
    ):
        return QUOTA_SPACE_STORAGE
    if snap.space_file_count and snap.space_used_files + 1 > snap.space_file_count:
        return QUOTA_SPACE_FILES
    return None


async def get_or_create_usage(session: AsyncSession, space: Space) -> SpaceUsage:
    usage = (
        await session.execute(select(SpaceUsage).where(SpaceUsage.space_id == space.id))
    ).scalar_one_or_none()
    if usage is None:
        usage = SpaceUsage(
            space_id=space.id, tenant_id=space.tenant_id, storage_bytes=0, file_count=0
        )
        session.add(usage)
        await session.flush()
    return usage


async def snapshot(
    session: AsyncSession, space: Space, usage: SpaceUsage | None = None
) -> QuotaSnapshot:
    """采集两级配额快照；租户用量 = 该租户全部空间 space_usage 求和。"""
    if usage is None:
        usage = await get_or_create_usage(session, space)

    tenant_used_bytes = tenant_used_files = 0
    tenant_id = space.tenant_id
    tenant: Tenant | None = None
    if tenant_id is not None:
        rows = (
            await session.execute(
                select(SpaceUsage.storage_bytes, SpaceUsage.file_count).where(
                    SpaceUsage.tenant_id == tenant_id
                )
            )
        ).all()
        tenant_used_bytes = sum(r[0] for r in rows)
        tenant_used_files = sum(r[1] for r in rows)
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        ).scalar_one_or_none()

    return QuotaSnapshot(
        tenant_storage_bytes=tenant.quota_storage_bytes if tenant else 0,
        tenant_file_count=tenant.quota_file_count if tenant else 0,
        tenant_used_bytes=tenant_used_bytes,
        tenant_used_files=tenant_used_files,
        space_storage_bytes=space.quota_storage_bytes,
        space_file_count=space.quota_file_count,
        space_used_bytes=usage.storage_bytes,
        space_used_files=usage.file_count,
    )


async def check_upload_quota(
    session: AsyncSession, *, space: Space, incoming_bytes: int, usage: SpaceUsage | None = None
) -> QuotaSnapshot:
    """上传闸门：超限抛 PermissionDeniedError（details.reason = 违规码）。"""
    snap = await snapshot(session, space, usage)
    reason = decide_upload(snap, incoming_bytes)
    if reason is not None:
        raise PermissionDeniedError(reason="配额不足", quota_reason=reason)
    return snap


async def apply_asset_added(
    session: AsyncSession, *, space_id: uuid.UUID, tenant_id: uuid.UUID | None, size_bytes: int
) -> None:
    """登记成功后计量 +1 文件 / +size 字节。"""
    usage = (
        await session.execute(select(SpaceUsage).where(SpaceUsage.space_id == space_id))
    ).scalar_one_or_none()
    if usage is None:
        usage = SpaceUsage(space_id=space_id, tenant_id=tenant_id, storage_bytes=0, file_count=0)
        session.add(usage)
    usage.storage_bytes += size_bytes
    usage.file_count += 1
    await session.flush()


async def apply_asset_removed(
    session: AsyncSession, *, space_id: uuid.UUID, size_bytes: int
) -> None:
    """删除资产后扣减用量（不产生负数；行保留作为计量基线）。"""
    usage = (
        await session.execute(select(SpaceUsage).where(SpaceUsage.space_id == space_id))
    ).scalar_one_or_none()
    if usage is None:
        return
    usage.storage_bytes = max(0, usage.storage_bytes - size_bytes)
    usage.file_count = max(0, usage.file_count - 1)
    await session.flush()
