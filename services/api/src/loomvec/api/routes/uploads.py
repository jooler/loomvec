"""P1/P2 上传链路：预签名直传 + 文件/文本资产登记（触发管线）+ 去重预检。

P2：目标空间经 body.space_id 指定并做 editor 闸门校验；配额硬校验、
重复 checksum 提示在登记链路完成。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    body_space,
    get_celery,
    get_session,
    get_storage,
    require_scope,
    service_settings,
)
from loomvec.api.schemas.assets import (
    AssetCreateRequest,
    AssetOut,
    AssetTextRequest,
    DuplicateCheckOut,
    UploadRequest,
    UploadResponse,
)
from loomvec.api.services import assets as asset_service
from loomvec.core.config import Settings

router = APIRouter(prefix="/api/v1", tags=["uploads"])


@router.post("/uploads", response_model=UploadResponse)
async def create_upload(
    body: UploadRequest,
    identity: Identity = Depends(require_scope("write")),
    settings: Settings = Depends(service_settings),
    storage=Depends(get_storage),
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    space = await body_space(session, identity, settings, body.space_id)
    key, url, expires_in = await asset_service.presign_upload(
        session, settings, storage, body, space.id
    )
    return UploadResponse(key=key, upload_url=url, expires_in=expires_in)


@router.post("/assets", response_model=AssetOut, status_code=201)
async def create_asset(
    body: AssetCreateRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
    storage=Depends(get_storage),
    celery=Depends(get_celery),
) -> AssetOut:
    """登记已直传的文件资产：对象校验 + 两级配额闸门 + 入队管线。"""
    space = await body_space(session, identity, settings, body.space_id)
    asset = await asset_service.register_file_asset(
        session,
        identity=identity,
        settings=settings,
        storage=storage,
        key=body.key,
        filename=body.filename,
        size=body.size,
        content_type=body.content_type,
        space=space,
        folder_id=body.folder_id,
    )
    asset_service.enqueue_pipeline(celery, str(asset.id))
    return AssetOut(**asset_service.asset_out(asset))


@router.post("/assets/text", response_model=AssetOut, status_code=201)
async def create_text_asset(
    body: AssetTextRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
    celery=Depends(get_celery),
) -> AssetOut:
    """文本/Markdown 直接摄取（inline_text，无需对象存储）。"""
    space = await body_space(session, identity, settings, body.space_id)
    asset = await asset_service.register_text_asset(
        session,
        identity=identity,
        settings=settings,
        name=body.name,
        content=body.content,
        space=space,
        folder_id=body.folder_id,
    )
    asset_service.enqueue_pipeline(celery, str(asset.id))
    return AssetOut(**asset_service.asset_out(asset))


@router.get("/assets/duplicate-check", response_model=DuplicateCheckOut)
async def duplicate_check(
    checksum: str,
    space_id: uuid.UUID | None = None,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
) -> DuplicateCheckOut:
    """去重预检：同空间内同 checksum 的现存资产（编辑器+；不阻断上传）。"""
    space = await body_space(session, identity, settings, space_id)
    rows = await asset_service.check_duplicate(session, space=space, checksum=checksum)
    return DuplicateCheckOut(
        exists=bool(rows),
        assets=[AssetOut(**asset_service.asset_out(a)) for a in rows],
    )
