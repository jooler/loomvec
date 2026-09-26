"""Finder 文件夹接口：空间内目录树 CRUD / 移动（防环）/ 级联删除 / 递归复制 +
单资产复制。权限对齐资产域：读 viewer+，写 editor+（owner 不限）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    body_space,
    get_celery,
    get_milvus,
    get_session,
    require_scope,
    resolve_space_access,
    service_settings,
)
from loomvec.api.identity import user_uuid
from loomvec.api.schemas.assets import (
    AssetCopyRequest,
    AssetMoveRequest,
    AssetOut,
    FolderCopyRequest,
    FolderCreateRequest,
    FolderMoveRequest,
    FolderOut,
    FolderRenameRequest,
)
from loomvec.api.services import assets as asset_service
from loomvec.api.services import folders as folder_service
from loomvec.api.services.folders import resolve_folder
from loomvec.core.config import Settings
from loomvec.core.db.models import SpaceRole
from loomvec.core.db.repos import AssetFolderRepo

router = APIRouter(prefix="/api/v1", tags=["folders"])


def _folder_out(f) -> FolderOut:
    return FolderOut(
        id=f.id,
        space_id=f.space_id,
        parent_id=f.parent_id,
        name=f.name,
        created_at=f.created_at,
        created_by=f.created_by,
    )


@router.get("/spaces/{space_id}/folders", response_model=list[FolderOut])
async def list_folders(
    space_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> list[FolderOut]:
    """空间内全部活文件夹（扁平列表，前端组树/算面包屑）。"""
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    rows = await AssetFolderRepo(session).list_for_space(space_id)
    return [_folder_out(f) for f in rows]


@router.post("/spaces/{space_id}/folders", response_model=FolderOut, status_code=201)
async def create_folder(
    space_id: uuid.UUID,
    body: FolderCreateRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
) -> FolderOut:
    space = await body_space(session, identity, settings, space_id, SpaceRole.EDITOR)
    folder = await folder_service.create_folder(
        session, identity=identity, space=space, name=body.name, parent_id=body.parent_id
    )
    return _folder_out(folder)


@router.patch("/folders/{folder_id}", response_model=FolderOut)
async def rename_folder(
    folder_id: uuid.UUID,
    body: FolderRenameRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> FolderOut:
    access, folder = await resolve_folder(session, identity, folder_id, min_role=SpaceRole.EDITOR)
    del access  # 结构管理不限创建者：editor+ 即可（闸门已由 resolve_folder 保证）
    await folder_service.rename_folder(session, folder, body.name)
    return _folder_out(folder)


@router.post("/folders/{folder_id}/move", response_model=FolderOut)
async def move_folder(
    folder_id: uuid.UUID,
    body: FolderMoveRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> FolderOut:
    """移动文件夹（同空间内）：parent_id 空 + unset_parent=True 移到根目录。"""
    access, folder = await resolve_folder(session, identity, folder_id, min_role=SpaceRole.EDITOR)
    del access  # 结构管理不限创建者：editor+ 即可
    parent = None if body.unset_parent else body.parent_id
    await folder_service.move_folder(session, folder, parent)
    return _folder_out(folder)


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: uuid.UUID,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    milvus=Depends(get_milvus),
) -> Response:
    """删除文件夹：级联软删后代文件夹与其中资产（向量/图谱/配额联动清理）。"""
    access, folder = await resolve_folder(session, identity, folder_id, min_role=SpaceRole.EDITOR)
    del access  # 结构管理不限创建者：editor+ 即可
    await folder_service.delete_folder_cascade(session, milvus=milvus, folder=folder)
    return Response(status_code=204)


@router.post("/folders/{folder_id}/copy", response_model=FolderOut)
async def copy_folder(
    folder_id: uuid.UUID,
    body: FolderCopyRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    celery=Depends(get_celery),
) -> FolderOut:
    """递归复制文件夹树（含资产）：目标父下同名文件夹并入（合并语义）。"""
    access, folder = await resolve_folder(session, identity, folder_id, min_role=SpaceRole.EDITOR)
    del access  # 结构管理不限创建者：editor+ 即可

    def enqueue(asset_id: str) -> None:
        asset_service.enqueue_pipeline(celery, asset_id)

    dest = await folder_service.copy_folder_recursive(
        session, identity=identity, source=folder, target_parent_id=body.parent_id, enqueue=enqueue
    )
    return _folder_out(dest)


@router.post("/assets/{asset_id}/copy", response_model=AssetOut, status_code=201)
async def copy_asset(
    asset_id: uuid.UUID,
    body: AssetCopyRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
    celery=Depends(get_celery),
) -> AssetOut:
    """复制资产：默认同空间同文件夹；可指定目标空间（editor+）与目标文件夹。

    源空间须为真实成员：公共空间虚拟 viewer 不可把运营内容复制到自己的空间。
    """
    from loomvec.api.routes.assets import _load_asset_detail
    from loomvec.core.errors import PermissionDeniedError

    access, asset = await _load_asset_detail(session, asset_id, identity)
    if access.member is None:
        raise PermissionDeniedError(reason="无该资产的复制权限", space_id=str(asset.space_id))
    target_space_id = body.target_space_id or asset.space_id
    space = await body_space(session, identity, settings, target_space_id, SpaceRole.EDITOR)
    target_folder_id = body.target_folder_id
    if target_space_id != asset.space_id and target_folder_id is not None:
        # 跨空间复制不能携带源空间文件夹引用
        from loomvec.api.services.folders import validate_folder_in_space

        target_folder_id = await validate_folder_in_space(session, space.id, target_folder_id)
    copy = await folder_service.copy_asset(
        session,
        identity=identity,
        source=asset,
        target_space=space,
        target_folder_id=target_folder_id,
    )
    asset_service.enqueue_pipeline(celery, str(copy.id))
    return AssetOut(**asset_service.asset_out(copy))


@router.patch("/assets/{asset_id}/location", response_model=AssetOut)
async def move_asset(
    asset_id: uuid.UUID,
    body: AssetMoveRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
) -> AssetOut:
    """移动资产（同空间内）：folder_id 指定目标文件夹；unset_folder=True 移到根目录。"""
    from loomvec.api.routes.assets import _load_asset_detail, _require_asset_manager
    from loomvec.core.errors import ValidationError

    access, asset = await _load_asset_detail(session, asset_id, identity)
    _require_asset_manager(
        access, asset, user_uuid(identity) if not identity.user_id.startswith("apikey:") else None
    )
    if body.folder_id is None and not body.unset_folder:
        raise ValidationError("folder_id 与 unset_folder 不能同时为空")
    await asset_service.update_asset(
        session, asset=asset, folder_id=body.folder_id, unset_folder=body.unset_folder
    )
    return AssetOut(**asset_service.asset_out(asset))
