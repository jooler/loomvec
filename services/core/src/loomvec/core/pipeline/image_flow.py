"""P2-WRK-01 图片管线分支：parse / chunk / embed 三个步骤的图片实现。

与文档流（steps.py 的 MinerU/LLM 分片）并列；index 步骤两类型共用
（rows 结构见 steps.IndexStep）。编排由 Steps 依 mime 分支调用。

- parse：EXIF 提取（可选）→ 缩略图派生物（无图像后端时跳过不阻断）→ VLM caption（可选开关）；
- chunk：整图单单元（caption + 关键 EXIF 文本），无 LLM 分片，不做 marker 缓存；
- embed：clip_dense 通道图文向量（BGE-VL / 百炼 multimodal-embedding 类）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import (
    Asset,
    AssetRendition,
    AssetVersion,
    ChunkMethod,
    RenditionKind,
    SemanticUnit,
    UnitType,
)
from loomvec.core.errors import ValidationError
from loomvec.core.imaging import THUMBNAIL_MIME, extract_exif, make_thumbnail
from loomvec.core.pipeline.base import (
    IMAGE_UNIT_MODEL_PREFIX,
    PipelineDeps,
    image_unit_text,
)
from loomvec.core.storage_keys import embed_vectors_key, thumbnail_key

logger = structlog.get_logger("loomvec.pipeline.image")


async def parse_image(
    session: AsyncSession,
    deps: PipelineDeps,
    asset: Asset,
    version: AssetVersion,
    data: bytes,
    mime: str,
) -> dict:
    """图片解析：checksum → EXIF → 缩略图派生物 → VLM caption → version_meta。"""
    checksum = hashlib.sha256(data).hexdigest()
    version.checksum = version.checksum or checksum
    asset.checksum = asset.checksum or checksum

    exif = await extract_exif(data, whitelist=deps.settings.image.exif_whitelist)
    thumb_key, thumb_dims = await _make_thumbnail_rendition(session, deps, asset, version, data)
    caption = await image_caption(deps, asset, data)

    version.version_meta = {
        **version.version_meta,
        "parse": {
            "parser": "image",
            "parser_version": deps.settings.pipeline.parser_version,
            "md_key": None,
            "layout_key": None,
            "page_count": 1,
            "mime": mime,
            "thumb_key": thumb_key,
            "thumb_width": thumb_dims[0] if thumb_dims else None,
            "thumb_height": thumb_dims[1] if thumb_dims else None,
            "caption": caption,
            "exif": exif,
        },
    }
    asset.asset_meta = {
        **asset.asset_meta,
        "page_count": 1,
        "caption": caption,
        **{k: v for k, v in exif.items() if k in ("ImageWidth", "ImageHeight")},
    }
    await session.flush()
    return {"parser": "image", "caption": bool(caption), "exif_keys": len(exif)}


async def _make_thumbnail_rendition(
    session: AsyncSession, deps: PipelineDeps, asset: Asset, version: AssetVersion, data: bytes
) -> tuple[str | None, tuple[int, int] | None]:
    """缩略图：落对象存储 + upsert AssetRendition；无图像后端时返回 (None, None)。"""
    thumb = await make_thumbnail(
        data,
        max_width=deps.settings.image.thumbnail_max_width,
        max_height=deps.settings.image.thumbnail_max_height,
        quality=deps.settings.image.thumbnail_quality,
    )
    if thumb is None:
        logger.warning("thumbnail_skipped", asset_id=str(asset.id), reason="no_image_backend")
        return None, None

    payload, width, height = thumb
    key = thumbnail_key(asset.id, version.version)
    await deps.storage.put_object(
        deps.settings.storage.bucket_derived, key, payload, content_type=THUMBNAIL_MIME
    )
    existing = (
        await session.execute(
            select(AssetRendition).where(
                AssetRendition.asset_id == asset.id,
                AssetRendition.version_id == version.id,
                AssetRendition.kind == RenditionKind.THUMBNAIL,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            AssetRendition(
                tenant_id=asset.tenant_id,
                asset_id=asset.id,
                version_id=version.id,
                kind=RenditionKind.THUMBNAIL,
                storage_key=key,
                mime_type=THUMBNAIL_MIME,
                width=width,
                height=height,
                size_bytes=len(payload),
            )
        )
    else:
        existing.storage_key = key
        existing.width = width
        existing.height = height
        existing.size_bytes = len(payload)
    return key, (width, height)


async def image_caption(deps: PipelineDeps, asset: Asset, data: bytes) -> str | None:
    """VLM caption（image.caption_enabled 开关；失败降级 None 不阻断管线）。"""
    if not deps.settings.image.caption_enabled:
        return None
    try:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        # mock 供方经 LOOMVEC_TASK 标记路由；真实 VLM 读自然语言指令
                        "text": (
                            f"[LOOMVEC_TASK=image_caption][LOOMVEC_IMAGE_NAME={asset.name}]"
                            "用一句中文描述这张图片的主要内容。"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:"
                            + asset.mime_type
                            + ";base64,"
                            + base64.b64encode(data).decode()
                        },
                    },
                ],
            }
        ]
        raw = (await deps.ai.vlm(messages)).strip()
        # mock 走 complete() 返回 JSON；真实 VLM 返回纯文本
        if raw.startswith("{"):
            return str(json.loads(raw).get("caption") or raw)
        return raw or None
    except Exception as e:
        logger.warning("image_caption_failed", asset_id=str(asset.id), error=str(e))
        return None


async def chunk_image(session: AsyncSession, asset: Asset, version: AssetVersion) -> dict:
    """图片分片：整图单单元，替换旧单元（重跑幂等）。"""
    parse_meta: dict[str, Any] = version.version_meta.get("parse") or {}
    content = image_unit_text(asset, parse_meta)

    old_units = (
        (await session.execute(select(SemanticUnit).where(SemanticUnit.version_id == version.id)))
        .scalars()
        .all()
    )
    for u in old_units:
        await session.delete(u)

    session.add(
        SemanticUnit(
            asset_id=asset.id,
            version_id=version.id,
            space_id=asset.space_id,
            tenant_id=asset.tenant_id,
            unit_type=UnitType.IMAGE,
            title=asset.name,
            content=content,
            keywords=[],
            locator={},
            chunk_method=ChunkMethod.STRUCTURAL_FALLBACK,
            order_index=0,
            char_count=len(content),
        )
    )
    await session.flush()

    version.chunk_cache_key = None  # 图片内容随重解析变化，不做 marker 缓存
    version.version_meta = {
        **version.version_meta,
        "chunk": {"units": 1, "fallback_batches": 0, "unit_type": "image"},
    }
    return {"units": 1, "unit_type": "image", "replaced": len(old_units)}


async def embed_image(
    session: AsyncSession, deps: PipelineDeps, asset: Asset, version: AssetVersion
) -> dict:
    """图片嵌入：clip_dense 通道，向量缓存与文档流同布局（embed/{asset}/v{n}/vectors.json）。"""
    clip_model = "mock" if deps.settings.ai.mock else (deps.settings.ai.clip.model or "unknown")
    model_version = IMAGE_UNIT_MODEL_PREFIX + clip_model
    unit = (
        (await session.execute(select(SemanticUnit).where(SemanticUnit.version_id == version.id)))
        .scalars()
        .first()
    )
    if unit is None:
        raise ValidationError("无可嵌入单元（请先跑 chunk 步骤）", asset_id=str(asset.id))
    if not version.storage_key:
        raise ValidationError("图片资产缺少原始对象", asset_id=str(asset.id))

    data = await deps.storage.get_object(deps.settings.storage.bucket_raw, version.storage_key)
    caption = (version.version_meta.get("parse") or {}).get("caption")
    vec = (await deps.ai.clip_embed_images([data], captions=[caption]))[0]
    unit.embed_model_version = model_version

    vectors_key = embed_vectors_key(asset.id, version.version)
    await deps.storage.put_object(
        deps.settings.storage.bucket_derived,
        vectors_key,
        json.dumps({str(unit.id): vec}).encode(),
        content_type="application/json",
    )
    version.version_meta = {
        **version.version_meta,
        "embed": {
            "model_version": model_version,
            "vectors_key": vectors_key,
            "count": 1,
            "channel": "clip_dense",
        },
    }
    await session.flush()
    return {"embedded": 1, "model_version": model_version, "channel": "clip_dense"}
