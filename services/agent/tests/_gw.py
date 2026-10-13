"""网关测试共用工具：MockTransport 客户端、假 Redis、SSE 文本构造。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from loomvec.agent.gateway.client import GatewayClient, make_http_client

API_KEY = "acpgw_test_SECRET_KEY_value"


class FakeRedis:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = store or {}
        self.fail = False

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = value

    async def delete(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.store.pop(key, None)


class Recorder:
    """记录 MockTransport 收到的请求，并把响应交给 handler 决定。"""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def bodies(self) -> list[Any]:
        return [json.loads(r.content) if r.content else None for r in self.requests]


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], **kw: Any
) -> tuple[GatewayClient, Recorder]:
    rec = Recorder(handler)
    http = make_http_client("http://gw.test", transport=httpx.MockTransport(rec))
    return GatewayClient(http, api_key=API_KEY, **kw), rec


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def error_response(status: int, code: str, message: str = "m", details: Any = None):
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return json_response(status, {"error": err}, {"x-request-id": "req-1"})


def sse_text(events: list[tuple[str, dict[str, Any]]], *, heartbeat: bool = True) -> str:
    """网关 SSE 文本：`id: <seq>` / `event:` / `data:`，夹一条注释心跳。"""
    parts: list[str] = []
    for i, (name, data) in enumerate(events):
        if heartbeat and i == 1:
            parts.append(": ping\n\n")
        seq = data.get("seq", i + 1)
        parts.append(f"id: {seq}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")
    return "".join(parts)


def sse_response(events: list[tuple[str, dict[str, Any]]]) -> httpx.Response:
    return httpx.Response(200, text=sse_text(events), headers={"content-type": "text/event-stream"})


def ev(event: str, seq: int, turn_id: str | None = "trn_1", **data: Any) -> tuple[str, dict]:
    """一个网关事件（载荷平铺 + session_id / turn_id / seq / ts，见 toWire）。"""
    return event, {
        **data,
        "session_id": "ses_1",
        "turn_id": turn_id,
        "seq": seq,
        "ts": "2026-10-13T00:00:00.000Z",
    }


SEARCH_RESULT = {
    "batch_id": "b1",
    "ref_items": [
        {
            "n": 1,
            "unit_id": "u1",
            "asset_id": "a1",
            "asset_name": "手册",
            "unit_type": "text",
            "title": None,
            "text_snippet": "片段",
            "locator": {"pages": [3]},
        }
    ],
    "graph_evidence": [{"head": "A", "relation": "uses", "tail": "B"}],
}


def tool_events(
    seq: int,
    call_id: str,
    name: str,
    *,
    output: str = "ok",
    status: str = "completed",
    raw_input: Any = None,
) -> list[tuple[str, dict]]:
    """一次完整工具调用（tool.started + tool.completed，载荷按网关 normalize.ts 的形状）。"""
    return [
        ev(
            "tool.started",
            seq,
            tool_call_id=call_id,
            title=name,
            kind="other",
            status="pending",
            tool_name=name,
            raw_input={"q": "x"} if raw_input is None else raw_input,
            locations=[],
            content=[],
        ),
        ev(
            "tool.completed",
            seq + 1,
            tool_call_id=call_id,
            status=status,
            title=name,
            kind="other",
            tool_name=name,
            content=[{"type": "content", "content": {"type": "text", "text": output}}],
            raw_input={"q": "x"},
            raw_output=output,
        ),
    ]
