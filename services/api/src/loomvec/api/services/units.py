"""chunk（语义单元）管理服务：列表 / 手动新增 / 编辑 / 删除。

借鉴 RAGFlow 的 chunk 管理语义：新增与编辑即时向量化（同步调嵌入 +
写向量缓存 + upsert Milvus 行），删除即时清向量——不依赖管线重跑。
图片资产的单单元由图片流（caption+EXIF+clip）维护，不开放增改。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.ai import AiGateway
from loomvec.core.config import Settings
from loomvec.core.db.models import Asset, AssetVersion, ChunkMethod, SemanticUnit, UnitType
from loomvec.core.db.repos import AssetVersionRepo
from loomvec.core.errors import NotFoundError, ValidationError
from loomvec.core.pipeline.chunking import strip_html
from loomvec.core.pipeline.mime import is_image_mime
from loomvec.core.storage import ObjectStorage
from loomvec.core.storage_keys import embed_vectors_key


def _model_version(settings: Settings) -> str:
    return "mock" if settings.ai.mock else (settings.ai.embedding.model or "unknown")


def embed_input(unit: SemanticUnit, max_chars: int) -> str:
    """嵌入输入（与管线 EmbedStep._embed_text 同构）：标题 + 去表格 HTML 正文。"""
    body = strip_html(unit.content) if unit.unit_type == UnitType.TABLE else unit.content
    title = f"{unit.title}\n" if unit.title else ""
    return (title + body)[:max_chars]


async def _read_vectors_cache(
    storage: ObjectStorage, settings: Settings, version: AssetVersion
) -> tuple[dict[str, Any], dict[str, Any], str, bool]:
    """读向量缓存：返回 (units_map, frames_map, cache_key, existed)；缺失视为空。"""
    vectors_key = embed_vectors_key(version.asset_id, version.version)
    try:
        raw = await storage.get_object(settings.storage.bucket_derived, vectors_key)
        cache = json.loads(raw.decode())
    except Exception:
        return {}, {}, vectors_key, False
    if "units" in cache and isinstance(cache.get("units"), dict):
        return dict(cache["units"]), dict(cache.get("frames") or {}), vectors_key, True
    return dict(cache), {}, vectors_key, True


async def _write_vectors_cache(
    storage: ObjectStorage,
    settings: Settings,
    version: AssetVersion,
    units_map: dict[str, Any],
    frames_map: dict[str, Any],
    vectors_key: str,
) -> None:
    """整体回写缓存：文档为扁平 {unit_id: vec}；媒体为嵌套 {"units", "frames"}。"""
    payload: dict[str, Any] = dict(units_map)
    if frames_map:
        payload = {"units": units_map, "frames": frames_map}
    await storage.put_object(
        settings.storage.bucket_derived,
        vectors_key,
        json.dumps(payload, ensure_ascii=False).encode(),
        content_type="application/json",
    )


def _index_row(
    milvus,
    settings: Settings,
    unit: SemanticUnit,
    vector: list[float],
    frame_vec: list[float] | None,
) -> dict[str, Any]:
    """单单元向量行（结构与管线 IndexStep 一致：text 双通道字段均非空，缺侧补零）。"""
    return {
        "id": str(unit.id),
        "space_id": str(unit.space_id),
        "tenant_id": str(unit.tenant_id) if unit.tenant_id else "",
        "asset_id": str(unit.asset_id),
        "unit_type": unit.unit_type.value,
        "model_version": unit.embed_model_version,
        "text": (unit.content if unit.unit_type == UnitType.TEXT else strip_html(unit.content))[
            : settings.pipeline.index_text_max_chars
        ],
        "text_dense": vector,
        "clip_dense": frame_vec or [0.0] * milvus.clip_dim,
    }


async def _sync_embed(
    session: AsyncSession,
    *,
    ai: AiGateway,
    milvus,
    storage: ObjectStorage,
    settings: Settings,
    version: AssetVersion,
    unit: SemanticUnit,
) -> None:
    """编辑/新增后同步向量化：嵌入 → 版本标记 → 缓存合并 → 索引 upsert。

    缓存整体读改写，保证 index 步骤重跑（先清资产全部向量再按缓存重插）不丢本单元；
    媒体单元按 locator.frame 保留既有场景帧向量（clip 通道）。
    """
    [vector] = await ai.embed([embed_input(unit, settings.pipeline.embed_text_max_chars)])
    unit.embed_model_version = _model_version(settings)
    await session.flush()
    units_map, frames_map, vectors_key, _existed = await _read_vectors_cache(
        storage, settings, version
    )
    frame_vec = frames_map.get((unit.locator or {}).get("frame") or "")
    units_map[str(unit.id)] = vector
    await _write_vectors_cache(storage, settings, version, units_map, frames_map, vectors_key)
    await milvus.async_upsert_units([_index_row(milvus, settings, unit, vector, frame_vec)])


def _require_mutable(asset: Asset) -> None:
    if is_image_mime(asset.mime_type):
        raise ValidationError("图片资产的语义单元由管线维护，不支持手动增改")


async def list_units(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
    unit_type: str | None = None,
    q: str | None = None,
) -> tuple[list[SemanticUnit], int]:
    """chunk 列表（order_index 升序）+ 总数；支持类型过滤与内容/标题关键词。"""
    conds = [SemanticUnit.asset_id == asset_id]
    if unit_type:
        conds.append(SemanticUnit.unit_type == UnitType(unit_type))
    if q:
        like = f"%{q}%"
        conds.append(or_(SemanticUnit.content.ilike(like), SemanticUnit.title.ilike(like)))
    total = (
        await session.execute(select(func.count()).select_from(SemanticUnit).where(*conds))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(SemanticUnit)
                .where(*conds)
                .order_by(SemanticUnit.order_index.asc(), SemanticUnit.created_at.asc())
                .offset(offset)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def get_unit(
    session: AsyncSession, *, asset_id: uuid.UUID, unit_id: uuid.UUID
) -> SemanticUnit:
    unit = (
        await session.execute(
            select(SemanticUnit).where(
                SemanticUnit.id == unit_id, SemanticUnit.asset_id == asset_id
            )
        )
    ).scalar_one_or_none()
    if unit is None:
        raise NotFoundError(resource="semantic_unit", id=str(unit_id))
    return unit


async def create_unit(
    session: AsyncSession,
    *,
    ai: AiGateway,
    milvus,
    storage: ObjectStorage,
    settings: Settings,
    asset: Asset,
    content: str,
    title: str | None,
    keywords: list[str] | None,
    unit_type: str,
) -> SemanticUnit:
    """手动补片：追加为最后一个 chunk，即时向量化入索引。"""
    _require_mutable(asset)
    version = await AssetVersionRepo(session).latest_for(asset.id)
    if version is None:
        raise NotFoundError(resource="asset_version", id=str(asset.id))
    if unit_type not in (UnitType.TEXT.value, UnitType.TABLE.value):
        raise ValidationError("unit_type 仅支持 text / table")
    order_index = (
        await session.execute(
            select(func.coalesce(func.max(SemanticUnit.order_index), -1)).where(
                SemanticUnit.version_id == version.id
            )
        )
    ).scalar_one() + 1
    unit = SemanticUnit(
        asset_id=asset.id,
        version_id=version.id,
        space_id=asset.space_id,
        tenant_id=asset.tenant_id,
        unit_type=UnitType(unit_type),
        title=title,
        content=content,
        keywords=keywords or [],
        locator={},
        chunk_method=ChunkMethod.MANUAL,
        order_index=order_index,
        char_count=len(content),
    )
    session.add(unit)
    await session.flush()
    await _sync_embed(
        session,
        ai=ai,
        milvus=milvus,
        storage=storage,
        settings=settings,
        version=version,
        unit=unit,
    )
    await session.commit()
    return unit


async def update_unit(
    session: AsyncSession,
    *,
    ai: AiGateway,
    milvus,
    storage: ObjectStorage,
    settings: Settings,
    asset: Asset,
    unit: SemanticUnit,
    title: str | None,
    content: str | None,
    keywords: list[str] | None,
) -> SemanticUnit:
    """编辑 chunk：标题/内容变更即重新向量化（RAGFlow 语义）；keywords 仅元数据。"""
    _require_mutable(asset)
    version = await AssetVersionRepo(session).get(unit.version_id)
    if version is None:
        raise NotFoundError(resource="asset_version", id=str(unit.version_id))
    content_changed = content is not None and content != unit.content
    title_changed = title is not None and title != unit.title
    if content is not None:
        unit.content = content
        unit.char_count = len(content)
    if title is not None:
        unit.title = title
    if keywords is not None:
        unit.keywords = keywords
    await session.flush()
    if content_changed or title_changed:
        await _sync_embed(
            session,
            ai=ai,
            milvus=milvus,
            storage=storage,
            settings=settings,
            version=version,
            unit=unit,
        )
    await session.commit()
    return unit


async def delete_units(
    session: AsyncSession,
    *,
    milvus,
    storage: ObjectStorage,
    settings: Settings,
    asset: Asset,
    unit_ids: list[uuid.UUID],
) -> int:
    """删除 chunk（单/批）：PG 行 + 向量缓存条目 + Milvus 向量即时清理。"""
    version = await AssetVersionRepo(session).latest_for(asset.id)
    if version is None:
        raise NotFoundError(resource="asset_version", id=str(asset.id))
    units = (
        (
            await session.execute(
                select(SemanticUnit).where(
                    SemanticUnit.asset_id == asset.id,
                    SemanticUnit.version_id == version.id,
                    SemanticUnit.id.in_(unit_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    found = {u.id for u in units}
    missing = set(unit_ids) - found
    if missing:
        raise NotFoundError(resource="semantic_unit", id=str(sorted(missing)[0]))
    units_map, frames_map, vectors_key, cache_existed = await _read_vectors_cache(
        storage, settings, version
    )
    for u in units:
        units_map.pop(str(u.id), None)
    if cache_existed:  # 缓存本就不存在时无需落空文件（embed 步骤会整体重写）
        await _write_vectors_cache(storage, settings, version, units_map, frames_map, vectors_key)
    await session.execute(delete(SemanticUnit).where(SemanticUnit.id.in_(found)))
    await session.commit()
    await milvus.async_delete_unit_ids(sorted(found))
    return len(found)


async def latest_version(session: AsyncSession, asset_id: uuid.UUID) -> AssetVersion:
    version = await AssetVersionRepo(session).latest_for(asset_id)
    if version is None:
        raise NotFoundError(resource="asset_version", id=str(asset_id))
    return version


def unit_out(unit: SemanticUnit) -> dict[str, Any]:
    return {
        "id": unit.id,
        "unit_type": unit.unit_type.value,
        "title": unit.title,
        "content": unit.content,
        "keywords": list(unit.keywords or []),
        "locator": unit.locator or {},
        "chunk_method": unit.chunk_method.value,
        "order_index": unit.order_index,
        "char_count": unit.char_count,
        "embed_model_version": unit.embed_model_version,
        "created_at": unit.created_at,
    }
