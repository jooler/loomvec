"""网关客户端（httpx.MockTransport）：鉴权头、超时 / 重试策略、错误映射、SSE、文件。"""

from __future__ import annotations

import json

import httpx
import pytest

from loomvec.agent.gateway.client import GatewayClient, make_http_client
from loomvec.agent.gateway.errors import (
    GatewayConfigError,
    GatewayError,
    GatewayPathError,
    GatewayTransportError,
    to_domain_error,
    user_message,
)
from loomvec.core.errors import (
    ConflictError,
    NotFoundError,
    RateLimitedError,
    UpstreamUnavailableError,
    ValidationError,
)

from ._gw import API_KEY, error_response, ev, json_response, make_client, sse_response, sse_text


async def test_authorization_header_and_repr_hides_key():
    client, rec = make_client(lambda r: json_response(200, {"cancelled": False, "turn_id": None}))
    await client.cancel_turn("ses_1")
    assert rec.requests[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in repr(client) and API_KEY not in str(client)


async def test_get_retries_once_on_transport_error_and_5xx():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return json_response(200, {"content": "hi"})

    client, rec = make_client(handler)
    assert await client.get_instructions("ws_1") == "hi"
    assert len(rec.requests) == 2

    seq = iter([503, 200])
    client, rec = make_client(
        lambda r: (
            error_response(503, "agent_unavailable")
            if next(seq) == 503
            else json_response(200, {"content": None})
        )
    )
    assert await client.get_instructions("ws_1") is None
    assert len(rec.requests) == 2


async def test_get_retry_is_only_once():
    client, rec = make_client(lambda r: error_response(503, "agent_unavailable", "down"))
    with pytest.raises(GatewayError) as ei:
        await client.get_instructions("ws_1")
    assert ei.value.status == 503 and len(rec.requests) == 2

    def boom(request):
        raise httpx.ConnectError("refused")

    client, rec = make_client(boom)
    with pytest.raises(GatewayTransportError):
        await client.get_instructions("ws_1")
    assert len(rec.requests) == 2


@pytest.mark.parametrize("call", ["create_workspace", "put_instructions", "patch", "cancel"])
async def test_non_get_requests_are_never_retried(call):
    def boom(request):
        raise httpx.ReadTimeout("slow")

    client, rec = make_client(boom)
    with pytest.raises(GatewayTransportError):
        if call == "create_workspace":
            await client.create_workspace("e")
        elif call == "put_instructions":
            await client.put_instructions("ws", "x")
        elif call == "patch":
            await client.patch_session("ses", {"mcp_servers": []})
        else:
            await client.cancel_turn("ses")
    assert len(rec.requests) == 1

    client, rec = make_client(lambda r: error_response(503, "agent_unavailable"))
    with pytest.raises(GatewayError):
        await client.create_workspace("e")
    assert len(rec.requests) == 1


async def test_error_body_is_parsed_without_leaking_headers():
    client, _ = make_client(
        lambda r: error_response(429, "quota_exceeded", "满了", {"quota": "daily_tokens"})
    )
    with pytest.raises(GatewayError) as ei:
        await client.create_workspace("e")
    e = ei.value
    assert (e.status, e.code, e.message) == (429, "quota_exceeded", "满了")
    assert e.details == {"quota": "daily_tokens"} and e.request_id == "req-1"
    assert API_KEY not in str(e) and API_KEY not in repr(e)

    client, _ = make_client(lambda r: httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(GatewayError) as ei:
        await client.create_workspace("e")
    assert ei.value.status == 502 and ei.value.code == "internal"


# ---- workspace 409 回填 ----


async def test_get_or_create_workspace_created():
    client, rec = make_client(lambda r: json_response(201, {"id": "ws_new"}))
    assert await client.get_or_create_workspace("env-1") == ("ws_new", True)
    assert rec.bodies()[0] == {"external_ref": "env-1"}


async def test_get_or_create_workspace_409_backfills_from_details():
    client, _ = make_client(
        lambda r: error_response(409, "conflict", "已占用", {"workspace_id": "ws_old"})
    )
    assert await client.get_or_create_workspace("env-1") == ("ws_old", False)


async def test_get_or_create_workspace_409_without_details_falls_back_to_list():
    def handler(request):
        if request.method == "POST":
            return error_response(409, "conflict")
        assert request.url.params["external_ref"] == "env-1"
        return json_response(
            200,
            {
                "items": [
                    {"id": "ws_arch", "external_ref": "env-1", "status": "archived"},
                    {"id": "ws_live", "external_ref": "env-1", "status": "active"},
                ]
            },
        )

    client, _ = make_client(handler)
    assert await client.get_or_create_workspace("env-1") == ("ws_live", False)


async def test_get_or_create_workspace_other_errors_propagate():
    client, _ = make_client(lambda r: error_response(403, "forbidden"))
    with pytest.raises(GatewayError) as ei:
        await client.get_or_create_workspace("env-1")
    assert ei.value.status == 403


# ---- SSE ----


async def test_open_turn_streams_events_and_skips_heartbeats():
    events = [
        ev("turn.started", 1, prompt=[]),
        ev("message.delta", 2, text="你好"),
        ev("turn.completed", 3, stop_reason="end_turn", usage=None, final_text="你好"),
    ]
    client, rec = make_client(lambda r: sse_response(events))
    async with client.open_turn("ses_1", [{"type": "text", "text": "q"}]) as stream:
        got = [(m.event, m.data["seq"]) async for m in stream]
    assert got == [("turn.started", 1), ("message.delta", 2), ("turn.completed", 3)]
    body = json.loads(rec.requests[0].content)
    assert body == {"content": [{"type": "text", "text": "q"}], "stream": True}
    assert rec.requests[0].headers["accept"] == "text/event-stream"


async def test_open_turn_admission_error_raises_before_any_event():
    client, _ = make_client(lambda r: error_response(400, "unsupported", "该 agent 不支持图片输入"))
    with pytest.raises(GatewayError) as ei:
        async with client.open_turn("ses_1", []):
            pytest.fail("不应进入上下文")
    assert ei.value.code == "unsupported"


async def test_open_events_sends_after_seq_and_follow():
    client, rec = make_client(lambda r: sse_response([ev("turn.completed", 9)]))
    async with client.open_events("ses_1", after_seq=7, follow=True) as stream:
        [m async for m in stream]
    q = rec.requests[0].url.params
    assert q["after_seq"] == "7" and q["follow"] == "1"


async def test_stream_transport_error_is_wrapped():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"event: message.delta\ndata: {}\n\n"
            raise httpx.ReadError("reset")

    client, _ = make_client(lambda r: httpx.Response(200, stream=Broken()))
    with pytest.raises(GatewayTransportError):
        async with client.open_turn("ses_1", []) as stream:
            [m async for m in stream]


def test_sse_text_helper_roundtrip():
    assert "event: turn.started" in sse_text([ev("turn.started", 1)])


# ---- 消息分页 ----


async def test_all_messages_follows_cursor_and_stops():
    pages = {
        None: {"items": [{"role": "user", "turn_id": "t1"}], "next_cursor": "t1"},
        "t1": {"items": [{"role": "user", "turn_id": "t2"}], "next_cursor": None},
    }

    def handler(request):
        return json_response(200, pages[request.url.params.get("cursor")])

    client, rec = make_client(handler)
    items = await client.all_messages("ses_1", page_limit=1)
    assert [i["turn_id"] for i in items] == ["t1", "t2"]
    assert len(rec.requests) == 2

    # 游标不前进时不死循环
    client, rec = make_client(lambda r: json_response(200, {"items": [], "next_cursor": "same"}))
    await client.all_messages("ses_1")
    assert len(rec.requests) == 2


# ---- 文件 ----


async def test_put_file_sends_raw_bytes_with_content_length():
    client, rec = make_client(lambda r: json_response(200, {"path": "a/b.txt", "size": 3}))
    await client.put_file("ws_1", ".loomvec/uploads/s/中文 名.txt", b"abc", size=3)
    req = rec.requests[0]
    assert req.method == "PUT" and req.content == b"abc"
    assert req.headers["content-type"] == "application/octet-stream"
    # 路径段按 URL 规则编码，'/' 保留
    assert req.url.raw_path.decode().startswith("/v1/workspaces/ws_1/files/.loomvec/uploads/s/")
    assert "%20" in req.url.raw_path.decode()

    async def chunks():
        yield b"ab"
        yield b"c"

    await client.put_file("ws_1", "x.txt", chunks(), size=3)
    assert rec.requests[1].content == b"abc"
    assert rec.requests[1].headers["content-length"] == "3"


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../b", "./x", "a/./b", "..", "", "/"])
async def test_file_paths_with_dot_segments_are_rejected_before_sending(bad):
    client, rec = make_client(lambda r: json_response(200, {}))
    with pytest.raises(GatewayPathError):
        await client.stat_file("ws_1", bad)
    with pytest.raises(GatewayPathError):
        await client.put_file("ws_1", bad, b"x")
    with pytest.raises(GatewayPathError):
        await client.mkdir("ws_1", bad)
    assert rec.requests == []


async def test_read_file_truncates_and_reports_total_size():
    body = b"x" * 5000
    client, _ = make_client(
        lambda r: httpx.Response(200, content=body, headers={"content-length": "5000"})
    )
    data, total, truncated = await client.read_file("ws_1", "big.txt", max_bytes=1000)
    assert (len(data), total, truncated) == (1000, 5000, True)

    data, total, truncated = await client.read_file("ws_1", "big.txt", max_bytes=10_000)
    assert (len(data), total, truncated) == (5000, 5000, False)


async def test_read_file_404_raises_gateway_error():
    client, _ = make_client(lambda r: error_response(404, "not_found", "文件不存在"))
    with pytest.raises(GatewayError) as ei:
        await client.read_file("ws_1", "nope.txt", max_bytes=10)
    assert ei.value.status == 404


async def test_healthz_never_raises():
    client, _ = make_client(lambda r: httpx.Response(200, json={"ok": True}))
    assert await client.healthz() is True
    client, _ = make_client(lambda r: httpx.Response(500))
    assert await client.healthz() is False

    def boom(request):
        raise httpx.ConnectError("refused")

    client, _ = make_client(boom)
    assert await client.healthz() is False


async def test_timeouts_normal_vs_sse():
    """普通请求带总超时；SSE 只设连接超时与读空闲超时（不设总时长）。"""
    seen = {}

    def handler(request):
        seen[request.url.path] = request.extensions["timeout"]
        if request.url.path.endswith("/turns"):
            return sse_response([ev("turn.completed", 1)])
        return json_response(200, {"cancelled": False})

    client, _ = make_client(handler, request_timeout_s=12.5, sse_read_timeout_s=77)
    await client.cancel_turn("ses_1")
    async with client.open_turn("ses_1", []) as stream:
        [m async for m in stream]
    normal = seen["/v1/sessions/ses_1/cancel"]
    assert normal["read"] == 12.5 and normal["connect"] == 10.0
    sse = seen["/v1/sessions/ses_1/turns"]
    assert sse["read"] == 77 and sse["connect"] == 10.0


# ---- 错误映射（合同 §3.11） ----


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "unauthorized", GatewayConfigError),
        (403, "forbidden", GatewayConfigError),
        (404, "not_found", NotFoundError),
        (429, "rate_limited", RateLimitedError),
        (429, "quota_exceeded", RateLimitedError),
        (413, "quota_exceeded", RateLimitedError),
        (409, "workspace_busy", ConflictError),
        (400, "invalid_request", ValidationError),
        (400, "path_forbidden", ValidationError),
        (500, "internal", UpstreamUnavailableError),
        (503, "agent_unavailable", UpstreamUnavailableError),
    ],
)
def test_error_mapping_to_domain_errors(status, code, expected):
    err = to_domain_error(GatewayError(status, code, "原因", {"quota": "daily_tokens"}))
    assert isinstance(err, expected)


def test_credential_errors_do_not_become_401_for_the_frontend():
    """401 会被 api 门面当成用户登录失效；网关凭据问题必须是 5xx 并带可读文案。"""
    err = to_domain_error(GatewayError(401, "unauthorized", "bad key"))
    assert err.http_status == 503 and "凭据无效" in err.message
    assert to_domain_error(GatewayTransportError("ConnectError")).http_status == 502


def test_user_messages_are_readable_chinese():
    assert "今日 token" in user_message(
        GatewayError(429, "quota_exceeded", "x", {"quota": "daily_tokens"})
    )
    assert "磁盘" in user_message(
        GatewayError(413, "quota_exceeded", "x", {"quota": "workspace_disk_bytes"})
    )
    assert "正在回答" in user_message(GatewayError(409, "workspace_busy", "x"))
    assert "不支持" in user_message(GatewayError(400, "unsupported", "图片"))


async def test_client_is_reusable_with_shared_pool_for_different_keys():
    http = make_http_client(
        "http://gw.test",
        transport=httpx.MockTransport(
            lambda r: json_response(200, {"cancelled": False, "auth": r.headers["authorization"]})
        ),
    )
    a = GatewayClient(http, api_key="key-a")
    b = GatewayClient(http, api_key="key-b")
    assert await a.cancel_turn("s") is False
    assert await b.cancel_turn("s") is False
    await http.aclose()
