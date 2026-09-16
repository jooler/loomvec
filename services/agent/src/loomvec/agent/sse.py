"""SSE 帧序列化（对齐旧 qa.py `_sse` 格式：event:/data: + 空行结尾）。"""

from __future__ import annotations

import json
from typing import Any


def sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
