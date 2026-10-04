"""层级知识库 API：知识库是带权限的树形标签，文献与节点多对多关联。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_scope, require_space
from loomvec.api.services import knowledge_bases as knowledge_base_service
from loomvec.api.services import spaces as space_service
from loomvec.core.authz import SpaceAccess, at_least, tag_role, visible_tag_ids
from loomvec.core.db.models import SpaceRole, User
from loomvec.core.db.repos import TagRepo

router = APIRouter(prefix="/api/v1", tags=["knowledge-bases"])


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None


class KnowledgeBaseUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    role: str | None = None


class KnowledgeBaseListOut(BaseModel):
    items: list[KnowledgeBaseOut]


class KnowledgeBaseMemberRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    role: SpaceRole = SpaceRole.VIEWER


class KnowledgeBaseMemberOut(BaseModel):
    user_id: uuid.UUID
    username: str | None
    display_name: str | None
    role: str


class KnowledgeBaseMemberListOut(BaseModel):
    items: list[KnowledgeBaseMemberOut]


async def _nodes_for_access(session: AsyncSession, access: SpaceAccess) -> list[KnowledgeBaseOut]:
    rows = await TagRepo(session).list_for_tenant(access.space.tenant_id, space_id=access.space.id)
    if at_least(access.role, SpaceRole.EDITOR):
        allowed = {tag.id for tag in rows}
    elif access.member is None:
        allowed = set()
    else:
        allowed = await visible_tag_ids(session, user_id=access.member.user_id)
        by_id = {tag.id: tag for tag in rows}
        for tag_id in list(allowed):
            current = by_id.get(tag_id)
            while current is not None and current.parent_id is not None:
                allowed.add(current.parent_id)
                current = by_id.get(current.parent_id)

    user_id = access.member.user_id if access.member is not None else None
    items: list[KnowledgeBaseOut] = []
    for tag in rows:
        if tag.id not in allowed:
            continue
        role = await tag_role(session, tag_id=tag.id, user_id=user_id) if user_id else None
        items.append(
            KnowledgeBaseOut(
                id=tag.id,
                name=tag.name,
                parent_id=tag.parent_id,
                role=role.value if role else None,
            )
        )
    return items


@router.get("/spaces/{space_id}/knowledge-bases", response_model=KnowledgeBaseListOut)
async def list_knowledge_bases(
    access: SpaceAccess = Depends(require_space()),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBaseListOut:
    return KnowledgeBaseListOut(items=await _nodes_for_access(session, access))


@router.post("/spaces/{space_id}/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseCreateRequest,
    access: SpaceAccess = Depends(require_space(SpaceRole.EDITOR)),
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBaseOut:
    tag = await knowledge_base_service.create(
        session, identity=identity, space=access.space, name=body.name, parent_id=body.parent_id
    )
    return KnowledgeBaseOut(
        id=tag.id, name=tag.name, parent_id=tag.parent_id, role=SpaceRole.OWNER.value
    )


@router.patch(
    "/spaces/{space_id}/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseOut
)
async def update_knowledge_base(
    knowledge_base_id: uuid.UUID,
    body: KnowledgeBaseUpdateRequest,
    access: SpaceAccess = Depends(require_space(SpaceRole.VIEWER)),
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBaseOut:
    tag = await knowledge_base_service.get_tag(session, knowledge_base_id)
    tag = await knowledge_base_service.update(
        session,
        identity=identity,
        space=access.space,
        tag=tag,
        name=body.name,
        parent_id=body.parent_id,
        change_parent="parent_id" in body.model_fields_set,
    )
    role = await tag_role(session, tag_id=tag.id, user_id=uuid.UUID(identity.user_id))
    return KnowledgeBaseOut(
        id=tag.id, name=tag.name, parent_id=tag.parent_id, role=role.value if role else None
    )


@router.delete("/spaces/{space_id}/knowledge-bases/{knowledge_base_id}", status_code=204)
async def delete_knowledge_base(
    knowledge_base_id: uuid.UUID,
    access: SpaceAccess = Depends(require_space(SpaceRole.VIEWER)),
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    tag = await knowledge_base_service.get_tag(session, knowledge_base_id)
    await knowledge_base_service.delete(session, identity=identity, space=access.space, tag=tag)


@router.get(
    "/spaces/{space_id}/knowledge-bases/{knowledge_base_id}/members",
    response_model=KnowledgeBaseMemberListOut,
)
async def list_knowledge_base_members(
    knowledge_base_id: uuid.UUID,
    access: SpaceAccess = Depends(require_space(SpaceRole.VIEWER)),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBaseMemberListOut:
    tag = await knowledge_base_service.get_tag(session, knowledge_base_id)
    await knowledge_base_service.ensure_tag_in_space(tag, access.space)
    if access.role == SpaceRole.VIEWER and (
        access.member is None
        or await tag_role(session, tag_id=tag.id, user_id=access.member.user_id) is None
    ):
        from loomvec.core.errors import PermissionDeniedError

        raise PermissionDeniedError(reason="无该知识库成员列表权限")
    members = await TagRepo(session).members_for(tag.id)
    users = (
        {
            u.id: u
            for u in (
                await session.execute(select(User).where(User.id.in_([m.user_id for m in members])))
            )
            .scalars()
            .all()
        }
        if members
        else {}
    )
    return KnowledgeBaseMemberListOut(
        items=[
            KnowledgeBaseMemberOut(
                user_id=m.user_id,
                username=users.get(m.user_id).username if users.get(m.user_id) else None,
                display_name=users.get(m.user_id).display_name if users.get(m.user_id) else None,
                role=m.role.value,
            )
            for m in members
        ]
    )


@router.put(
    "/spaces/{space_id}/knowledge-bases/{knowledge_base_id}/members",
    response_model=KnowledgeBaseMemberOut,
)
async def set_knowledge_base_member(
    knowledge_base_id: uuid.UUID,
    body: KnowledgeBaseMemberRequest,
    access: SpaceAccess = Depends(require_space(SpaceRole.VIEWER)),
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> KnowledgeBaseMemberOut:
    tag = await knowledge_base_service.get_tag(session, knowledge_base_id)
    user = await space_service.user_by_username(session, body.username)
    if user is None:
        from loomvec.core.errors import NotFoundError

        raise NotFoundError(resource="user", id=body.username)
    member = await knowledge_base_service.set_member(
        session,
        identity=identity,
        space=access.space,
        tag=tag,
        user=user,
        role=body.role,
    )
    return KnowledgeBaseMemberOut(
        user_id=member.user_id,
        username=user.username,
        display_name=user.display_name,
        role=member.role.value,
    )


@router.delete(
    "/spaces/{space_id}/knowledge-bases/{knowledge_base_id}/members/{user_id}", status_code=204
)
async def remove_knowledge_base_member(
    knowledge_base_id: uuid.UUID,
    user_id: uuid.UUID,
    access: SpaceAccess = Depends(require_space(SpaceRole.VIEWER)),
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> None:
    tag = await knowledge_base_service.get_tag(session, knowledge_base_id)
    await knowledge_base_service.remove_member(
        session, identity=identity, space=access.space, tag=tag, user_id=user_id
    )
