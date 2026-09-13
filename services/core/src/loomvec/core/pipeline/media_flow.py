"""P3-WRK-03/04 音视频管线分支：parse（ffprobe/场景切分/关键帧/转写）、
chunk（场景/时间戳分块）、embed（文本 + 关键帧 clip 双通道）。

外部工具约定：
- ffmpeg / ffprobe 走系统二进制（worker 镜像安装；缺失时降级——场景=全片单段、
  无关键帧、无转写，文本单元仍可建，管线不阻断）；
- 场景切分优先 PySceneDetect（可选依赖 `loomvec-worker[media]`），未安装时
  用 ffmpeg scene score 滤波（select='gt(scene,t)'）等效替代；
- 转写用 faster-whisper（可选依赖，懒加载；缺失时跳过转写）。

时间戳约定：全部秒（float）；单元落 semantic_unit.time_start/time_end，
locator {"time_start", "time_end", "frame_key"} 供播放器 seek 与字幕对齐。
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import (
    Asset,
    AssetVersion,
    ChunkMethod,
    GraphStatus,
    SemanticUnit,
    UnitType,
)
from loomvec.core.errors import ValidationError
from loomvec.core.pipeline.base import IMAGE_UNIT_MODEL_PREFIX, PipelineDeps
from loomvec.core.pipeline.mime import is_video_mime
from loomvec.core.storage_keys import embed_vectors_key, keyframe_key

logger = structlog.get_logger("loomvec.pipeline.media")

MEDIA_UNIT_MODEL_PREFIX = "media:"


# ---------------------------------------------------------------------------
# 外部工具封装
# ---------------------------------------------------------------------------


def _has(*bins: str) -> bool:
    return all(shutil.which(b) for b in bins)


async def _run_cmd(args: list[str], timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{args[0]} 退出码 {proc.returncode}: {err.decode()[-500:]}")
    return out.decode()


async def ffprobe(path: Path, timeout: float) -> dict[str, Any]:
    """ffprobe 元数据：时长/分辨率/编解码/码率/采样率。"""
    out = await _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout,
    )
    return json.loads(out)


async def detect_scenes(
    path: Path, *, threshold: float, min_scene_seconds: float, duration: float, timeout: float
) -> list[tuple[float, float]]:
    """场景切分：PySceneDetect 优先，缺失时 ffmpeg scene score。

    返回 [(start, end)]（秒，闭开区间语义，末段含尾）。
    """
    cuts: list[float] = []
    try:
        cuts = await _detect_scenes_pyscenedetect(path, threshold, timeout)
    except ImportError:
        try:
            cuts = await _detect_scenes_ffmpeg(path, threshold / 100.0, duration, timeout)
        except Exception as e:
            logger.warning("scene_detect_failed_fallback_single", error=str(e))
    except Exception as e:  # PySceneDetect 运行失败 → ffmpeg 兜底
        logger.warning("scene_detect_pyscenedetect_failed", error=str(e))
        try:
            cuts = await _detect_scenes_ffmpeg(path, threshold / 100.0, duration, timeout)
        except Exception as e2:
            logger.warning("scene_detect_failed_fallback_single", error=str(e2))

    points = sorted(c for c in cuts if 0.0 < c < duration)
    scenes: list[tuple[float, float]] = []
    start = 0.0
    for p in points:
        if p - start >= min_scene_seconds:
            scenes.append((round(start, 3), round(p, 3)))
            start = p
    scenes.append((round(start, 3), round(duration, 3)))
    return scenes


async def _detect_scenes_pyscenedetect(path: Path, threshold: float, timeout: float) -> list[float]:
    """PySceneDetect ContentDetector（可选依赖）。"""

    def _run() -> list[float]:
        from scenedetect import ContentDetector, SceneManager, open_video

        video = open_video(str(path))
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=threshold))
        manager.detect_scenes(video, show_progress=False)
        return [t.get_seconds() for t in manager.get_cut_list()]

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=timeout)


async def _detect_scenes_ffmpeg(
    path: Path, threshold: float, duration: float, timeout: float
) -> list[float]:
    """ffmpeg scene score：select 滤波输出超过阈值的帧时间点（0~1 分数）。"""
    if duration <= 0:
        return []
    th = f"{threshold:.3f}"
    out = await _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"movie={path},select='gt(scene\\,{th})'",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
        ],
        timeout,
    )
    cuts: list[float] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            cuts.append(float(line))
        except ValueError:
            continue
    return cuts


async def extract_keyframes(
    path: Path, scenes: list[tuple[float, float]], out_dir: Path, *, width: int, timeout: float
) -> list[Path | None]:
    """每个场景中点抽一帧，缩放到目标宽度（ffmpeg；缺失时返回 None 列表）。"""
    if not _has("ffmpeg"):
        logger.warning("keyframes_skipped", reason="ffmpeg_not_available")
        return [None] * len(scenes)
    paths: list[Path | None] = []
    scale = f"scale={width}:-2" if width else "scale=iw:-2"
    for i, (start, end) in enumerate(scenes):
        target = out_dir / f"scene-{i:04d}.jpg"
        at = max(0.0, (start + end) / 2)
        try:
            await _run_cmd(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{at:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    scale,
                    str(target),
                ],
                timeout,
            )
            paths.append(target if target.exists() else None)
        except Exception as e:
            logger.warning("keyframe_extract_failed", scene=i, error=str(e))
            paths.append(None)
    return paths


# ---------------------------------------------------------------------------
# 转写（faster-whisper，可选依赖懒加载）
# ---------------------------------------------------------------------------


async def transcribe(
    path: Path,
    *,
    model_size: str,
    device: str,
    compute_type: str,
    language: str | None,
    timeout: float,
) -> list[dict[str, float | str]]:
    """faster-whisper 转写：返回 [{start, end, text}]；未安装/失败返回 []。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.warning("transcribe_skipped", reason="faster_whisper_not_installed")
        return []

    def _run() -> list[dict[str, float | str]]:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments, _info = model.transcribe(
            str(path), language=language, vad_filter=True, beam_size=1
        )
        return [
            {
                "start": float(s.start),
                "end": float(s.end),
                "text": str(s.text).strip(),
            }
            for s in segments
            if str(s.text).strip()
        ]

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=timeout)
    except Exception as e:
        logger.warning("transcribe_failed", error=str(e))
        return []


# ---------------------------------------------------------------------------
# parse 步骤分支
# ---------------------------------------------------------------------------


async def parse_media(
    session: AsyncSession,
    deps: PipelineDeps,
    asset: Asset,
    version: AssetVersion,
    data: bytes,
    mime: str,
) -> dict:
    """媒体解析：ffprobe → 场景切分（视频）→ 关键帧 → VLM 场景 caption → 转写。"""
    settings = deps.settings
    media = settings.media
    is_video = is_video_mime(mime)

    with tempfile.TemporaryDirectory(prefix="loomvec-media-") as tmp:
        src = Path(tmp) / f"source{Path(asset.name).suffix or '.bin'}"
        src.write_bytes(data)
        duration = 0.0
        width = height = None
        video_codec = audio_codec = None
        sample_rate = None
        try:
            probe = await ffprobe(src, media.probe_timeout_seconds)
            duration = float((probe.get("format") or {}).get("duration") or 0.0)
            for stream in probe.get("streams") or []:
                if stream.get("codec_type") == "video" and width is None:
                    width, height = stream.get("width"), stream.get("height")
                    video_codec = stream.get("codec_name")
                elif stream.get("codec_type") == "audio" and audio_codec is None:
                    audio_codec = stream.get("codec_name")
                    sample_rate = stream.get("sample_rate")
        except Exception as e:
            logger.warning("ffprobe_failed", asset_id=str(asset.id), error=str(e))
        if duration <= 0:
            duration = 0.0

        # 场景切分（视频）；音频=单场景全片
        if is_video and duration > 0:
            scenes = await detect_scenes(
                src,
                threshold=media.scene_threshold,
                min_scene_seconds=media.min_scene_seconds,
                duration=duration,
                timeout=media.probe_timeout_seconds,
            )
        else:
            scenes = [(0.0, duration)] if duration > 0 else []
        if len(scenes) > media.max_scenes_per_asset:  # 超长视频截断（降采样后仍超则牺牲精度）
            step = math.ceil(len(scenes) / media.max_scenes_per_asset)
            scenes = [
                (scenes[i][0], scenes[min(i + step, len(scenes)) - 1][1])
                for i in range(0, len(scenes), step)
            ]

        # 关键帧 + 场景 caption（视频）
        frames: list[str | None] = []
        captions: list[str | None] = []
        if is_video and scenes:
            frame_paths = await extract_keyframes(
                src,
                scenes,
                Path(tmp),
                width=media.keyframe_width,
                timeout=media.probe_timeout_seconds,
            )
            for i, fp in enumerate(frame_paths):
                key = keyframe_key(asset.id, version.version, i, media.keyframe_format)
                if fp is None:
                    frames.append(None)
                    continue
                payload = fp.read_bytes()
                await deps.storage.put_object(
                    settings.storage.bucket_derived,
                    key,
                    payload,
                    content_type="image/jpeg",
                )
                frames.append(key)
            captions = await _scene_captions(deps, asset, frames, scenes)

        # 转写（音视频通用）
        transcript: list[dict[str, Any]] = []
        if settings.transcribe.enabled and duration > 0:
            transcript = await transcribe(
                src,
                model_size=settings.transcribe.model_size,
                device=settings.transcribe.device,
                compute_type=settings.transcribe.compute_type,
                language=settings.transcribe.language,
                timeout=max(media.probe_timeout_seconds, duration * 4),
            )

    parse_meta = {
        "parser": "media",
        "parser_version": settings.pipeline.parser_version,
        "md_key": None,
        "layout_key": None,
        "page_count": 1,
        "mime": mime,
        "media": {
            "kind": "video" if is_video else "audio",
            "duration": duration,
            "width": width,
            "height": height,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
            "sample_rate": sample_rate,
            "scenes": [
                {
                    "start": s,
                    "end": e,
                    "frame_key": frames[i] if i < len(frames) else None,
                    "caption": captions[i] if i < len(captions) else None,
                }
                for i, (s, e) in enumerate(scenes)
            ],
            "transcript": transcript,
        },
    }
    version.version_meta = {**version.version_meta, "parse": parse_meta}
    asset.asset_meta = {
        **asset.asset_meta,
        "page_count": 1,
        "duration": duration,
        "media_kind": "video" if is_video else "audio",
    }
    await session.flush()
    return {
        "parser": "media",
        "duration": duration,
        "scenes": len(scenes),
        "transcript_segments": len(transcript),
    }


async def _scene_captions(
    deps: PipelineDeps, asset: Asset, frames: list[str | None], scenes: list[tuple[float, float]]
) -> list[str | None]:
    """VLM 场景 caption（逐帧；失败降级 None 不阻断）。"""
    if not deps.settings.image.caption_enabled:
        return [None] * len(frames)
    captions: list[str | None] = []
    for i, key in enumerate(frames):
        if key is None:
            captions.append(None)
            continue
        try:
            data = await deps.storage.get_object(deps.settings.storage.bucket_derived, key)
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"[LOOMVEC_TASK=image_caption]"
                                f"[LOOMVEC_IMAGE_NAME={asset.name} 场景{i + 1}]"
                                "用一句中文描述这个视频场景画面。"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64," + base64.b64encode(data).decode()
                            },
                        },
                    ],
                }
            ]
            raw = (await deps.ai.vlm(messages)).strip()
            caption = str(json.loads(raw).get("caption") or raw) if raw.startswith("{") else raw
            captions.append(caption or None)
        except Exception as e:
            logger.warning("scene_caption_failed", scene=i, error=str(e))
            captions.append(None)
    return captions


# ---------------------------------------------------------------------------
# chunk 步骤分支
# ---------------------------------------------------------------------------


def _group_transcript(
    transcript: list[dict[str, Any]], *, max_chars: int, max_seconds: float
) -> list[dict[str, Any]]:
    """转写段按目标字符数/时长归组成块（时间戳保持连续区间）。"""
    groups: list[dict[str, Any]] = []
    for seg in transcript:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        if (
            groups
            and len(groups[-1]["text"]) + len(text) <= max_chars
            and float(seg["end"]) - float(groups[-1]["start"]) <= max_seconds
        ):
            last = groups[-1]
            last["text"] += text
            last["end"] = float(seg["end"])
        else:
            groups.append({"start": float(seg["start"]), "end": float(seg["end"]), "text": text})
    return groups


async def chunk_media(
    session: AsyncSession, deps: PipelineDeps, asset: Asset, version: AssetVersion
) -> dict:
    """媒体分块：视频=场景单元（caption+场景内转写）；音频=转写时间戳分块。"""
    parse_meta: dict[str, Any] = version.version_meta.get("parse") or {}
    media_meta: dict[str, Any] = parse_meta.get("media") or {}
    duration = float(media_meta.get("duration") or 0.0)
    scenes: list[dict[str, Any]] = media_meta.get("scenes") or []
    transcript: list[dict[str, Any]] = media_meta.get("transcript") or []
    is_video = media_meta.get("kind") == "video"

    old_units = (
        (await session.execute(select(SemanticUnit).where(SemanticUnit.version_id == version.id)))
        .scalars()
        .all()
    )
    for u in old_units:
        await session.delete(u)

    units_meta: list[dict[str, Any]] = []

    def _unit(
        content: str, title: str | None, start: float | None, end: float | None, frame: str | None
    ) -> SemanticUnit:
        locator: dict[str, Any] = {}
        if start is not None:
            locator = {"time_start": start, "time_end": end, **({"frame": frame} if frame else {})}
        return SemanticUnit(
            asset_id=asset.id,
            version_id=version.id,
            space_id=asset.space_id,
            tenant_id=asset.tenant_id,
            unit_type=UnitType.TEXT,
            title=title,
            content=content,
            keywords=[],
            locator=locator,
            time_start=start,
            time_end=end,
            chunk_method=ChunkMethod.STRUCTURAL_FALLBACK,
            order_index=len(units_meta),
            char_count=len(content),
        )

    if is_video and scenes:
        for sc in scenes:
            start, end = float(sc["start"]), float(sc["end"])
            frame = sc.get("frame_key")
            caption = sc.get("caption")
            seg_text = " ".join(
                str(t.get("text") or "").strip()
                for t in transcript
                if float(t.get("start", 0)) >= start - 1e-6 and float(t.get("end", 0)) <= end + 1e-6
            ).strip()
            parts = []
            if caption:
                parts.append(str(caption))
            if seg_text:
                parts.append(seg_text)
            content = "\n".join(parts) or f"视频场景 {start:.1f}s~{end:.1f}s"
            title = (str(caption)[:120] if caption else None) or f"场景 {start:.0f}s-{end:.0f}s"
            units_meta.append(_unit(content, title, start, end, frame))
    else:
        groups = _group_transcript(
            transcript,
            max_chars=deps.settings.transcribe.segment_max_chars,
            max_seconds=deps.settings.transcribe.segment_max_seconds,
        )
        if groups:
            for g in groups:
                units_meta.append(
                    _unit(str(g["text"]), None, float(g["start"]), float(g["end"]), None)
                )
        else:
            # 无转写（whisper 缺失/静音）：退化为资产级单单元，保证资产可检索
            kind = "视频" if is_video else "音频"
            mm, ss = divmod(int(duration), 60)
            content = f"{kind}资产：{asset.name}（时长 {mm:02d}:{ss:02d}）"
            units_meta.append(_unit(content, asset.name, 0.0, duration or None, None))

    for u in units_meta:
        session.add(u)
    await session.flush()

    version.chunk_cache_key = None  # 媒体分块由解析产物决定，重解析即重切
    version.version_meta = {
        **version.version_meta,
        "chunk": {"units": len(units_meta), "fallback_batches": 0, "unit_type": "media"},
    }
    if deps.settings.graph.enabled and deps.settings.graph.extraction_enabled:
        asset.graph_status = GraphStatus.PENDING
    else:
        asset.graph_status = GraphStatus.SKIPPED
    await session.flush()
    return {"units": len(units_meta), "unit_type": "media"}


# ---------------------------------------------------------------------------
# embed 步骤分支（文本 + 关键帧 clip 双通道）
# ---------------------------------------------------------------------------


async def embed_media(
    session: AsyncSession, deps: PipelineDeps, asset: Asset, version: AssetVersion
) -> dict:
    """媒体嵌入：单元文本走 text_dense；视频场景关键帧走 clip_dense。"""
    mock = deps.settings.ai.mock
    text_model = "mock" if mock else (deps.settings.ai.embedding.model or "unknown")
    clip_model = "mock" if mock else (deps.settings.ai.clip.model or "unknown")
    units = (
        (await session.execute(select(SemanticUnit).where(SemanticUnit.version_id == version.id)))
        .scalars()
        .all()
    )
    if not units:
        raise ValidationError("无可嵌入单元（请先跑 chunk 步骤）", asset_id=str(asset.id))

    max_chars = deps.settings.pipeline.embed_text_max_chars
    batch_size = max(1, deps.settings.pipeline.embed_batch_size)
    vectors: dict[str, list[float]] = {}
    for i in range(0, len(units), batch_size):
        batch = units[i : i + batch_size]
        embed_inputs = [f"{(u.title or '')[:100]}\n{u.content[:max_chars]}" for u in batch]
        vecs = await deps.ai.embed(embed_inputs)
        for u, v in zip(batch, vecs, strict=True):
            vectors[str(u.id)] = v
            u.embed_model_version = text_model
        await session.flush()

    # 关键帧 clip 向量：场景单元 locator.frame → clip_dense
    frames: dict[str, list[float]] = {}
    frame_units = [u for u in units if (u.locator or {}).get("frame") and u.time_start is not None]
    if frame_units:
        payloads: list[bytes] = []
        keys: list[str] = []
        captions: list[str | None] = []
        for u in frame_units:
            key = u.locator["frame"]
            data = await deps.storage.get_object(deps.settings.storage.bucket_derived, key)
            payloads.append(data)
            keys.append(key)
            captions.append(u.title)
        clip_vecs = await deps.ai.clip_embed_images(payloads, captions=captions)
        frames = dict(zip(keys, clip_vecs, strict=True))

    vectors_key = embed_vectors_key(asset.id, version.version)
    await deps.storage.put_object(
        deps.settings.storage.bucket_derived,
        vectors_key,
        json.dumps({"units": vectors, "frames": frames, "layout": "media"}).encode(),
        content_type="application/json",
    )
    version.version_meta = {
        **version.version_meta,
        "embed": {
            "model_version": text_model,
            "clip_model_version": IMAGE_UNIT_MODEL_PREFIX + clip_model,
            "vectors_key": vectors_key,
            "count": len(vectors),
            "frames": len(frames),
            "channel": "media",
        },
    }
    await session.flush()
    return {"embedded": len(vectors), "frames": len(frames), "model_version": text_model}
