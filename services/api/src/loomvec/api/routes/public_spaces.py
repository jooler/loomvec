"""P5 用户端公共空间接口：可浏览列表与问答链接开关。

公共空间对用户的语义边界（12 文档）：
- 可见 = 运营端勾选的分组覆盖当前用户（visible_public_spaces 唯一取数）；
- 可见即可只读浏览内容（资产/检索/图谱走 require_space 虚拟 viewer）；
- 链接/断开（space_link）仅控制是否作为问答检索源
  （chat scope_space_ids 合法值，authz.linked_public_space_ids 即时校验）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_scope
from loomvec.api.identity import user_uuid
from loomvec.core.authz import is_public_space_visible, visible_public_spaces
from loomvec.core.db.models import Space, SpaceLink, SpaceType
from loomvec.core.errors import NotFoundError, PermissionDeniedError

router = APIRouter(prefix="/api/v1", tags=["public-spaces"])


class PublicSpaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    created_at: object
    linked: bool


class PublicSpaceListOut(BaseModel):
    items: list[PublicSpaceOut]


class SpaceLinkRequest(BaseModel):
    linked: bool


class SpaceLinkOut(BaseModel):
    space_id: uuid.UUID
    linked: bool


@router.get("/public-spaces", response_model=PublicSpaceListOut)
async def list_public_spaces(
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> PublicSpaceListOut:
    """当前用户经分组可见的公共空间（含链接状态；可进入只读浏览）。"""
    spaces = await visible_public_spaces(session, user_id=user_uuid(identity))
    linked_ids = set(
        (
            await session.execute(
                select(SpaceLink.space_id).where(SpaceLink.user_id == user_uuid(identity))
            )
        )
        .scalars()
        .all()
    )
    return PublicSpaceListOut(
        items=[
            PublicSpaceOut(
                id=s.id,
                slug=s.slug,
                name=s.name,
                description=s.description,
                created_at=s.created_at,
                linked=s.id in linked_ids,
            )
            for s in spaces
        ]
    )


@router.put("/public-spaces/{space_id}/link", response_model=SpaceLinkOut)
async def set_space_link(
    space_id: uuid.UUID,
    body: SpaceLinkRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> SpaceLinkOut:
    """链接/断开公共空间（幂等开关）。

    资格校验与列表同口径：空间存在且为有效公共空间 × 用户分组被勾选；
    分组被移出后再开启 → 403（既有链接行的检索有效性由 authz 即时求交保证）。
    """
    user_id = user_uuid(identity)
    space = (
        await session.execute(select(Space).where(Space.id == space_id, Space.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if space is None or space.space_type != SpaceType.PUBLIC or space.banned_at is not None:
        raise NotFoundError(resource="public_space", id=str(space_id))
    if not await is_public_space_visible(session, space=space, user_id=user_id):
        raise PermissionDeniedError(reason="该公共空间未对你所在的分组公开", space_id=str(space_id))
    row = (
        await session.execute(
            select(SpaceLink).where(SpaceLink.user_id == user_id, SpaceLink.space_id == space_id)
        )
    ).scalar_one_or_none()
    if body.linked and row is None:
        session.add(SpaceLink(user_id=user_id, space_id=space_id))
        await session.commit()
    elif not body.linked and row is not None:
        await session.delete(row)
        await session.commit()
    return SpaceLinkOut(space_id=space_id, linked=body.linked)
