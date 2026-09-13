"""P3-API-04 播放接口：音视频按需播放（懒转码）、关键帧清单、WebVTT 字幕。

- `GET /assets/{asset_id}/playback`：播放决策。
    native      → 原始文件可直接播放（mp4/webm 白名单或已有转码产物）；
    transcoding → 懒转码进行中（首次"播放"即触发转码任务；进度经 Redis 查询）；
    audio       → 音频直接播放（浏览器对 mp3/wav/m4a 兼容良好）；
  响应带关键帧侧栏数据（parse 产物中的场景帧）与定位参数（time_start）；
- `GET /assets/{asset_id}/subtitles`：转写 → WebVTT（text/vtt，<track> 直接可用）；
- 懒转码触发条件：视频容器不可直放（mov/mkv 等）且 media.transcode_enabled。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_celery,
    get_redis,
    get_session,
    get_storage,
    require_scope,
    service_settings,
)
from loomvec.core.config import Settings
from loomvec.core.constants import QUEUE_PIPELINE_HIGH
from loomvec.core.db.models import (
    Asset,
    AssetVersion,
    RenditionKind,
    SemanticUnit,
)
from loomvec.core.db.repos import AssetRenditionRepo, AssetVersionRepo
from loomvec.core.errors import NotFoundError
from loomvec.core.pipeline.mime import is_audio_mime, is_native_video_mime, is_video_mime
from loomvec.core.storage import ObjectStorage
from loomvec.core.transcode import progress_key

router = APIRouter(prefix="/api/v1", tags=["playback"])


class KeyframeOut(BaseModel):
    time_start: float
    time_end: float | None
    url: str | None


class PlaybackOut(BaseModel):
    asset_id: uuid.UUID
    mode: str  # native / transcoding / audio / unsupported
    mime_type: str
    url: str | None = None
    progress: float | None = None  # transcoding 时 0~1
    duration: float | None = None
    keyframes: list[KeyframeOut] = []


async def _load_media_asset(
    session: AsyncSession, asset_id: uuid.UUID, identity: Identity
) -> tuple[Asset, AssetVersion]:
    from loomvec.api.deps import resolve_space_access
    from loomvec.core.authz import SpaceRole, decide_asset_visibility

    asset = (
        await session.execute(select(Asset).where(Asset.id == asset_id, Asset.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if asset is None:
        raise NotFoundError(resource="asset", id=str(asset_id))
    access = await resolve_space_access(session, identity, asset.space_id, SpaceRole.VIEWER)
    if not decide_asset_visibility(
        review_required=access.space.review_required,
        review_status=asset.review_status,
        role=access.role if access.member else SpaceRole.EDITOR,
    ):
        raise NotFoundError(resource="asset", id=str(asset_id))
    version = await AssetVersionRepo(session).latest_for(asset.id)
    if version is None:
        raise NotFoundError(resource="asset_version", id=str(asset_id))
    return asset, version


def _media_meta(version: AssetVersion) -> dict[str, Any]:
    return (version.version_meta.get("parse") or {}).get("media") or {}


@router.get("/assets/{asset_id}/playback", response_model=PlaybackOut)
async def playback(
    asset_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(service_settings),
    storage: ObjectStorage = Depends(get_storage),
    redis=Depends(get_redis),
    celery=Depends(get_celery),
    request: Request = None,
) -> PlaybackOut:
    _ = request
    asset, version = await _load_media_asset(session, asset_id, identity)
    if not (is_video_mime(asset.mime_type) or is_audio_mime(asset.mime_type)):
        raise NotFoundError(resource="playback", id=str(asset_id))

    media_meta = _media_meta(version)
    duration = media_meta.get("duration")
    keyframes = _keyframes(settings, storage, asset, media_meta)

    out = PlaybackOut(
        asset_id=asset.id,
        mode="unsupported",
        mime_type=asset.mime_type,
        duration=duration,
        keyframes=keyframes,
    )

    # 已有转码产物：优先（kind=transcode 唯一行）
    renditions = await AssetRenditionRepo(session).for_asset(asset.id)
    transcode_rendition = next((r for r in renditions if r.kind == RenditionKind.TRANSCODE), None)
    if transcode_rendition is not None:
        out.mode = "native"
        out.mime_type = "video/mp4"
        out.url = _presign(settings, storage, transcode_rendition.storage_key)
        return out

    if is_audio_mime(asset.mime_type):
        out.mode = "audio"
        out.url = _presign(settings, storage, version.storage_key)
        return out

    native = is_native_video_mime(asset.mime_type, settings.media.native_video_mimes)
    if native:
        out.mode = "native"
        out.url = _presign(settings, storage, version.storage_key)
        return out

    # 不可直放容器 → 懒转码（首次播放触发；转码中返回关键帧 + 进度过渡）
    if not settings.media.transcode_enabled:
        return out
    progress = await _read_progress(redis, asset_id)
    running = await _transcode_job_running(session, asset.id)
    if progress and progress.get("status") == "ready":
        # 产物行写入存在竞态延迟：下一轮请求将命中 cached 分支
        out.mode = "transcoding"
        out.progress = 1.0
        return out
    if (progress and progress.get("status") == "running") or running:
        out.mode = "transcoding"
        out.progress = float(progress.get("progress") or 0.0) if progress else 0.0
        return out
    celery.send_task("media.transcode", args=[str(asset.id)], queue=QUEUE_PIPELINE_HIGH)
    out.mode = "transcoding"
    out.progress = 0.0
    return out


async def _transcode_job_running(session: AsyncSession, asset_id: uuid.UUID) -> bool:
    from loomvec.core.constants import QUEUE_PIPELINE_HIGH  # noqa: F401 — 语义注释
    from loomvec.core.db.models import JobStatus, JobType, ProcessingJob

    row = (
        await session.execute(
            select(ProcessingJob.id)
            .where(
                ProcessingJob.asset_id == asset_id,
                ProcessingJob.job_type == JobType.TRANSCODE,
                ProcessingJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _read_progress(redis, asset_id: uuid.UUID) -> dict | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(progress_key(str(asset_id)))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _keyframes(
    settings: Settings, storage: ObjectStorage, asset: Asset, media_meta: dict[str, Any]
) -> list[KeyframeOut]:
    frames: list[KeyframeOut] = []
    for scene in (media_meta.get("scenes") or [])[: settings.media.max_scenes_per_asset]:
        key = scene.get("frame_key")
        url = _presign(settings, storage, key) if key else None
        frames.append(
            KeyframeOut(
                time_start=float(scene.get("start") or 0.0),
                time_end=scene.get("end"),
                url=url,
            )
        )
    return frames


def _presign(settings: Settings, storage: ObjectStorage, key: str | None) -> str | None:
    if not key:
        return None
    try:
        return storage.presign_get(settings.storage.bucket_derived, key)
    except Exception:
        return None


@router.get("/assets/{asset_id}/subtitles")
async def subtitles(
    asset_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """转写 → WebVTT（视频/音频单元带时间区间即成轨；无转写 404）。"""
    asset, _version = await _load_media_asset(session, asset_id, identity)
    units = (
        (
            await session.execute(
                select(SemanticUnit)
                .where(
                    SemanticUnit.asset_id == asset.id,
                    SemanticUnit.time_start.is_not(None),
                )
                .order_by(SemanticUnit.time_start)
            )
        )
        .scalars()
        .all()
    )
    # 场景单元（视频）的 content 含 caption+转写，直接作为字幕文本
    cues = [
        (float(u.time_start), float(u.time_end or u.time_start + 3.0), u.content)
        for u in units
        if u.content
    ]
    if not cues:
        raise NotFoundError(resource="subtitles", id=str(asset_id))
    return Response(content=build_webvtt(cues), media_type="text/vtt")


def build_webvtt(cues: list[tuple[float, float, str]]) -> str:
    """(start, end, text) → WebVTT 文本（00:00:00.000 时间轴）。"""
    lines = ["WEBVTT", ""]

    def ts(t: float) -> str:
        ms = round(t * 1000)
        h, ms = divmod(ms, 3600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    for start, end, text in cues:
        text = text.replace("\n", " ").strip()
        lines += [f"{ts(start)} --> {ts(end)}", text, ""]
    return "\n".join(lines)
