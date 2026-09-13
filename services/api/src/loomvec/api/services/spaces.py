"""P2-API-01 空间服务：创建（白名单校验）、设置、删除级联、用量查询。

删除级联（退出标准）：PG 行（成员/用量/资产域级联）+ Milvus 空间向量 +
对象存储 raw/derived 前缀清理；空间行软删。
"""

from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.services import notifications as notification_service
from loomvec.core.config import get_settings
from loomvec.core.constants import CHUNK_PRESETS, DEFAULT_CHUNK_PRESET, EMBEDDING_MODELS
from loomvec.core.db.models import Space, SpaceMember, SpaceRole, SpaceType, User
from loomvec.core.db.repos import AssetRepo, SpaceMemberRepo, SpaceRepo, SpaceUsageRepo
from loomvec.core.errors import ValidationError
from loomvec.core.quota import get_or_create_usage
from loomvec.core.storage import ObjectStorage
from loomvec.core.storage_keys import asset_derived_prefixes, raw_space_prefix

logger = structlog.get_logger("loomvec.api.spaces")


async def validate_space_settings(
    session: AsyncSession,
    *,
    embedding_model: str | None = None,
    chunk_preset: str | None = None,
) -> None:
    """创建/设置空间时的白名单校验（P4 动态配置：白名单可运营调整，即时生效）。"""
    from loomvec.api.services.admin_settings import effective_value

    whitelist = await effective_value(session, "models.embedding_whitelist") or list(
        EMBEDDING_MODELS
    )
    presets = await effective_value(session, "models.chunk_presets") or list(CHUNK_PRESETS)
    if embedding_model is not None and embedding_model not in whitelist:
        raise ValidationError("不支持的嵌入模型", allowed=list(whitelist), got=embedding_model)
    if chunk_preset is not None and chunk_preset not in presets:
        raise ValidationError("不支持的分片预设", allowed=list(presets), got=chunk_preset)


async def _unique_slug(session: AsyncSession, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "space"
    slug = base
    repo = SpaceRepo(session)
    n = 1
    while await repo.slug_exists(slug):
        n += 1
        slug = f"{base}-{n}"[:64]
    return slug


async def create_space(
    session: AsyncSession,
    *,
    identity: Identity,
    name: str,
    description: str | None = None,
    space_type: SpaceType = SpaceType.SHARED,
    review_required: bool = False,
    embedding_model: str | None = None,
    chunk_preset: str | None = None,
) -> Space:
    """创建空间：创建者即 owner；用量行与成员行同事务建立。"""
    await validate_space_settings(
        session, embedding_model=embedding_model, chunk_preset=chunk_preset
    )
    if not identity.user_id or identity.user_id.startswith("apikey:"):
        raise ValidationError("API Key 无法创建空间（无用户上下文）")
    owner_id = uuid.UUID(identity.user_id)
    tenant_id = uuid.UUID(identity.tenant_id) if identity.tenant_id else None

    slug = await _unique_slug(session, name)
    space = Space(
        tenant_id=tenant_id,
        slug=slug,
        name=name,
        description=description,
        space_type=space_type,
        owner_id=owner_id,
        review_required=review_required,
        embedding_model=embedding_model,
        chunk_preset=chunk_preset or DEFAULT_CHUNK_PRESET,
    )
    session.add(space)
    await session.flush()
    session.add(SpaceMember(space_id=space.id, user_id=owner_id, role=SpaceRole.OWNER))
    await get_or_create_usage(session, space)
    await session.commit()
    return space


def space_out(space: Space, *, my_role: str | None = None, member_count: int | None = None) -> dict:
    data = {
        "id": space.id,
        "slug": space.slug,
        "name": space.name,
        "description": space.description,
        "space_type": space.space_type.value,
        "owner_id": space.owner_id,
        "review_required": space.review_required,
        "embedding_model": space.embedding_model,
        "chunk_preset": space.chunk_preset,
        "quota_storage_bytes": space.quota_storage_bytes,
        "quota_file_count": space.quota_file_count,
        "created_at": space.created_at,
    }
    if my_role is not None:
        data["my_role"] = my_role
    if member_count is not None:
        data["member_count"] = member_count
    return data


async def update_space_settings(session: AsyncSession, *, space: Space, **fields) -> Space:
    """owner 设置：名称/描述/审核开关/模型与预设/空间级配额。"""
    await validate_space_settings(
        session,
        embedding_model=fields.get("embedding_model"),
        chunk_preset=fields.get("chunk_preset"),
    )
    for key, value in fields.items():
        if value is not None or key in ("review_required",):
            setattr(space, key, value)
    await session.commit()
    return space


async def delete_space_cascade(
    session: AsyncSession, *, space: Space, milvus, storage: ObjectStorage
) -> None:
    """删除空间：软删空间行 → 成员/用量行删除 → 向量与对象清理。"""
    from datetime import UTC, datetime

    space.deleted_at = datetime.now(UTC)
    await session.commit()

    # Milvus：清该空间全部向量
    await milvus.async_delete_space_units(space.id)

    # 对象存储：raw/{space_id}/ 与派生前缀逐资产清理（key 约定见 core.storage_keys）
    settings = get_settings()
    assets = await AssetRepo(session).list_for_space_cleanup(space.id)
    try:
        await storage.delete_prefix(settings.storage.bucket_raw, raw_space_prefix(space.id))
    except Exception as e:  # 清理失败不阻塞空间删除（记录待运维补偿）
        logger.warning("space_storage_cleanup_failed", space_id=str(space.id), error=str(e))
    for asset in assets:
        for prefix in asset_derived_prefixes(asset.id):
            try:
                await storage.delete_prefix(settings.storage.bucket_derived, prefix)
            except Exception as e:
                logger.warning("space_storage_cleanup_failed", space_id=str(space.id), error=str(e))
                break

    # 成员与用量行物理删除（软删空间不再需要）
    members = await SpaceMemberRepo(session).list_for_space(space.id)
    for m in members:
        await session.delete(m)
    usage = await SpaceUsageRepo(session).get_for_space(space.id)
    if usage is not None:
        await session.delete(usage)
    await session.commit()


async def space_usage_out(session: AsyncSession, space: Space) -> dict:
    usage = await get_or_create_usage(session, space)
    return {
        "space_id": space.id,
        "storage_bytes": usage.storage_bytes,
        "file_count": usage.file_count,
        "quota_storage_bytes": space.quota_storage_bytes,
        "quota_file_count": space.quota_file_count,
    }


async def member_count(session: AsyncSession, space_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from loomvec.core.db.models import SpaceMember

    stmt = select(func.count()).select_from(SpaceMember)
    stmt = stmt.where(SpaceMember.space_id == space_id)
    return int((await session.execute(stmt)).scalar_one())


async def user_display(session: AsyncSession, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    from loomvec.core.db.repos import UserRepo

    user = await UserRepo(session).get(user_id)
    return user.display_name or user.username if user else None


async def user_by_username(session: AsyncSession, username: str) -> User | None:
    from loomvec.core.db.repos import UserRepo

    return await UserRepo(session).get_by_username(username)


def member_out(
    m: SpaceMember, *, username: str | None = None, display_name: str | None = None
) -> dict:
    return {
        "user_id": m.user_id,
        "username": username,
        "display_name": display_name,
        "role": m.role.value,
        "invited_by": m.invited_by,
        "created_at": m.created_at,
    }


def _tenant_uuid(space: Space) -> uuid.UUID | None:
    return space.tenant_id


async def add_member(
    session: AsyncSession,
    *,
    space: Space,
    user: User,
    role: SpaceRole,
    invited_by: uuid.UUID | None,
) -> SpaceMember:
    """邀请成员（owner 操作）；重复邀请 409。"""
    from loomvec.core.errors import ConflictError

    repo = SpaceMemberRepo(session)
    existing = await repo.get(space.id, user.id)
    if existing is not None:
        raise ConflictError("用户已是空间成员", user_id=str(user.id))
    member = await repo.create(
        space_id=space.id,
        user_id=user.id,
        role=role,
        invited_by=invited_by,
        tenant_id=space.tenant_id,
    )
    await notification_service.notify(
        session,
        user_id=user.id,
        tenant_id=space.tenant_id,
        type_="member.added",
        title=f"你已被加入空间「{space.name}」（{role.value}）",
        payload={"space_id": str(space.id), "role": role.value},
    )
    await session.commit()
    return member


async def change_member_role(
    session: AsyncSession, *, space: Space, member: SpaceMember, new_role: SpaceRole
) -> SpaceMember:
    """角色变更（owner 操作）；保护最后一个 owner。"""
    from loomvec.core.errors import ValidationError

    if member.role == SpaceRole.OWNER and new_role != SpaceRole.OWNER:
        owners = [
            m
            for m in await SpaceMemberRepo(session).list_for_space(space.id)
            if m.role == SpaceRole.OWNER
        ]
        if len(owners) <= 1:
            raise ValidationError("不能移除最后一个 owner（请先转移所有权）")
    member.role = new_role
    await session.flush()
    await notification_service.notify(
        session,
        user_id=member.user_id,
        tenant_id=space.tenant_id,
        type_="member.role_changed",
        title=f"你在空间「{space.name}」的角色变更为 {new_role.value}",
        payload={"space_id": str(space.id), "role": new_role.value},
    )
    await session.commit()
    return member


async def remove_member(session: AsyncSession, *, space: Space, member: SpaceMember) -> None:
    """移除成员（owner 操作）即失权；不能移除 owner（含最后一个 owner 保护）。"""
    from loomvec.core.errors import ValidationError

    if member.role == SpaceRole.OWNER:
        raise ValidationError("不能移除 owner（请先变更角色/转移所有权）")
    await session.delete(member)
    await session.flush()
    await notification_service.notify(
        session,
        user_id=member.user_id,
        tenant_id=space.tenant_id,
        type_="member.removed",
        title=f"你已被移出空间「{space.name}」",
        payload={"space_id": str(space.id)},
    )
    await session.commit()
