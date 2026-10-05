"""知识库树服务：知识库是带父子关系和成员权限的标签视图。

人工路径（create/update/delete/成员/confirm/merge）一律经 require_manager 校验。
管线自动归类新建 pending 节点的系统路径在 core.knowledge_base_auto（worker 调用），
pending 节点由这里的 confirm / merge 人工处理。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.core.authz import at_least, tag_role
from loomvec.core.db.models import AssetTag, Space, SpaceRole, Tag, TagMember, TagStatus, User
from loomvec.core.db.repos import TagRepo
from loomvec.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)


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


async def confirm(session: AsyncSession, *, identity: Identity, space: Space, tag: Tag) -> Tag:
    """确认 pending 节点为正式知识库；已是 active 时原样返回。"""
    await ensure_tag_in_space(tag, space)
    await require_manager(session, identity=identity, space=space, tag=tag)
    if tag.status != TagStatus.ACTIVE:
        tag.status = TagStatus.ACTIVE.value
        await session.commit()
    return tag


async def merge(
    session: AsyncSession, *, identity: Identity, space: Space, tag: Tag, target: Tag
) -> Tag:
    """把 pending 节点并入本空间另一节点：文献关联与直接子节点改挂目标，源节点记
    merged_into 后软删（之后自动归类沿此重定向，不再重建同名节点）。

    需同时具备源与目标两个节点的管理权限（并入会扩大目标节点成员可见的文献范围）；
    源节点的成员授权不迁移。源/目标按 id 顺序加 FOR UPDATE 行锁后复核状态：与自动归类
    写关联前的 FOR KEY SHARE 复核互斥，并发写入的关联要么先落库被一并迁移，要么在合并
    提交后沿 merged_into 改挂目标。改挂直接子节点只是普通 UPDATE parent_id（隐式
    FOR NO KEY UPDATE），不与自动归类的 FOR KEY SHARE 冲突，不会在"自动归类命中子
    节点、merge 命中父节点"时反过来构成死锁。
    """
    await ensure_tag_in_space(tag, space)
    await ensure_tag_in_space(target, space)
    if target.space_id != space.id:
        raise ValidationError("合并目标须是本空间的知识库（不支持早期租户级标签）")
    await require_manager(session, identity=identity, space=space, tag=tag)
    await require_manager(session, identity=identity, space=space, tag=target)
    locked = {
        row.id: row
        for row in (
            await session.execute(
                select(Tag)
                .where(Tag.id.in_([tag.id, target.id]))
                .order_by(Tag.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    }
    for node in (tag, target):
        if node.id not in locked or locked[node.id].deleted_at is not None:
            raise NotFoundError(resource="knowledge_base", id=str(node.id))
    if tag.status != TagStatus.PENDING:
        raise ConflictError("只能合并待确认（pending）的知识库", status=tag.status)
    if target.id in set(await TagRepo(session).descendant_ids(tag.id)):
        raise ValidationError("不能合并到自身或自己的子节点")
    target_asset_ids = set(
        (await session.execute(select(AssetTag.asset_id).where(AssetTag.tag_id == target.id)))
        .scalars()
        .all()
    )
    links = (
        (await session.execute(select(AssetTag).where(AssetTag.tag_id == tag.id))).scalars().all()
    )
    for link in links:
        if link.asset_id in target_asset_ids:
            await session.delete(link)  # 文献已挂目标节点，去掉重复关联
        else:
            link.tag_id = target.id
    children = (
        (
            await session.execute(
                select(Tag).where(Tag.parent_id == tag.id, Tag.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    for child in children:
        child.parent_id = target.id
    tag.merged_into = target.id
    tag.deleted_at = datetime.now(UTC)
    await session.commit()
    return target


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
