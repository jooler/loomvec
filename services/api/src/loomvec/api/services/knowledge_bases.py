"""知识库树服务：知识库是带父子关系和成员权限的标签视图。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.core.authz import at_least, tag_role
from loomvec.core.db.models import Space, SpaceRole, Tag, TagMember, User
from loomvec.core.db.repos import TagRepo
from loomvec.core.errors import NotFoundError, PermissionDeniedError, ValidationError


def _user_id(identity: Identity) -> uuid.UUID:
    if identity.user_id.startswith("apikey:"):
        raise PermissionDeniedError(reason="API Key 无法管理知识库")
    return uuid.UUID(identity.user_id)


async def get_tag(session: AsyncSession, tag_id: uuid.UUID) -> Tag:
    tag = await TagRepo(session).get(tag_id)
    if tag is None:
        raise NotFoundError(resource="knowledge_base", id=str(tag_id))
    return tag


async def ensure_tag_in_space(tag: Tag, space: Space) -> None:
    if tag.tenant_id != space.tenant_id or (tag.space_id is not None and tag.space_id != space.id):
        raise PermissionDeniedError(reason="知识库不属于当前租户")


async def require_manager(
    session: AsyncSession, *, identity: Identity, space: Space, tag: Tag | None = None
) -> uuid.UUID:
    """空间 owner 或知识库 owner 可管理树节点/成员。"""
    user_id = _user_id(identity)
    from loomvec.core.authz import get_membership

    membership = await get_membership(session, space_id=space.id, user_id=user_id)
    if membership is not None and membership.role == SpaceRole.OWNER:
        return user_id
    if tag is not None:
        role = await tag_role(session, tag_id=tag.id, user_id=user_id)
        if role is not None and at_least(role, SpaceRole.OWNER):
            return user_id
    raise PermissionDeniedError(reason="无该知识库管理权限")


async def create(
    session: AsyncSession,
    *,
    identity: Identity,
    space: Space,
    name: str,
    parent_id: uuid.UUID | None = None,
) -> Tag:
    name = name.strip()
    if not name:
        raise ValidationError("知识库名称不能为空")
    parent = await get_tag(session, parent_id) if parent_id else None
    if parent is not None:
        await ensure_tag_in_space(parent, space)
        await require_manager(session, identity=identity, space=space, tag=parent)
    else:
        await require_manager(session, identity=identity, space=space)
    tag = Tag(
        tenant_id=space.tenant_id,
        space_id=space.id,
        name=name,
        parent_id=parent_id,
        created_by=_user_id(identity),
    )
    session.add(tag)
    await session.flush()
    session.add(
        TagMember(
            tenant_id=space.tenant_id,
            tag_id=tag.id,
            user_id=_user_id(identity),
            role=SpaceRole.OWNER,
        )
    )
    await session.commit()
    return tag


async def update(
    session: AsyncSession,
    *,
    identity: Identity,
    space: Space,
    tag: Tag,
    name: str | None = None,
    parent_id: uuid.UUID | None = None,
    change_parent: bool = False,
) -> Tag:
    await ensure_tag_in_space(tag, space)
    await require_manager(session, identity=identity, space=space, tag=tag)
    if name is not None:
        if not name.strip():
            raise ValidationError("知识库名称不能为空")
        tag.name = name.strip()
    if change_parent:
        if parent_id == tag.id:
            raise ValidationError("知识库不能挂到自身下面")
        if parent_id is None:
            tag.parent_id = None
        else:
            parent = await get_tag(session, parent_id)
            await ensure_tag_in_space(parent, space)
            await require_manager(session, identity=identity, space=space, tag=parent)
            descendants = set(await TagRepo(session).descendant_ids(tag.id))
            if parent.id in descendants:
                raise ValidationError("知识库不能移动到自己的子节点下面")
            tag.parent_id = parent.id
    await session.commit()
    return tag


async def delete(session: AsyncSession, *, identity: Identity, space: Space, tag: Tag) -> None:
    await ensure_tag_in_space(tag, space)
    await require_manager(session, identity=identity, space=space, tag=tag)
    descendants = await TagRepo(session).descendant_ids(tag.id)
    rows = (await session.execute(select(Tag).where(Tag.id.in_(descendants)))).scalars().all()
    for row in rows:
        row.deleted_at = datetime.now(UTC)
    await session.commit()


async def set_member(
    session: AsyncSession,
    *,
    identity: Identity,
    space: Space,
    tag: Tag,
    user: User,
    role: SpaceRole,
) -> TagMember:
    await ensure_tag_in_space(tag, space)
    await require_manager(session, identity=identity, space=space, tag=tag)
    if user.tenant_id != space.tenant_id:
        raise ValidationError("用户不属于当前租户")
    member = await TagRepo(session).set_member(tag_id=tag.id, user_id=user.id, role=role)
    await session.commit()
    return member


async def remove_member(
    session: AsyncSession,
    *,
    identity: Identity,
    space: Space,
    tag: Tag,
    user_id: uuid.UUID,
) -> None:
    await ensure_tag_in_space(tag, space)
    await require_manager(session, identity=identity, space=space, tag=tag)
    members = await TagRepo(session).members_for(tag.id)
    if sum(1 for m in members if m.role == SpaceRole.OWNER and m.user_id != user_id) == 0:
        raise ValidationError("知识库至少保留一个 owner")
    if not await TagRepo(session).remove_member(tag_id=tag.id, user_id=user_id):
        raise NotFoundError(resource="knowledge_base_member", id=str(user_id))
    await session.commit()
