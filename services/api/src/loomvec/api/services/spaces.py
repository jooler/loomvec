"""P2-API-01 空间服务：创建（白名单校验）、设置、删除级联、用量查询。

删除级联（退出标准）：PG 行（成员/用量/资产域级联）+ Milvus 空间向量 +
对象存储 raw/derived 前缀清理；空间行软删。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.services import notifications as notification_service
from loomvec.core.config import get_settings
from loomvec.core.constants import (
    CHUNK_PRESETS,
    CHUNK_STRATEGIES,
    DEFAULT_CHUNK_PRESET,
    EMBEDDING_MODELS,
    GRAPH_EXTRACTION_MODES,
)
from loomvec.core.db.models import Space, SpaceMember, SpaceRole, SpaceType, User
from loomvec.core.db.repos import AssetRepo, SpaceMemberRepo, SpaceRepo, SpaceUsageRepo
from loomvec.core.db.system_config import load_extraction_profiles
from loomvec.core.errors import ValidationError
from loomvec.core.quota import get_or_create_usage
from loomvec.core.storage import ObjectStorage
from loomvec.core.storage_keys import asset_derived_prefixes, raw_space_prefix

logger = structlog.get_logger("loomvec.api.spaces")

# validate_space_settings 校验的字段（与 Space 列同名）
_VALIDATED_FIELDS = (
    "embedding_model",
    "chunk_preset",
    "chunk_strategy",
    "graph_extraction",
    "extraction_profile",
)


async def validate_space_settings(
    session: AsyncSession,
    *,
    embedding_model: str | None = None,
    chunk_preset: str | None = None,
    chunk_strategy: str | None = None,
    graph_extraction: str | None = None,
    extraction_profile: str | None = None,
) -> None:
    """创建/设置空间时的白名单校验（P4 动态配置：白名单可运营调整，即时生效）。

    extraction_profile 须是可用的抽取领域配置（内置 + admin 自定义）。
    """
    from loomvec.api.services.admin_settings import effective_value

    whitelist = await effective_value(session, "models.embedding_whitelist") or list(
        EMBEDDING_MODELS
    )
    presets = await effective_value(session, "models.chunk_presets") or list(CHUNK_PRESETS)

    def _check(value: str | None, allowed: list[str], label: str) -> None:
        if value is not None and value not in allowed:
            raise ValidationError(label, allowed=allowed, got=value)

    _check(embedding_model, list(whitelist), "不支持的嵌入模型")
    _check(chunk_preset, list(presets), "不支持的分片预设")
    _check(chunk_strategy, list(CHUNK_STRATEGIES), "不支持的分片策略")
    _check(graph_extraction, list(GRAPH_EXTRACTION_MODES), "不支持的图谱抽取开关")
    if extraction_profile is not None:
        _check(
            extraction_profile,
            list(await load_extraction_profiles(session)),
            "不支持的抽取领域配置",
        )


async def usable_profile_keys(session: AsyncSession) -> frozenset[str]:
    """当前可用的抽取领域配置 key（内置 + 有效的自定义配置）。"""
    return frozenset(await load_extraction_profiles(session))


async def validate_space_settings_change(
    session: AsyncSession, space: Space, fields: dict[str, Any]
) -> None:
    """已有空间改设置时的白名单校验：只校验与当前值不同的字段。

    白名单是运营可调的（模型白名单、自定义抽取配置可被删除），空间上已存的旧值可能
    不再在白名单内；前端保存设置会把整张表单原样提交，值未变化的字段不应因此被拒绝
    （否则用户只改名称也保存不了）。想把字段改成另一个值时才要求新值可用。
    """
    await validate_space_settings(
        session,
        **{
            name: fields[name]
            for name in _VALIDATED_FIELDS
            if name in fields and fields[name] != getattr(space, name)
        },
    )


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
    chunk_strategy: str | None = None,
    graph_extraction: str | None = None,
    extraction_profile: str | None = None,
) -> Space:
    """创建空间：创建者即 owner；用量行与成员行同事务建立。

    chunk_strategy/graph_extraction/extraction_profile 留空 = 跟随全局配置（不像 chunk_preset
    那样落默认值），这是三者的常态——多数空间不需要覆盖。
    """
    await validate_space_settings(
        session,
        embedding_model=embedding_model,
        chunk_preset=chunk_preset,
        chunk_strategy=chunk_strategy,
        graph_extraction=graph_extraction,
        extraction_profile=extraction_profile,
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
        chunk_strategy=chunk_strategy,
        graph_extraction=graph_extraction,
        extraction_profile=extraction_profile,
    )
    session.add(space)
    await session.flush()
    session.add(SpaceMember(space_id=space.id, user_id=owner_id, role=SpaceRole.OWNER))
    await get_or_create_usage(session, space)
    await session.commit()
    return space


def extraction_profile_valid(space: Space, usable_keys: Collection[str]) -> bool:
    """空间选择的抽取领域配置是否仍可用（留空 = 跟随全局，恒有效）。

    False = 引用的自定义配置已被删除/失效：管线会按 resolve_extraction_profile 回落全局配置，
    前端据此提示空间 owner 重新选择。
    """
    return space.extraction_profile is None or space.extraction_profile in usable_keys


def space_out(
    space: Space,
    *,
    usable_keys: Collection[str],
    my_role: str | None = None,
    member_count: int | None = None,
) -> dict:
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
        "chunk_strategy": space.chunk_strategy,
        "graph_extraction": space.graph_extraction,
        "extraction_profile": space.extraction_profile,
        "extraction_profile_valid": extraction_profile_valid(space, usable_keys),
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
    """owner 设置：名称/描述/审核开关/模型与预设/空间级配额。

    白名单只校验与当前值不同的字段（见 validate_space_settings_change）。
    """
    await validate_space_settings_change(session, space, fields)
    # chunk_strategy/graph_extraction/extraction_profile 的 None 是合法终态（留空=跟随全局配置），
    # 必须能被显式 PATCH 为 null 来清除空间覆盖，不能像其余字段那样把 None 当“未提供”跳过
    nullable_fields = (
        "review_required",
        "chunk_strategy",
        "graph_extraction",
        "extraction_profile",
    )
    for key, value in fields.items():
        if value is not None or key in nullable_fields:
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
