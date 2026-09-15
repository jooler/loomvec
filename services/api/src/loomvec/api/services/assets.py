"""P1/P2 资产服务：上传校验/登记/级联删除/单步重跑 + 配额闸门/审核/编辑。

路由层保持薄（参数解析 + 响应序列化）；此服务可被路由、测试与未来
批处理脚本复用。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.core.config import Settings
from loomvec.core.constants import QUEUE_PIPELINE, QUEUE_PIPELINE_HIGH
from loomvec.core.db.models import (
    Asset,
    AssetStatus,
    AssetVersion,
    JobStatus,
    JobType,
    Space,
    SpaceRole,
)
from loomvec.core.db.pagination import Cursor
from loomvec.core.db.repos import (
    AssetRepo,
    CategoryRepo,
    MetadataFieldRepo,
    ProcessingJobRepo,
    TagRepo,
)
from loomvec.core.errors import NotFoundError, ValidationError
from loomvec.core.pipeline.mime import EXT_TO_MIME, file_ext, is_image_mime
from loomvec.core.pipeline.steps import PIPELINE_STEPS
from loomvec.core.quota import apply_asset_added, apply_asset_removed, check_upload_quota
from loomvec.core.storage import ObjectStorage
from loomvec.core.storage_keys import raw_upload_key
from loomvec.core.taxonomy import normalize_tag_name, validate_metadata


class UploadPolicy:
    """上传策略（P4 动态配置）：静态 Settings 回落 + system_config 覆盖。"""

    def __init__(
        self, settings: Settings, *, allowed_extensions, max_size_bytes, presign_expires_seconds
    ):
        self.allowed_extensions = allowed_extensions
        self.max_size_bytes = max_size_bytes
        self.presign_expires_seconds = presign_expires_seconds


async def upload_policy(session: AsyncSession | None, settings: Settings) -> UploadPolicy:
    """读上传策略：动态配置（即时生效）优先，未配置回落进程 Settings。"""

    # 来源唯一：应用参数回落 config/loomvec.json；DB 动态值为显式管理覆盖
    from loomvec.core.config import get_app_config

    u = get_app_config().upload
    if session is None:
        return UploadPolicy(
            settings,
            allowed_extensions=u.allowed_extensions,
            max_size_bytes=u.max_size_bytes,
            presign_expires_seconds=u.presign_expires_seconds,
        )
    from loomvec.api.services.admin_settings import effective_value

    max_size = await effective_value(session, "upload.max_size_bytes")
    exts = await effective_value(session, "upload.allowed_extensions")
    presign = await effective_value(session, "upload.presign_expires_seconds")
    return UploadPolicy(
        settings,
        allowed_extensions=exts if exts else u.allowed_extensions,
        max_size_bytes=max_size if max_size else u.max_size_bytes,
        presign_expires_seconds=presign if presign else u.presign_expires_seconds,
    )


def validate_upload(policy: UploadPolicy, filename: str, size: int) -> str:
    """MIME/大小白名单双重校验，返回规范化扩展名。"""
    ext = file_ext(filename)
    if ext not in policy.allowed_extensions:
        raise ValidationError(
            f"不支持的文件类型：{ext or '(无扩展名)'}",
            allowed=policy.allowed_extensions,
        )
    if size > policy.max_size_bytes:
        raise ValidationError(f"文件超过大小上限 {policy.max_size_bytes} 字节", size=size)
    return ext


async def presign_upload(
    session: AsyncSession | None,
    settings: Settings,
    storage: ObjectStorage,
    body,
    space_id: uuid.UUID,
) -> tuple[str, str, int]:
    """生成直传预签名；返回 (key, upload_url, expires_in)。"""
    policy = await upload_policy(session, settings)
    ext = validate_upload(policy, body.filename, body.size)
    key = raw_upload_key(space_id, uuid.uuid4().hex, body.filename)
    url = storage.presign_put(
        settings.storage.bucket_raw,
        key,
        expires_in=policy.presign_expires_seconds,
        content_type=body.content_type or EXT_TO_MIME.get(ext),
    )
    return key, url, policy.presign_expires_seconds


def enqueue_pipeline(celery, asset_id: str, *, queue: str = QUEUE_PIPELINE) -> None:
    celery.send_task("pipeline.process_asset", args=[asset_id, "parse"], queue=queue)


def _new_file_asset(session: AsyncSession, identity: Identity, **fields) -> Asset:
    tenant_uuid = uuid.UUID(identity.tenant_id) if identity.tenant_id else None
    asset = Asset(
        tenant_id=tenant_uuid,
        status=AssetStatus.PENDING,
        created_by=(
            uuid.UUID(identity.user_id) if not identity.user_id.startswith("apikey:") else None
        ),
        **fields,
    )
    session.add(asset)
    return asset


async def register_file_asset(
    session: AsyncSession,
    *,
    identity: Identity,
    settings: Settings,
    storage: ObjectStorage,
    key: str,
    filename: str,
    size: int,
    content_type: str | None,
    space: Space,
    folder_id: uuid.UUID | None = None,
) -> Asset:
    """登记已直传的文件资产：对象存在校验 → 两级配额闸门 → 计量 + v1。"""
    from loomvec.api.services.folders import validate_folder_in_space

    policy = await upload_policy(session, settings)
    ext = validate_upload(policy, filename, size)
    info = await storage.head_object(settings.storage.bucket_raw, key)
    if info is None:
        raise ValidationError("对象尚未上传或已过期", key=key)
    size = info.get("ContentLength") or size

    folder_id = await validate_folder_in_space(session, space.id, folder_id)
    await check_upload_quota(session, space=space, incoming_bytes=size)
    asset = _new_file_asset(
        session,
        identity,
        space_id=space.id,
        name=filename,
        mime_type=content_type or EXT_TO_MIME.get(ext, "application/octet-stream"),
        ext=ext,
        size_bytes=size,
        storage_key=key,
        folder_id=folder_id,
    )
    await session.flush()
    session.add(
        AssetVersion(
            asset_id=asset.id,
            tenant_id=asset.tenant_id,
            version=1,
            storage_key=key,
            mime_type=asset.mime_type,
            size_bytes=size,
        )
    )
    await apply_asset_added(session, space_id=space.id, tenant_id=space.tenant_id, size_bytes=size)
    await session.commit()
    return asset


async def register_text_asset(
    session: AsyncSession,
    *,
    identity: Identity,
    settings: Settings,
    name: str,
    content: str,
    space: Space,
    folder_id: uuid.UUID | None = None,
) -> Asset:
    """文本/Markdown 直接摄取（inline_text，无需对象存储）。"""
    from loomvec.api.services.folders import validate_folder_in_space

    size = len(content.encode())
    policy = await upload_policy(session, settings)
    if size > policy.max_size_bytes:
        raise ValidationError("文本超过大小上限", size=size)
    if "." not in name:
        name = f"{name}.md"
    folder_id = await validate_folder_in_space(session, space.id, folder_id)
    await check_upload_quota(session, space=space, incoming_bytes=size)

    asset = _new_file_asset(
        session,
        identity,
        space_id=space.id,
        name=name,
        mime_type=EXT_TO_MIME[".md"],
        ext=".md",
        size_bytes=size,
        folder_id=folder_id,
    )
    await session.flush()
    session.add(
        AssetVersion(
            asset_id=asset.id,
            tenant_id=asset.tenant_id,
            version=1,
            mime_type=asset.mime_type,
            size_bytes=asset.size_bytes,
            inline_text=content,
        )
    )
    await apply_asset_added(session, space_id=space.id, tenant_id=space.tenant_id, size_bytes=size)
    await session.commit()
    return asset


async def check_duplicate(session: AsyncSession, *, space: Space, checksum: str) -> list[Asset]:
    """去重提示：同空间同 checksum 的现存资产（不阻断上传）。"""
    return await AssetRepo(session).find_by_checksum(space.id, checksum)


async def update_asset(
    session: AsyncSession,
    *,
    asset: Asset,
    name: str | None = None,
    tag_names: list[str] | None = None,
    category_id: uuid.UUID | None = None,
    unset_category: bool = False,
    user_meta: dict | None = None,
    folder_id: uuid.UUID | None = None,
    unset_folder: bool = False,
) -> Asset:
    """编辑资产：名称 / 标签（get_or_create）/ 分类（空间内校验）/ 元数据（schema 校验）。

    Finder 移动：folder_id / unset_folder（目标文件夹须同空间；空间内移动不改配额）。
    """
    if name is not None:
        if not name.strip():
            raise ValidationError("资产名不能为空")
        asset.name = name.strip()

    if unset_folder or folder_id is not None:
        from loomvec.api.services.folders import validate_folder_in_space

        if unset_folder:
            asset.folder_id = None
        else:
            asset.folder_id = await validate_folder_in_space(session, asset.space_id, folder_id)

    if unset_category or category_id is not None:
        if unset_category:
            asset.category_id = None
        else:
            category = await CategoryRepo(session).get(category_id)
            if category is None or category.space_id != asset.space_id:
                raise ValidationError("分类不属于该空间", category_id=str(category_id))
            asset.category_id = category.id

    if tag_names is not None:
        tag_repo = TagRepo(session)
        tags = []
        for raw in tag_names:
            norm = normalize_tag_name(raw)
            if not norm:
                raise ValidationError("标签名不能为空")
            tags.append(
                await tag_repo.get_or_create(
                    tenant_id=asset.tenant_id, name=norm, created_by=asset.created_by
                )
            )
        await tag_repo.set_asset_tags(asset, tags)

    if user_meta is not None:
        fields = await MetadataFieldRepo(session).list_for_space(asset.space_id)
        asset.user_meta = validate_metadata(fields, user_meta)

    await session.commit()
    return asset


async def delete_asset_cascade(session: AsyncSession, *, milvus, asset_id: uuid.UUID) -> None:
    """软删资产 + 级联清理：语义单元行删除 + AGE 图边/悬空节点 + Milvus 向量
    清理 + 配额扣减（P3-QA-01：PG/AGE 同库同事务级联，Milvus 跟进）。"""
    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))
    asset.deleted_at = datetime.now(UTC)
    asset.status = AssetStatus.FAILED  # 删除后不再参与检索（status 仅展示语义）
    asset.status_reason = "deleted"
    await apply_asset_removed(session, space_id=asset.space_id, size_bytes=asset.size_bytes)
    # 图谱级联（与资产同库同事务）：删该资产的边 + 清理无边的触及节点
    from loomvec.core.graph.age import AgeStore

    try:
        age = AgeStore()
        # SAVEPOINT 内清理：AGE 异常只回滚保存点，主事务（软删+配额）不受牵连
        async with session.begin_nested():
            touched = await age.delete_asset_edges(session, str(asset.space_id), asset_id)
            await age.delete_orphan_nodes(session, str(asset.space_id), touched)
    except Exception as e:  # AGE 不可用不阻断删除（图可由主表重建）
        import structlog

        structlog.get_logger("loomvec.api.assets").warning(
            "asset_delete_graph_cleanup_failed", asset_id=str(asset_id), error=str(e)
        )
    await session.commit()

    await milvus.async_delete_asset_units(asset_id)


async def retry_asset(
    session: AsyncSession, *, celery, asset_id: uuid.UUID, step: str | None
) -> Asset:
    """单步重跑：默认从首个失败步骤起跑（退出标准：失败可见原因 + 单步重试）。"""
    asset = await AssetRepo(session).get_live(asset_id)
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))

    if step is None:
        jobs = await ProcessingJobRepo(session).recent_for_asset(asset_id)
        failed = next((j for j in reversed(jobs) if j.status == JobStatus.FAILED), None)
        step = failed.job_type.value if failed else "parse"
    if step not in PIPELINE_STEPS:
        raise ValidationError(f"未知步骤：{step}", steps=PIPELINE_STEPS)

    await ProcessingJobRepo(session).create(
        asset_id=asset_id,
        tenant_id=asset.tenant_id,
        job_type=JobType(step),
        status=JobStatus.PENDING,
    )
    asset.status = AssetStatus.PENDING
    asset.status_reason = None
    await session.commit()

    celery.send_task(
        "pipeline.process_asset", args=[str(asset_id), step], queue=QUEUE_PIPELINE_HIGH
    )
    return asset


def asset_out(asset: Asset, *, my_role: SpaceRole | str | None = None) -> dict:
    """资产 → 响应 dict（AssetOut 模型字段）。"""
    return {
        "id": asset.id,
        "space_id": asset.space_id,
        "name": asset.name,
        "mime_type": asset.mime_type,
        "ext": asset.ext,
        "size_bytes": asset.size_bytes,
        "status": asset.status.value,
        "status_reason": asset.status_reason,
        "review_status": asset.review_status.value if asset.review_status else None,
        "is_image": is_image_mime(asset.mime_type),
        "page_count": asset.asset_meta.get("page_count"),
        "checksum": asset.checksum,
        "folder_id": asset.folder_id,
        "created_at": asset.created_at,
        "created_by": asset.created_by,
    }


def encode_cursor(asset: Asset) -> str:
    return Cursor(created_at=asset.created_at, id=asset.id).encode()
