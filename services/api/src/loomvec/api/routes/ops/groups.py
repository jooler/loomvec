"""P5-OPS 用户分组接口（运营端）：分组 CRUD 与成员管理。

分组是公共空间可见性的授权单元（space_group_visibility），与空间成员角色
（space_member）完全解耦。删除分组级联删除其可见性勾选（FK CASCADE），
用户既有链接行保留但检索即时失效（authz 即时求交，不做后台清理）。
分组管理仅存在于运营端：管理端（系统运维）与用户端均不提供入口。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_ops
from loomvec.api.services import spaces as space_service
from loomvec.core.db.models import SpaceGroupVisibility, User, UserGroup, UserGroupMember
from loomvec.core.errors import ConflictError, NotFoundError

router = APIRouter(tags=["ops-groups"])


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class GroupUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)


class GroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    member_count: int
    space_count: int
    created_at: datetime


class GroupListOut(BaseModel):
    items: list[GroupOut]


class GroupMemberOut(BaseModel):
    user_id: uuid.UUID
    username: str | None
    display_name: str | None


class GroupMemberListOut(BaseModel):
    items: list[GroupMemberOut]


class GroupMemberAddRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)


async def get_group(group_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> UserGroup:
    group = (
        await session.execute(
            select(UserGroup).where(UserGroup.id == group_id, UserGroup.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if group is None:
        raise NotFoundError(resource="user_group", id=str(group_id))
    return group


async def _member_counts(session: AsyncSession, group_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not group_ids:
        return {}
    rows = (
        await session.execute(
            select(UserGroupMember.group_id, func.count())
            .where(UserGroupMember.group_id.in_(group_ids))
            .group_by(UserGroupMember.group_id)
        )
    ).all()
    return {gid: int(n) for gid, n in rows}


async def _space_counts(session: AsyncSession, group_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not group_ids:
        return {}
    rows = (
        await session.execute(
            select(SpaceGroupVisibility.group_id, func.count())
            .where(SpaceGroupVisibility.group_id.in_(group_ids))
            .group_by(SpaceGroupVisibility.group_id)
        )
    ).all()
    return {gid: int(n) for gid, n in rows}


def _group_out(g: UserGroup, *, member_count: int, space_count: int) -> GroupOut:
    return GroupOut(
        id=g.id,
        name=g.name,
        description=g.description,
        member_count=member_count,
        space_count=space_count,
        created_at=g.created_at,
    )


@router.get("/groups", response_model=GroupListOut)
async def list_groups(
    identity: Identity = Depends(require_ops("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> GroupListOut:
    groups = list(
        (
            await session.execute(
                select(UserGroup)
                .where(UserGroup.deleted_at.is_(None))
                .order_by(UserGroup.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    members = await _member_counts(session, [g.id for g in groups])
    spaces = await _space_counts(session, [g.id for g in groups])
    return GroupListOut(
        items=[
            _group_out(g, member_count=members.get(g.id, 0), space_count=spaces.get(g.id, 0))
            for g in groups
        ]
    )


@router.post("/groups", response_model=GroupOut, status_code=201)
async def create_group(
    body: GroupCreateRequest,
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    tenant_id = uuid.UUID(identity.tenant_id) if identity.tenant_id else None
    dup = (
        await session.execute(
            select(UserGroup).where(
                UserGroup.tenant_id == tenant_id,
                UserGroup.name == body.name,
                UserGroup.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise ConflictError("同名分组已存在", name=body.name)
    group = UserGroup(tenant_id=tenant_id, name=body.name, description=body.description)
    session.add(group)
    await session.commit()
    return _group_out(group, member_count=0, space_count=0)


@router.patch("/groups/{group_id}", response_model=GroupOut)
async def update_group(
    body: GroupUpdateRequest,
    group: UserGroup = Depends(get_group),
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> GroupOut:
    fields = body.model_dump(exclude_unset=True)
    new_name = fields.get("name")
    if new_name is not None and new_name != group.name:
        tenant_id = uuid.UUID(identity.tenant_id) if identity.tenant_id else None
        dup = (
            await session.execute(
                select(UserGroup).where(
                    UserGroup.tenant_id == tenant_id,
                    UserGroup.name == new_name,
                    UserGroup.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise ConflictError("同名分组已存在", name=new_name)
    for key, value in fields.items():
        setattr(group, key, value)
    await session.commit()
    members = await _member_counts(session, [group.id])
    spaces = await _space_counts(session, [group.id])
    return _group_out(
        group, member_count=members.get(group.id, 0), space_count=spaces.get(group.id, 0)
    )


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group: UserGroup = Depends(get_group),
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除分组（软删）：显式清除可见性勾选行（软删不触发 FK 级联）；
    用户链接行保留但即时失效（授权判定过滤已删分组）。"""
    from datetime import UTC

    from sqlalchemy import delete as sa_delete

    await record_audit(
        session,
        identity=identity,
        action="ops.group_deleted",
        object_type="user_group",
        object_id=str(group.id),
        before={"name": group.name},
    )
    group.deleted_at = datetime.now(UTC)
    # 软删是 UPDATE，SpaceGroupVisibility.group_id 的 ondelete=CASCADE 不会触发，
    # 勾选行显式删除（可见性授予关系不复存在，重建同名分组也不应继承旧勾选）
    await session.execute(
        sa_delete(SpaceGroupVisibility).where(SpaceGroupVisibility.group_id == group.id)
    )
    await session.commit()


@router.get("/groups/{group_id}/members", response_model=GroupMemberListOut)
async def list_group_members(
    group: UserGroup = Depends(get_group),
    identity: Identity = Depends(require_ops("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> GroupMemberListOut:
    rows = (
        (
            await session.execute(
                select(User)
                .join(UserGroupMember, UserGroupMember.user_id == User.id)
                .where(UserGroupMember.group_id == group.id, User.deleted_at.is_(None))
                .order_by(User.username.asc())
            )
        )
        .scalars()
        .all()
    )
    return GroupMemberListOut(
        items=[
            GroupMemberOut(user_id=u.id, username=u.username, display_name=u.display_name)
            for u in rows
        ]
    )


@router.post("/groups/{group_id}/members", response_model=GroupMemberOut, status_code=201)
async def add_group_member(
    body: GroupMemberAddRequest,
    group: UserGroup = Depends(get_group),
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> GroupMemberOut:
    """按用户名加入分组（与空间邀请同交互形态）；重复加入 409。"""
    user = await space_service.user_by_username(session, body.username)
    if user is None:
        raise NotFoundError(resource="user", id=body.username)
    dup = (
        await session.execute(
            select(UserGroupMember).where(
                UserGroupMember.group_id == group.id, UserGroupMember.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise ConflictError("用户已在分组中", user_id=str(user.id))
    session.add(UserGroupMember(group_id=group.id, user_id=user.id))
    await session.commit()
    return GroupMemberOut(user_id=user.id, username=user.username, display_name=user.display_name)


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
async def remove_group_member(
    user_id: uuid.UUID,
    group: UserGroup = Depends(get_group),
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = (
        await session.execute(
            select(UserGroupMember).where(
                UserGroupMember.group_id == group.id, UserGroupMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(resource="user_group_member", id=str(user_id))
    await session.delete(row)
    await session.commit()
