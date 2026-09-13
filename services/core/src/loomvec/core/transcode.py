"""P3-WRK-05 懒转码：首次播放触发预览转码（ffmpeg → H.264/AAC MP4）。

- 转码进度经 on_progress 回调（0~1）上报（worker 写 Redis + 发布事件，
  播放接口/前端轮询消费）；产物落 derived bucket 并登记 asset_rendition
  （kind=transcode，唯一约束天然防重复转码）；
- 浏览器可直放的容器（mp4/webm，配置白名单）不走转码；
- ffmpeg 缺失时任务失败（记错误，前端以关键帧+提示过渡）。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger("loomvec.media.transcode")

_PROGRESS_KEY_TTL = 6 * 3600


def has_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


async def probe_duration(path: Path, timeout: float) -> float:
    """ffprobe 时长（秒）；失败返回 0。"""
    from loomvec.core.pipeline.media_flow import ffprobe

    try:
        meta = await ffprobe(path, timeout)
        return float((meta.get("format") or {}).get("duration") or 0.0)
    except Exception:
        return 0.0


async def transcode_preview(
    src: Path,
    out: Path,
    *,
    height: int,
    crf: int,
    preset: str,
    timeout: float,
    on_progress: Callable[[float], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """转码为 H.264 + AAC MP4（web 播放兼容），高度等比缩放（音频轨照常转码）。"""
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg/ffprobe 不可用（worker 镜像需安装）")
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-vf",
        f"scale=-2:{height}",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",  # 便于浏览器边下边播
        "-nostats",
        "-progress",
        "pipe:1",
        str(out),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    duration = await probe_duration(src, timeout=min(timeout / 2, 120.0))

    async def _pump_progress() -> None:
        if proc.stdout is None:
            return
        async for line in proc.stdout:
            text = line.decode(errors="replace").strip()
            if not text or "=" not in text:
                continue
            key, _, value = text.partition("=")
            if key != "out_time_ms" or not value.isdigit():
                continue
            seconds = int(value) / 1_000_000
            if duration > 0 and on_progress is not None:
                await on_progress(min(0.99, seconds / duration))

    try:
        await asyncio.wait_for(asyncio.gather(proc.wait(), _pump_progress()), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
        raise RuntimeError(f"ffmpeg 转码失败（退出码 {proc.returncode}）：{err[-500:]}")
    if not out.exists():
        raise RuntimeError("转码未产出文件")


def progress_key(asset_id: str) -> str:
    from loomvec.core.constants import TRANSCODE_PROGRESS_KEY

    return TRANSCODE_PROGRESS_KEY.format(asset_id=asset_id)


async def write_progress(redis: Any, asset_id: str, progress: float, status: str) -> None:
    """进度写 Redis（懒加载的播放接口读取）并发布事件。"""
    if redis is None:
        return
    try:
        payload = json.dumps(
            {"asset_id": asset_id, "progress": round(progress, 4), "status": status}
        )
        await redis.set(progress_key(asset_id), payload, ex=_PROGRESS_KEY_TTL)
        from loomvec.core.constants import EVENT_TRANSCODE_PROGRESS
        from loomvec.core.events import publish_event

        await publish_event(redis, {"type": EVENT_TRANSCODE_PROGRESS, **json.loads(payload)})
    except Exception as e:
        logger.debug("transcode_progress_write_failed", asset_id=asset_id, error=str(e))
