"""chunk（语义单元）管理接口：列表 / 手动新增 / 编辑 / 批删。

借鉴 RAGFlow「Dataset → Document → Chunks」的管理语义：
- 查看：空间 viewer+ 可读（编辑权与资产编辑矩阵一致）；
- 新增/编辑：即时同步向量化（不入管线队列），见 services/units.py；
- 检索测试复用 POST /search 的 asset_ids 过滤，不在本模块重复实现。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_ai,
    get_milvus,
    get_session,
    get_storage,
    require_scope,
    service_settings,
)
from loomvec.api.identity import user_uuid
from loomvec.api.routes.assets import _load_asset_detail, _require_asset_manager
from loomvec.api.schemas.assets import (
    UnitBulkDeleteRequest,
    UnitCreateRequest,
    UnitListOut,
    UnitOut,
    UnitPatchRequest,
)
from loomvec.api.services import units as unit_service
from loomvec.core.config import Settings
from loomvec.core.storage import ObjectStorage

router = APIRouter(prefix="/api/v1", tags=["units"])


@router.get("/assets/{asset_id}/units", response_model=UnitListOut)
async def list_units(
    asset_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    unit_type: str | None = Query(default=None, description="text / table / image"),
    q: str | None = Query(default=None, max_length=256, description="内容/标题关键词"),
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> UnitListOut:
    _access, asset = await _load_asset_detail(session, asset_id, identity)
    rows, total = await unit_service.list_units(
        session, asset_id=asset.id, limit=limit, offset=offset, unit_type=unit_type, q=q
    )
    return UnitListOut(items=[UnitOut(**unit_service.unit_out(u)) for u in rows], total=total)


@router.post("/assets/{asset_id}/units", response_model=UnitOut, status_code=201)
async def create_unit(
    asset_id: uuid.UUID,
    body: UnitCreateRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    ai=Depends(get_ai),
    milvus=Depends(get_milvus),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(service_settings),
) -> UnitOut:
    """手动补片：追加 chunk 并即时向量化入索引。"""
    access, asset = await _load_asset_detail(session, asset_id, identity)
    _require_asset_manager(
        access, asset, user_uuid(identity) if not identity.user_id.startswith("apikey:") else None
    )
    unit = await unit_service.create_unit(
        session,
        ai=ai,
        milvus=milvus,
        storage=storage,
        settings=settings,
        asset=asset,
        content=body.content,
        title=body.title,
        keywords=body.keywords,
        unit_type=body.unit_type,
    )
    return UnitOut(**unit_service.unit_out(unit))


@router.patch("/assets/{asset_id}/units/{unit_id}", response_model=UnitOut)
async def update_unit(
    asset_id: uuid.UUID,
    unit_id: uuid.UUID,
    body: UnitPatchRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    ai=Depends(get_ai),
    milvus=Depends(get_milvus),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(service_settings),
) -> UnitOut:
    """编辑 chunk：标题/内容变更即重新向量化。"""
    access, asset = await _load_asset_detail(session, asset_id, identity)
    _require_asset_manager(
        access, asset, user_uuid(identity) if not identity.user_id.startswith("apikey:") else None
    )
    unit = await unit_service.get_unit(session, asset_id=asset.id, unit_id=unit_id)
    unit = await unit_service.update_unit(
        session,
        ai=ai,
        milvus=milvus,
        storage=storage,
        settings=settings,
        asset=asset,
        unit=unit,
        title=body.title,
        content=body.content,
        keywords=body.keywords,
    )
    return UnitOut(**unit_service.unit_out(unit))


@router.post("/assets/{asset_id}/units/bulk-delete", status_code=204)
async def bulk_delete_units(
    asset_id: uuid.UUID,
    body: UnitBulkDeleteRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    milvus=Depends(get_milvus),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(service_settings),
) -> Response:
    """批删 chunk：PG 行 + 向量缓存条目 + Milvus 向量即时清理。"""
    access, asset = await _load_asset_detail(session, asset_id, identity)
    _require_asset_manager(
        access, asset, user_uuid(identity) if not identity.user_id.startswith("apikey:") else None
    )
    await unit_service.delete_units(
        session,
        milvus=milvus,
        storage=storage,
        settings=settings,
        asset=asset,
        unit_ids=body.unit_ids,
    )
    return Response(status_code=204)
