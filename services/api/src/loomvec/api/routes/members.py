"""P2-API-02 成员接口：邀请 / 角色变更 / 移除（owner 操作）与成员列表。

被移除即失权：space_member 行删除后所有经 authz 的闸门立即拒绝该用户。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.deps import get_session, require_space
from loomvec.api.services import spaces as space_service
from loomvec.core.authz import SpaceAccess
from loomvec.core.db.models import SpaceRole
from loomvec.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1", tags=["members"])


class MemberAddRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    role: SpaceRole = SpaceRole.VIEWER


class MemberRoleRequest(BaseModel):
    role: SpaceRole


class MemberOut(BaseModel):
    user_id: uuid.UUID
    username: str | None
    display_name: str | None
    role: str


class MemberListOut(BaseModel):
    items: list[MemberOut]


@router.get("/spaces/{space_id}/members", response_model=MemberListOut)
async def list_members(
    access: SpaceAccess = Depends(require_space()),
    session: AsyncSession = Depends(get_session),
) -> MemberListOut:
    from sqlalchemy import select

    from loomvec.core.db.models import User
    from loomvec.core.db.repos import SpaceMemberRepo

    members = await SpaceMemberRepo(session).list_for_space(access.space.id)
    users: dict[uuid.UUID, User] = {}
    if members:
        rows = (
            (await session.execute(select(User).where(User.id.in_([m.user_id for m in members]))))
            .scalars()
            .all()
        )
        users = {u.id: u for u in rows}
    items = []
    for m in members:
        u = users.get(m.user_id)
        items.append(
            MemberOut(
                user_id=m.user_id,
                username=u.username if u else None,
                display_name=u.display_name if u else None,
                role=m.role.value,
            )
        )
    return MemberListOut(items=items)


@router.post("/spaces/{space_id}/members", response_model=MemberOut, status_code=201)
async def add_member(
    body: MemberAddRequest,
    access: SpaceAccess = Depends(require_space(SpaceRole.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    """邀请成员（owner）：按用户名邀请，角色 viewer/editor。"""
    user = await space_service.user_by_username(session, body.username)
    if user is None:
        raise NotFoundError(resource="user", id=body.username)
    member = await space_service.add_member(
        session,
        space=access.space,
        user=user,
        role=body.role,
        invited_by=access.member.user_id if access.member else None,
    )
    return MemberOut(
        user_id=member.user_id,
        username=user.username,
        display_name=user.display_name,
        role=member.role.value,
    )


@router.patch("/spaces/{space_id}/members/{user_id}", response_model=MemberOut)
async def change_member_role(
    user_id: uuid.UUID,
    body: MemberRoleRequest,
    access: SpaceAccess = Depends(require_space(SpaceRole.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> MemberOut:
    """角色变更（owner）；最后一个 owner 受保护。"""
    from loomvec.core.db.repos import SpaceMemberRepo

    member = await SpaceMemberRepo(session).get(access.space.id, user_id)
    if member is None:
        raise NotFoundError(resource="space_member", id=str(user_id))
    member = await space_service.change_member_role(
        session, space=access.space, member=member, new_role=body.role
    )
    return MemberOut(
        user_id=member.user_id,
        username=None,
        display_name=None,
        role=member.role.value,
    )


@router.delete("/spaces/{space_id}/members/{user_id}", status_code=204)
async def remove_member(
    user_id: uuid.UUID,
    access: SpaceAccess = Depends(require_space(SpaceRole.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> None:
    """移除成员（owner）即失权；owner 不可被移除。"""
    from loomvec.core.db.repos import SpaceMemberRepo

    member = await SpaceMemberRepo(session).get(access.space.id, user_id)
    if member is None:
        raise NotFoundError(resource="space_member", id=str(user_id))
    await space_service.remove_member(session, space=access.space, member=member)
