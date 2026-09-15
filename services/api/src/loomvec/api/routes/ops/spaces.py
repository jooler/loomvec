"""P5-OPS 运营域空间接口：公共空间 CRUD / 用量 / 分组可见性。

可见性模型：space_group_visibility 行存在即该分组可见（勾选即授权）；
空集合 = 不对任何分组公开（用户端不可链接）。PUT 全量替换勾选集合；
分组被移出后用户既有链接行保留但检索即时失效（authz.linked_public_space_ids
即时求交），不做后台清理。

运营者成员行兜底：公共空间的内容管理（上传/审核/图谱）复用用户域接口
（require_space 成员闸门），因此运营域内对具体空间的访问会自动把当前
运营者补齐为该空间 owner 成员（已有成员则提升为 owner），保证多运营者
协作时不因"非创建者"而无法维护内容。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_milvus, get_session, get_storage, require_ops
from loomvec.api.services import spaces as space_service
from loomvec.core.constants import CHUNK_PRESETS, EMBEDDING_MODELS
from loomvec.core.db.models import (
    Space,
    SpaceGroupVisibility,
    SpaceMember,
    SpaceRole,
    SpaceType,
    SpaceUsage,
    User,
    UserGroup,
    UserGroupMember,
)
from loomvec.core.db.repos import SpaceMemberRepo
from loomvec.core.errors import NotFoundError, ValidationError
from loomvec.core.storage import ObjectStorage

router = APIRouter(tags=["ops-spaces"])


class OpsSpaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    review_required: bool = False
    embedding_model: str | None = Field(
        default=None, description=f"白名单：{list(EMBEDDING_MODELS)}"
    )
    chunk_preset: str | None = Field(default=None, description=f"白名单：{list(CHUNK_PRESETS)}")


class OpsSpaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    review_required: bool | None = None
    embedding_model: str | None = None
    chunk_preset: str | None = None
    quota_storage_bytes: int | None = Field(default=None, ge=0)
    quota_file_count: int | None = Field(default=None, ge=0)


class OpsSpaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    review_required: bool
    embedding_model: str | None
    chunk_preset: str | None
    owner_id: uuid.UUID | None
    created_at: object
    member_count: int
    group_count: int
    storage_bytes: int
    file_count: int


class OpsSpaceListOut(BaseModel):
    items: list[OpsSpaceOut]


class GroupVisibilityItem(BaseModel):
    group_id: uuid.UUID
    name: str
    description: str | None
    member_count: int
    visible: bool


class GroupVisibilityListOut(BaseModel):
    items: list[GroupVisibilityItem]


class GroupVisibilityRequest(BaseModel):
    group_ids: list[uuid.UUID] = Field(default_factory=list)


class OpsSpaceUsageOut(BaseModel):
    space_id: uuid.UUID
    storage_bytes: int
    file_count: int
    quota_storage_bytes: int
    quota_file_count: int


async def get_public_space(
    space_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Space:
    """公共空间定位：不存在 / 非公共类型统一 404（不泄露空间存在性）。"""
    space = (
        await session.execute(select(Space).where(Space.id == space_id, Space.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if space is None or space.space_type != SpaceType.PUBLIC:
        raise NotFoundError(resource="public_space", id=str(space_id))
    return space


async def ensure_owner_membership(session: AsyncSession, space: Space, identity: Identity) -> None:
    """运营者 → 公共空间 owner 成员兜底（已有成员行则提升为 owner）。"""
    user_id = uuid.UUID(identity.user_id)
    member = await SpaceMemberRepo(session).get(space.id, user_id)
    escalated = False
    if member is not None:
        if member.role == SpaceRole.OWNER:
            return
        member.role = SpaceRole.OWNER
        escalated = True
    else:
        session.add(SpaceMember(space_id=space.id, user_id=user_id, role=SpaceRole.OWNER))
    await session.flush()
    await record_audit(
        session,
        identity=identity,
        action="ops.membership_escalated" if escalated else "ops.membership_granted",
        object_type="space_member",
        object_id=str(space.id),
        after={"space_id": str(space.id), "user_id": str(user_id), "role": "owner"},
    )
    await session.commit()


def _space_out(
    s: Space,
    *,
    member_count: int,
    group_count: int,
    storage_bytes: int,
    file_count: int,
) -> OpsSpaceOut:
    return OpsSpaceOut(
        id=s.id,
        slug=s.slug,
        name=s.name,
        description=s.description,
        review_required=s.review_required,
        embedding_model=s.embedding_model,
        chunk_preset=s.chunk_preset,
        owner_id=s.owner_id,
        created_at=s.created_at,
        member_count=member_count,
        group_count=group_count,
        storage_bytes=storage_bytes,
        file_count=file_count,
    )


async def _group_counts(session: AsyncSession, space_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not space_ids:
        return {}
    rows = (
        await session.execute(
            select(SpaceGroupVisibility.space_id, func.count())
            .where(SpaceGroupVisibility.space_id.in_(space_ids))
            .group_by(SpaceGroupVisibility.space_id)
        )
    ).all()
    return {sid: int(n) for sid, n in rows}


async def _usage_map(
    session: AsyncSession, space_ids: list[uuid.UUID]
) -> dict[uuid.UUID, SpaceUsage]:
    if not space_ids:
        return {}
    rows = (
        (await session.execute(select(SpaceUsage).where(SpaceUsage.space_id.in_(space_ids))))
        .scalars()
        .all()
    )
    return {u.space_id: u for u in rows}


@router.get("/spaces", response_model=OpsSpaceListOut)
async def list_public_spaces(
    identity: Identity = Depends(require_ops("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> OpsSpaceListOut:
    """公共空间列表（运营端左侧栏；仅 space_type=public）。"""
    spaces = list(
        (
            await session.execute(
                select(Space)
                .where(
                    Space.space_type == SpaceType.PUBLIC,
                    Space.deleted_at.is_(None),
                )
                .order_by(Space.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    counts = await _group_counts(session, [s.id for s in spaces])
    usages = await _usage_map(session, [s.id for s in spaces])
    items = [
        _space_out(
            s,
            member_count=await space_service.member_count(session, s.id),
            group_count=counts.get(s.id, 0),
            storage_bytes=usages[s.id].storage_bytes if s.id in usages else 0,
            file_count=usages[s.id].file_count if s.id in usages else 0,
        )
        for s in spaces
    ]
    return OpsSpaceListOut(items=items)


@router.post("/spaces", response_model=OpsSpaceOut, status_code=201)
async def create_public_space(
    body: OpsSpaceCreateRequest,
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> OpsSpaceOut:
    """创建公共空间：创建者（运营者）成为 owner，类型固定为 public。"""
    space = await space_service.create_space(
        session,
        identity=identity,
        name=body.name,
        description=body.description,
        space_type=SpaceType.PUBLIC,
        review_required=body.review_required,
        embedding_model=body.embedding_model,
        chunk_preset=body.chunk_preset,
    )
    return _space_out(
        space,
        member_count=1,
        group_count=0,
        storage_bytes=0,
        file_count=0,
    )


@router.get("/spaces/{space_id}", response_model=OpsSpaceOut)
async def get_public_space_detail(
    space: Space = Depends(get_public_space),
    identity: Identity = Depends(require_ops("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> OpsSpaceOut:
    await ensure_owner_membership(session, space, identity)
    counts = await _group_counts(session, [space.id])
    usages = await _usage_map(session, [space.id])
    return _space_out(
        space,
        member_count=await space_service.member_count(session, space.id),
        group_count=counts.get(space.id, 0),
        storage_bytes=usages[space.id].storage_bytes if space.id in usages else 0,
        file_count=usages[space.id].file_count if space.id in usages else 0,
    )


@router.patch("/spaces/{space_id}", response_model=OpsSpaceOut)
async def update_public_space(
    body: OpsSpaceUpdateRequest,
    space: Space = Depends(get_public_space),
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> OpsSpaceOut:
    """公共空间设置（运营域）：名称/描述/审核开关/模型预设/空间级配额。"""
    await ensure_owner_membership(session, space, identity)
    fields = body.model_dump(exclude_unset=True)
    space = await space_service.update_space_settings(session, space=space, **fields)
    counts = await _group_counts(session, [space.id])
    usages = await _usage_map(session, [space.id])
    return _space_out(
        space,
        member_count=await space_service.member_count(session, space.id),
        group_count=counts.get(space.id, 0),
        storage_bytes=usages[space.id].storage_bytes if space.id in usages else 0,
        file_count=usages[space.id].file_count if space.id in usages else 0,
    )


@router.delete("/spaces/{space_id}", status_code=204)
async def delete_public_space(
    space: Space = Depends(get_public_space),
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
    milvus=Depends(get_milvus),
    storage: ObjectStorage = Depends(get_storage),
) -> None:
    """删除公共空间：级联清理 PG + Milvus 向量 + 对象存储（同用户域 owner 删除）。"""
    await space_service.delete_space_cascade(session, space=space, milvus=milvus, storage=storage)


@router.get("/spaces/{space_id}/usage", response_model=OpsSpaceUsageOut)
async def public_space_usage(
    space: Space = Depends(get_public_space),
    identity: Identity = Depends(require_ops("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> OpsSpaceUsageOut:
    return OpsSpaceUsageOut(**await space_service.space_usage_out(session, space))


# ---- 分组可见性（原"成员管理"语义的运营端形态：勾选分组 = 空间可见） ----


async def _visibility_items(session: AsyncSession, space: Space) -> list[GroupVisibilityItem]:
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
    visible_ids = set(
        (
            await session.execute(
                select(SpaceGroupVisibility.group_id).where(
                    SpaceGroupVisibility.space_id == space.id
                )
            )
        )
        .scalars()
        .all()
    )
    member_counts: dict[uuid.UUID, int] = {}
    if groups:
        rows = (
            await session.execute(
                select(UserGroupMember.group_id, func.count())
                .where(UserGroupMember.group_id.in_([g.id for g in groups]))
                .group_by(UserGroupMember.group_id)
            )
        ).all()
        member_counts = {gid: int(n) for gid, n in rows}
    return [
        GroupVisibilityItem(
            group_id=g.id,
            name=g.name,
            description=g.description,
            member_count=member_counts.get(g.id, 0),
            visible=g.id in visible_ids,
        )
        for g in groups
    ]


@router.get("/spaces/{space_id}/visibility", response_model=GroupVisibilityListOut)
async def get_space_visibility(
    space: Space = Depends(get_public_space),
    identity: Identity = Depends(require_ops("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> GroupVisibilityListOut:
    """全部分组 + 当前空间的勾选状态（可见性页签数据源）。"""
    return GroupVisibilityListOut(items=await _visibility_items(session, space))


@router.put("/spaces/{space_id}/visibility", response_model=GroupVisibilityListOut)
async def set_space_visibility(
    body: GroupVisibilityRequest,
    space: Space = Depends(get_public_space),
    identity: Identity = Depends(require_ops("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> GroupVisibilityListOut:
    """全量替换可见分组集合（勾选提交）；显式审计 before/after。"""
    requested = list(dict.fromkeys(body.group_ids))
    if requested:
        exists = set(
            (
                await session.execute(
                    select(UserGroup.id).where(
                        UserGroup.id.in_(requested), UserGroup.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        missing = [str(g) for g in requested if g not in exists]
        if missing:
            raise ValidationError("存在不存在的分组", group_ids=missing[:5])
    before = set(
        (
            await session.execute(
                select(SpaceGroupVisibility.group_id).where(
                    SpaceGroupVisibility.space_id == space.id
                )
            )
        )
        .scalars()
        .all()
    )
    for row in (
        (
            await session.execute(
                select(SpaceGroupVisibility).where(SpaceGroupVisibility.space_id == space.id)
            )
        )
        .scalars()
        .all()
    ):
        if row.group_id not in set(requested):
            await session.delete(row)
    for gid in requested:
        if gid not in before:
            session.add(SpaceGroupVisibility(space_id=space.id, group_id=gid))
    await session.commit()
    await record_audit(
        session,
        identity=identity,
        action="ops.space_visibility_set",
        object_type="space",
        object_id=str(space.id),
        before={"group_ids": sorted(str(g) for g in before)},
        after={"group_ids": sorted(str(g) for g in requested)},
    )
    return GroupVisibilityListOut(items=await _visibility_items(session, space))


# ---- 分组内用户查询（可见性页签的分组详情展开用） ----


@router.get("/spaces/{space_id}/groups/{group_id}/members")
async def list_group_members_for_space(
    group_id: uuid.UUID,
    space: Space = Depends(get_public_space),
    identity: Identity = Depends(require_ops("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """指定分组内的用户列表（只读，成员管理入口在 /ops/groups）。"""
    rows = (
        (
            await session.execute(
                select(User)
                .join(UserGroupMember, UserGroupMember.user_id == User.id)
                .where(UserGroupMember.group_id == group_id, User.deleted_at.is_(None))
                .order_by(User.username.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "user_id": u.id,
                "username": u.username,
                "display_name": u.display_name,
            }
            for u in rows
        ]
    }
