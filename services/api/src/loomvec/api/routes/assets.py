"""P1/P2 资产接口：游标列表（标签/分类/审核过滤）/ 详情（标签/元数据/派生物）/
编辑 / 级联删除 / 预览（含图片原图）/ 任务 / 去重提示。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_milvus,
    get_session,
    get_storage,
    require_scope,
    resolve_space_access,
    service_settings,
)
from loomvec.api.identity import user_uuid
from loomvec.api.schemas.assets import (
    AssetDetailOut,
    AssetListOut,
    AssetOut,
    AssetPatchRequest,
    PreviewOut,
    RenditionOut,
    TagOut,
)
from loomvec.api.services import assets as asset_service
from loomvec.core.authz import (
    SpaceAccess,
    can_manage_asset,
    decide_asset_visibility,
    visible_space_ids,
)
from loomvec.core.config import Settings
from loomvec.core.db.models import SpaceRole
from loomvec.core.db.repos import AssetRenditionRepo, AssetRepo, AssetVersionRepo, TagRepo
from loomvec.core.errors import NotFoundError, PermissionDeniedError
from loomvec.core.storage import ObjectStorage

router = APIRouter(prefix="/api/v1", tags=["assets"])


async def _visible_space_ids(session: AsyncSession, identity: Identity) -> list[uuid.UUID]:
    if identity.user_id.startswith("apikey:"):
        return []  # API Key 聚合列表不开放（空间接口按租户闸门）
    return await visible_space_ids(session, user_id=user_uuid(identity))


@router.get("/assets", response_model=AssetListOut)
async def list_assets(
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    status: str | None = None,
    ext: str | None = None,
    tag_ids: str | None = Query(default=None, description="逗号分隔 tag id"),
    category_id: uuid.UUID | None = None,
    review_status: str | None = None,
    space_id: uuid.UUID | None = None,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> AssetListOut:
    """资产列表：指定空间（成员校验）或聚合我的全部空间（viewer 可见性过滤）。"""
    from loomvec.core.db.pagination import Cursor

    cur = Cursor.decode(cursor) if cursor else None
    tag_id_list = (
        [uuid.UUID(t.strip()) for t in tag_ids.split(",") if t.strip()] if tag_ids else None
    )
    include_unreviewed = False
    strict_review = False

    if space_id is not None:
        access = await resolve_space_access(session, identity, space_id, min_role=SpaceRole.VIEWER)
        space_ids = [space_id]
        if access.role in (SpaceRole.EDITOR, SpaceRole.OWNER):
            include_unreviewed = True
        else:
            strict_review = access.space.review_required
    else:
        space_ids = await _visible_space_ids(session, identity)

    rows = await AssetRepo(session).list_in_spaces(
        space_ids,
        limit=limit + 1,
        cursor_created_at=cur.created_at if cur else None,
        cursor_id=cur.id if cur else None,
        status=status,
        ext=ext if not ext or ext.startswith(".") else f".{ext}",
        include_unreviewed=include_unreviewed,
        strict_review=strict_review,
        tag_ids=tag_id_list,
        category_id=category_id,
        review_status=review_status,
    )
    next_cursor = asset_service.encode_cursor(rows[-1]) if len(rows) > limit else None
    return AssetListOut(
        items=[AssetOut(**asset_service.asset_out(a)) for a in rows[:limit]],
        next_cursor=next_cursor,
    )


async def _load_asset_detail(
    session: AsyncSession, asset_id: uuid.UUID, identity: Identity
) -> tuple[SpaceAccess, object]:
    """详情/编辑/删除公共入口：定位资产 → 空间闸门 → viewer 审核可见性。"""
    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))
    access = await resolve_space_access(session, identity, asset.space_id, SpaceRole.VIEWER)
    role = access.role if access.member else SpaceRole.EDITOR  # apikey → 租户兜底视为 editor
    if not decide_asset_visibility(
        review_required=access.space.review_required,
        review_status=asset.review_status,
        role=role,
    ):
        raise NotFoundError(resource="asset", id=str(asset_id))
    return access, asset


async def _build_detail(
    session: AsyncSession,
    asset,
    settings: Settings,
    storage: ObjectStorage,
) -> AssetDetailOut:
    version = await AssetVersionRepo(session).latest_for(asset.id)
    tags = await TagRepo(session).tags_for_asset(asset.id)
    renditions = await AssetRenditionRepo(session).for_asset(asset.id)
    rendition_out = []
    for r in renditions:
        try:
            url: str | None = storage.presign_get(settings.storage.bucket_derived, r.storage_key)
        except Exception:  # 预签名失败不阻塞详情
            url = None
        rendition_out.append(
            RenditionOut(
                kind=r.kind.value,
                mime_type=r.mime_type,
                width=r.width,
                height=r.height,
                url=url,
            )
        )
    base = asset_service.asset_out(asset)
    return AssetDetailOut(
        **base,
        chunk_method=asset.asset_meta.get("chunk_method"),
        version_meta=version.version_meta if version else {},
        asset_meta=asset.asset_meta,
        tags=[TagOut(id=t.id, name=t.name) for t in tags],
        category_id=asset.category_id,
        metadata=asset.user_meta,
        review_reason=asset.review_reason,
        renditions=rendition_out,
    )


@router.get("/assets/{asset_id}", response_model=AssetDetailOut)
async def get_asset(
    asset_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
    storage: ObjectStorage = Depends(get_storage),
) -> AssetDetailOut:
    _access, asset = await _load_asset_detail(session, asset_id, identity)
    del _access
    return await _build_detail(session, asset, settings, storage)


@router.patch("/assets/{asset_id}", response_model=AssetDetailOut)
async def patch_asset(
    asset_id: uuid.UUID,
    body: AssetPatchRequest,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
    storage: ObjectStorage = Depends(get_storage),
) -> AssetDetailOut:
    """编辑资产（editor+）：名称/标签/分类/元数据（空间 schema 校验）。"""
    access, asset = await _load_asset_detail(session, asset_id, identity)
    _require_asset_manager(
        access, asset, user_uuid(identity) if not identity.user_id.startswith("apikey:") else None
    )
    await asset_service.update_asset(
        session,
        asset=asset,
        name=body.name,
        tag_names=body.tags,
        category_id=body.category_id,
        unset_category=body.unset_category,
        user_meta=body.metadata,
    )
    return await _build_detail(session, asset, settings, storage)


def _require_asset_manager(access: SpaceAccess, asset, user_id: uuid.UUID | None) -> None:
    """editor 仅可管理自己上传的资产；owner 不限；viewer 拒绝（doc 05 能力矩阵）。

    API Key 身份（member 为空）按租户级 editor 兜底，仅可管理 API Key 登记的资产。
    """
    role = access.role if access.member else SpaceRole.EDITOR
    if not can_manage_asset(role=role, asset_created_by=asset.created_by, user_id=user_id):
        raise PermissionDeniedError(reason="无该资产的编辑/删除权限（editor 仅限本人上传）")


@router.delete("/assets/{asset_id}", status_code=204)
async def delete_asset(
    asset_id: uuid.UUID,
    identity: Identity = Depends(require_scope("write")),
    session: AsyncSession = Depends(get_session),
    milvus=Depends(get_milvus),
) -> Response:
    """删除资产（editor+）：软删 + 级联清理向量与配额扣减。"""
    access, asset = await _load_asset_detail(session, asset_id, identity)
    _require_asset_manager(
        access, asset, user_uuid(identity) if not identity.user_id.startswith("apikey:") else None
    )
    await asset_service.delete_asset_cascade(session, milvus=milvus, asset_id=asset_id)
    return Response(status_code=204)


@router.get("/assets/{asset_id}/preview", response_model=PreviewOut)
async def preview_asset(
    asset_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
    storage: ObjectStorage = Depends(get_storage),
) -> PreviewOut:
    """预览与定位：PDF 原文 / Markdown 产物 / 内联文本 / 图片原图。"""
    _access, asset = await _load_asset_detail(session, asset_id, identity)
    version = await AssetVersionRepo(session).latest_for(asset_id)
    if version is None:
        raise NotFoundError(resource="asset_version", id=str(asset_id))

    parse_meta = (version.version_meta or {}).get("parse") or {}
    if asset.storage_key and (
        asset.mime_type == "application/pdf" or asset.mime_type.startswith("image/")
    ):
        return PreviewOut(
            mode="image" if asset.mime_type.startswith("image/") else "pdf",
            url=storage.presign_get(settings.storage.bucket_raw, asset.storage_key),
            page_count=parse_meta.get("page_count"),
        )
    md_key = parse_meta.get("md_key")
    if md_key:
        return PreviewOut(
            mode="markdown",
            url=storage.presign_get(settings.storage.bucket_derived, md_key),
            page_count=parse_meta.get("page_count"),
        )
    if version.inline_text is not None:
        return PreviewOut(mode="markdown", content=version.inline_text, page_count=1)
    raise NotFoundError(resource="preview", id=str(asset_id))
