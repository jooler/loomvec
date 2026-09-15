"""P2-API-01 空间接口：创建（白名单内选模型/预设）、列表（我的空间）、
设置（owner）、删除（级联清理 PG+Milvus+对象存储）、用量查询。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_milvus,
    get_session,
    get_storage,
    require_scope,
    require_space,
)
from loomvec.api.services import spaces as space_service
from loomvec.core.authz import SpaceAccess
from loomvec.core.constants import CHUNK_PRESETS, EMBEDDING_MODELS
from loomvec.core.db.models import SpaceRole, SpaceType
from loomvec.core.db.repos import SpaceMemberRepo
from loomvec.core.errors import ValidationError
from loomvec.core.storage import ObjectStorage

router = APIRouter(prefix="/api/v1", tags=["spaces"])


class SpaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    space_type: SpaceType = SpaceType.SHARED
    review_required: bool = False
    embedding_model: str | None = Field(
        default=None, description=f"白名单：{list(EMBEDDING_MODELS)}"
    )
    chunk_preset: str | None = Field(default=None, description=f"白名单：{list(CHUNK_PRESETS)}")


class SpaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    review_required: bool | None = None
    embedding_model: str | None = None
    chunk_preset: str | None = None
    quota_storage_bytes: int | None = Field(default=None, ge=0)
    quota_file_count: int | None = Field(default=None, ge=0)


class SpaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    space_type: str
    owner_id: uuid.UUID | None
    review_required: bool
    embedding_model: str | None
    chunk_preset: str | None
    quota_storage_bytes: int
    quota_file_count: int
    created_at: datetime
    my_role: str | None = None
    member_count: int | None = None


class SpaceListOut(BaseModel):
    items: list[SpaceOut]


class SpaceUsageOut(BaseModel):
    space_id: uuid.UUID
    storage_bytes: int
    file_count: int
    quota_storage_bytes: int
    quota_file_count: int


class MetadataFieldOut(BaseModel):
    """空间元数据 schema（P2-CORE-04；详情编辑器按此渲染）。"""

    id: uuid.UUID
    key: str
    name: str
    field_type: str
    required: bool
    options: list = []


class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None = None


class TagOut(BaseModel):
    id: uuid.UUID
    name: str


def _out(s, my_role: str | None = None, member_count: int | None = None) -> SpaceOut:
    return SpaceOut(**space_service.space_out(s, my_role=my_role, member_count=member_count))


@router.post("/spaces", response_model=SpaceOut, status_code=201)
async def create_space(
    body: SpaceCreateRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> SpaceOut:
    """创建空间：创建者成为 owner，同事务建立成员与用量行。"""
    if body.space_type == SpaceType.PUBLIC:
        # 公共空间由运营端创建（POST /api/v1/ops/spaces），用户端入口关闭
        raise ValidationError("公共空间由运营端创建", space_type=body.space_type.value)
    space = await space_service.create_space(
        session,
        identity=identity,
        name=body.name,
        description=body.description,
        space_type=body.space_type,
        review_required=body.review_required,
        embedding_model=body.embedding_model,
        chunk_preset=body.chunk_preset,
    )
    return _out(space, my_role=SpaceRole.OWNER.value, member_count=1)


@router.get("/spaces", response_model=SpaceListOut)
async def list_my_spaces(
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> SpaceListOut:
    """我的空间列表（成员关系过滤；非成员空间不可见）。"""
    from sqlalchemy import select

    from loomvec.core.db.models import Space as SpaceModel
    from loomvec.core.errors import ValidationError

    if identity.user_id.startswith("apikey:"):
        raise ValidationError("API Key 无用户空间列表")
    user_id = uuid.UUID(identity.user_id)
    memberships = await SpaceMemberRepo(session).list_for_user(user_id)
    by_space = {m.space_id: m for m in memberships}
    rows: list = []
    if by_space:
        rows = list(
            (
                await session.execute(
                    select(SpaceModel)
                    .where(
                        SpaceModel.id.in_(by_space.keys()),
                        SpaceModel.deleted_at.is_(None),
                    )
                    .order_by(SpaceModel.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    out = [
        _out(
            s,
            my_role=by_space[s.id].role.value,
            member_count=await space_service.member_count(session, s.id),
        )
        for s in rows
    ]
    return SpaceListOut(items=out)


@router.get("/spaces/{space_id}", response_model=SpaceOut)
async def get_space(
    access: SpaceAccess = Depends(require_space()),
    session: AsyncSession = Depends(get_session),
) -> SpaceOut:
    return _out(
        access.space,
        my_role=access.role.value,
        member_count=await space_service.member_count(session, access.space.id),
    )


@router.patch("/spaces/{space_id}", response_model=SpaceOut)
async def update_space(
    body: SpaceUpdateRequest,
    access: SpaceAccess = Depends(require_space(SpaceRole.OWNER)),
    session: AsyncSession = Depends(get_session),
) -> SpaceOut:
    """空间设置（owner）：名称/描述/审核开关/模型预设/空间级配额。"""
    fields = body.model_dump(exclude_unset=True)
    space = await space_service.update_space_settings(session, space=access.space, **fields)
    return _out(space, my_role=access.role.value)


@router.delete("/spaces/{space_id}", status_code=204)
async def delete_space(
    access: SpaceAccess = Depends(require_space(SpaceRole.OWNER)),
    session: AsyncSession = Depends(get_session),
    milvus=Depends(get_milvus),
    storage: ObjectStorage = Depends(get_storage),
) -> None:
    """删除空间（owner）：级联清理 PG + Milvus 向量 + 对象存储前缀。"""
    await space_service.delete_space_cascade(
        session, space=access.space, milvus=milvus, storage=storage
    )


@router.get("/spaces/{space_id}/usage", response_model=SpaceUsageOut)
async def space_usage(
    access: SpaceAccess = Depends(require_space()),
    session: AsyncSession = Depends(get_session),
) -> SpaceUsageOut:
    """空间用量与配额（配额进度展示）。"""
    return SpaceUsageOut(**await space_service.space_usage_out(session, access.space))


# ---- 空间级 schema / 分类 / 标签查询（资产详情编辑器渲染用） ----


@router.get("/spaces/{space_id}/metadata-fields", response_model=list[MetadataFieldOut])
async def list_metadata_fields(
    access: SpaceAccess = Depends(require_space()),
    session: AsyncSession = Depends(get_session),
) -> list[MetadataFieldOut]:
    from loomvec.core.db.repos import MetadataFieldRepo

    rows = await MetadataFieldRepo(session).list_for_space(access.space.id)
    return [
        MetadataFieldOut(
            id=f.id,
            key=f.key,
            name=f.name,
            field_type=f.field_type.value,
            required=f.required,
            options=f.options or [],
        )
        for f in rows
    ]


@router.get("/spaces/{space_id}/categories", response_model=list[CategoryOut])
async def list_categories(
    access: SpaceAccess = Depends(require_space()),
    session: AsyncSession = Depends(get_session),
) -> list[CategoryOut]:
    from loomvec.core.db.repos import CategoryRepo

    rows = await CategoryRepo(session).list_for_space(access.space.id)
    return [CategoryOut(id=c.id, name=c.name, parent_id=c.parent_id) for c in rows]


@router.get("/spaces/{space_id}/tags", response_model=list[TagOut])
async def list_space_tags(
    access: SpaceAccess = Depends(require_space()),
    session: AsyncSession = Depends(get_session),
) -> list[TagOut]:
    """空间可用标签（空间内实际使用过的去重集合；筛选器与编辑器渲染用）。"""
    from sqlalchemy import distinct, select

    from loomvec.core.db.models import Asset, AssetTag
    from loomvec.core.db.repos import TagRepo

    used = (
        (
            await session.execute(
                select(distinct(AssetTag.tag_id)).where(
                    AssetTag.asset_id.in_(select(Asset.id).where(Asset.space_id == access.space.id))
                )
            )
        )
        .scalars()
        .all()
    )
    rows = await TagRepo(session).list_by_ids(list(used))
    return [TagOut(id=t.id, name=t.name) for t in rows]
