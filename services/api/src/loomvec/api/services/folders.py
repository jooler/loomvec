"""Finder 式文件夹管理服务：目录树 CRUD / 移动（防环）/ 级联删除 / 递归复制。

结构不变量（服务层保证，路由层薄）：
- 文件夹属于唯一空间，移动只在同空间内（跨空间走复制）；
- 同父下活文件夹名唯一（软删行不占名）；
- parent 链无环（移动目标不得是自身或后代）；
- 删除文件夹级联软删后代文件夹与其中资产（配额扣减 + 向量/图谱清理）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.core.authz import SpaceAccess
from loomvec.core.db.models import (
    Asset,
    AssetFolder,
    AssetRendition,
    AssetStatus,
    AssetTag,
    AssetVersion,
    Space,
    SpaceRole,
)
from loomvec.core.db.repos import (
    AssetFolderRepo,
    AssetRenditionRepo,
    AssetRepo,
    AssetVersionRepo,
    TagRepo,
)
from loomvec.core.errors import NotFoundError, ValidationError
from loomvec.core.quota import apply_asset_added, apply_asset_removed, check_upload_quota

logger = structlog.get_logger("loomvec.api.folders")


async def resolve_folder(
    session: AsyncSession,
    identity: Identity,
    folder_id: uuid.UUID,
    *,
    min_role: SpaceRole,
) -> tuple[SpaceAccess, AssetFolder]:
    """定位文件夹 → 空间闸门（与资产域 _load_asset_detail 同构）。"""
    from loomvec.api.deps import resolve_space_access

    folder = await AssetFolderRepo(session).get_live(folder_id)
    if folder is None:
        raise NotFoundError(resource="asset_folder", id=str(folder_id))
    access = await resolve_space_access(session, identity, folder.space_id, min_role)
    return access, folder


async def validate_folder_in_space(
    session: AsyncSession, space_id: uuid.UUID, folder_id: uuid.UUID | None
) -> uuid.UUID | None:
    """校验目标文件夹属于该空间且未删除；None/缺省直通（根目录）。"""
    if folder_id is None:
        return None
    folder = await AssetFolderRepo(session).get_live(folder_id)
    if folder is None or folder.space_id != space_id:
        raise ValidationError("目标文件夹不存在或不属于该空间", folder_id=str(folder_id))
    return folder.id


async def create_folder(
    session: AsyncSession,
    *,
    identity: Identity,
    space: Space,
    name: str,
    parent_id: uuid.UUID | None,
) -> AssetFolder:
    name = (name or "").strip()
    if not name:
        raise ValidationError("文件夹名不能为空")
    await validate_folder_in_space(session, space.id, parent_id)
    repo = AssetFolderRepo(session)
    if await repo.find_sibling_name(space.id, parent_id, name) is not None:
        raise ValidationError("同级目录下已存在同名文件夹", name=name)
    folder = await repo.create(
        tenant_id=space.tenant_id,
        space_id=space.id,
        parent_id=parent_id,
        name=name,
        created_by=uuid.UUID(identity.user_id)
        if not identity.user_id.startswith("apikey:")
        else None,
    )
    await session.commit()
    return folder


async def rename_folder(session: AsyncSession, folder: AssetFolder, name: str) -> AssetFolder:
    name = (name or "").strip()
    if not name:
        raise ValidationError("文件夹名不能为空")
    repo = AssetFolderRepo(session)
    sibling = await repo.find_sibling_name(folder.space_id, folder.parent_id, name)
    if sibling is not None and sibling.id != folder.id:
        raise ValidationError("同级目录下已存在同名文件夹", name=name)
    folder.name = name
    await session.commit()
    return folder


async def move_folder(
    session: AsyncSession, folder: AssetFolder, parent_id: uuid.UUID | None
) -> AssetFolder:
    """移动文件夹（同空间内）：防环（目标不得为自身/后代）+ 同父重名校验。"""
    repo = AssetFolderRepo(session)
    if parent_id is not None:
        target = await validate_folder_in_space(session, folder.space_id, parent_id)
        # descendant_ids 含自身：同时覆盖「移到自己内部」与「移到后代」
        if target is not None and target in await repo.descendant_ids(
            folder.id, space_id=folder.space_id
        ):
            raise ValidationError("不能把文件夹移动到它自身或它的子目录内")
    sibling = await repo.find_sibling_name(folder.space_id, parent_id, folder.name)
    if sibling is not None and sibling.id != folder.id:
        raise ValidationError("目标目录下已存在同名文件夹", name=folder.name)
    folder.parent_id = parent_id
    await session.commit()
    return folder


async def delete_folder_cascade(session: AsyncSession, *, milvus, folder: AssetFolder) -> int:
    """删除文件夹：级联软删后代文件夹 + 其中全部资产（配额扣减 + 向量/图谱清理）。

    返回删除的资产数。与单资产删除同语义（PG/AGE 同事务级联，Milvus 事后跟进）。
    """
    from loomvec.core.graph.age import AgeStore

    repo = AssetFolderRepo(session)
    folder_ids = await repo.descendant_ids(folder.id, space_id=folder.space_id)
    assets = await AssetRepo(session).list_in_folders(folder_ids)

    now = datetime.now(UTC)
    try:
        age = AgeStore()
    except Exception:  # AGE 不可用不阻断删除（图可由主表重建）
        age = None

    for asset in assets:
        asset.deleted_at = now
        asset.status = AssetStatus.FAILED
        asset.status_reason = "deleted"
        await apply_asset_removed(session, space_id=asset.space_id, size_bytes=asset.size_bytes)
        if age is not None:
            try:
                # SAVEPOINT 内清理：AGE 异常只回滚保存点，主事务（软删+配额）不受牵连
                async with session.begin_nested():
                    touched = await age.delete_asset_edges(session, str(asset.space_id), asset.id)
                    await age.delete_orphan_nodes(session, str(asset.space_id), touched)
            except Exception as e:
                logger.warning(
                    "folder_delete_graph_cleanup_failed",
                    asset_id=str(asset.id),
                    error=str(e),
                )

    for fid in folder_ids:
        row = await session.get(AssetFolder, fid)
        if row is not None and row.deleted_at is None:
            row.deleted_at = now
    await session.commit()

    for asset in assets:
        await milvus.async_delete_asset_units(asset.id)
    return len(assets)


async def copy_asset(
    session: AsyncSession,
    *,
    identity: Identity,
    source: Asset,
    target_space: Space,
    target_folder_id: uuid.UUID | None,
) -> Asset:
    """复制资产：新资产行共享原始对象（storage_key/inline_text），入队完整管线。

    配额按新资产计量（逻辑容量翻倍，物理对象共享——资产删除从不物理清对象）。
    标签与派生物一并复制，缩略图立即可用；语义单元由管线重建（幂等缓存命中）。
    单资产复制入口：复制 + 提交（调用方在返回后入队管线任务）。
    """
    await validate_folder_in_space(session, target_space.id, target_folder_id)
    copy = await _replicate_asset(
        session,
        identity=identity,
        source=source,
        target_space=target_space,
        target_folder_id=target_folder_id,
    )
    await session.commit()
    return copy


async def _replicate_asset(
    session: AsyncSession,
    *,
    identity: Identity,
    source: Asset,
    target_space: Space,
    target_folder_id: uuid.UUID | None,
) -> Asset:
    """复制资产行（不提交）：配额校验/计量与 version/rendition/tag 复制在当前事务内。"""
    await check_upload_quota(session, space=target_space, incoming_bytes=source.size_bytes)

    tenant_uuid = target_space.tenant_id
    copy = Asset(
        tenant_id=tenant_uuid,
        space_id=target_space.id,
        folder_id=target_folder_id,
        name=source.name,
        mime_type=source.mime_type,
        ext=source.ext,
        size_bytes=source.size_bytes,
        storage_key=source.storage_key,
        checksum=source.checksum,
        status=AssetStatus.PENDING,
        created_by=uuid.UUID(identity.user_id)
        if not identity.user_id.startswith("apikey:")
        else None,
        asset_meta=dict(source.asset_meta or {}),
        category_id=source.category_id,
        user_meta=dict(source.user_meta or {}),
    )
    session.add(copy)
    await session.flush()

    version = await AssetVersionRepo(session).latest_for(source.id)
    if version is not None:
        new_version = AssetVersion(
            asset_id=copy.id,
            tenant_id=tenant_uuid,
            version=1,
            checksum=version.checksum,
            storage_key=version.storage_key,
            mime_type=version.mime_type,
            size_bytes=version.size_bytes,
            inline_text=version.inline_text,
            chunk_cache_key=version.chunk_cache_key,
            version_meta=dict(version.version_meta or {}),
        )
        session.add(new_version)
        await session.flush()
        rendition_repo = AssetRenditionRepo(session)
        for r in await rendition_repo.for_asset(source.id):
            session.add(
                AssetRendition(
                    asset_id=copy.id,
                    version_id=new_version.id,
                    kind=r.kind,
                    storage_key=r.storage_key,
                    mime_type=r.mime_type,
                    width=r.width,
                    height=r.height,
                    size_bytes=r.size_bytes,
                )
            )
    for tag in await TagRepo(session).tags_for_asset(source.id):
        session.add(AssetTag(asset_id=copy.id, tag_id=tag.id, tenant_id=tenant_uuid))

    await apply_asset_added(
        session,
        space_id=target_space.id,
        tenant_id=target_space.tenant_id,
        size_bytes=source.size_bytes,
    )
    return copy


async def copy_folder_recursive(
    session: AsyncSession,
    *,
    identity: Identity,
    source: AssetFolder,
    target_parent_id: uuid.UUID | None,
    enqueue,
) -> AssetFolder:
    """递归复制文件夹树：目标父下同名活文件夹则并入（合并语义），资产同名共存。

    例外：并入目标不能是源自身（同级复制场景），此时创建「<名> 副本」。
    enqueue(asset_id) 由路由层注入；**提交后**统一派发（worker 才能读到已提交的行），
    任一资产复制失败（如配额超限）则整个事务回滚，不产生半成品。
    """
    await validate_folder_in_space(session, source.space_id, target_parent_id)
    repo = AssetFolderRepo(session)
    asset_repo = AssetRepo(session)
    space = await session.get(Space, source.space_id)
    if space is None:
        raise NotFoundError(resource="space", id=str(source.space_id))

    async def _ensure_child(
        parent_id: uuid.UUID | None, name: str, avoid_id: uuid.UUID
    ) -> AssetFolder:
        """目标父下取同名活文件夹；命中源自身或无同名则新建（副本后缀避让）。"""
        existing = await repo.find_sibling_name(source.space_id, parent_id, name)
        if existing is not None and existing.id != avoid_id:
            return existing
        if existing is not None and existing.id == avoid_id:
            suffix = " 副本"
            while True:
                candidate = await repo.find_sibling_name(
                    source.space_id, parent_id, f"{name}{suffix}"
                )
                if candidate is None or candidate.id == avoid_id:
                    name = f"{name}{suffix}"
                    break
                suffix = f"{suffix} 副本" if not suffix.endswith(" 副本") else f"{suffix} 2"
        return await repo.create(
            tenant_id=space.tenant_id,
            space_id=source.space_id,
            parent_id=parent_id,
            name=name,
            created_by=uuid.UUID(identity.user_id)
            if not identity.user_id.startswith("apikey:")
            else None,
        )

    # BFS：源文件夹映射到副本；同一事务内复制，提交后统一派发管线任务
    created: list[AssetFolder] = []
    copied_asset_ids: list[str] = []
    queue: list[tuple[AssetFolder, uuid.UUID | None]] = [(source, target_parent_id)]
    while queue:
        src, dest_parent = queue.pop(0)
        dest = await _ensure_child(dest_parent, src.name, avoid_id=src.id)
        created.append(dest)
        for asset in await asset_repo.list_in_folders([src.id]):
            copy = await _replicate_asset(
                session,
                identity=identity,
                source=asset,
                target_space=space,
                target_folder_id=dest.id,
            )
            copied_asset_ids.append(str(copy.id))
        for child in await repo.children_of(src.id, src.space_id):
            queue.append((child, dest.id))
    await session.commit()
    for asset_id in copied_asset_ids:
        enqueue(asset_id)
    # 返回根副本（并入已有文件夹时为既有行）
    return created[0] if created else source
