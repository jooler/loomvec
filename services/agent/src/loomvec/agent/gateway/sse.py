"""SSE 解析（WHATWG 子集）：网关每帧 `id:` / `event:` / `data:`，另有 `: ping` 注释心跳。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SseMessage:
    event: str
    data: dict[str, Any]
    id: str | None = None


async def parse_sse(lines: AsyncIterable[str]) -> AsyncIterator[SseMessage]:
    """逐行 → 逐帧。注释行与无 data 的帧忽略；data 不是 JSON 对象的帧忽略。

    `lines` 来自 `httpx.Response.aiter_lines()`（已按 \\n / \\r\\n 切分，不含行尾）。
    """
    event, event_id = "message", None
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if data_lines:
                msg = _build(event, event_id, data_lines)
                if msg is not None:
                    yield msg
            event, event_id, data_lines = "message", None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value or "message"
        elif name == "data":
            data_lines.append(value)
        elif name == "id":
            event_id = value
    if data_lines:  # 流在最后一帧的空行之前结束：仍尽量交出
        msg = _build(event, event_id, data_lines)
        if msg is not None:
            yield msg


def _build(event: str, event_id: str | None, data_lines: list[str]) -> SseMessage | None:
    try:
        data = json.loads("\n".join(data_lines))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return SseMessage(event=event, data=data, id=event_id)
