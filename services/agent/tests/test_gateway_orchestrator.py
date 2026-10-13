"""提问流编排：SSE 帧序列、受理期 4xx、@ 文献旁路、断流续传、断开不取消回合。"""

from __future__ import annotations

import base64
import json

import httpx

from loomvec.agent.attachments import AttachmentMeta
from loomvec.agent.gateway.orchestrator import (
    AskContext,
    GatewayOrchestrator,
    Outcome,
    attachment_uri,
    build_prompt_blocks,
)
from loomvec.agent.gateway.service import PreparedSession
from loomvec.core.agent_tokens import MENTIONS_PREFIX

from ._gw import (
    SEARCH_RESULT,
    FakeRedis,
    error_response,
    ev,
    json_response,
    make_client,
    sse_response,
    sse_text,
    tool_events,
)

SEARCH = "mcp__loomvec__search_knowledge"
ASSET = "11111111-1111-1111-1111-111111111111"
USAGE = {
    "input_tokens": 10,
    "output_tokens": 4,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "thought_tokens": 0,
    "total_tokens": 14,
}


def _ctx(**kw) -> AskContext:
    return AskContext(
        **{
            "env_id": "env-1",
            "session_id": "sess-hex",
            "question": "问题",
            "prepared": PreparedSession(
                workspace_id="ws_1", gateway_session_id="ses_1", agent="claude", model="claude"
            ),
            **kw,
        }
    )


def _orch(redis=None, **kw) -> GatewayOrchestrator:
    return GatewayOrchestrator(redis if redis is not None else FakeRedis(), resume_delay_s=0, **kw)


def _parse(frames: list[str]) -> list[tuple[str, dict]]:
    out = []
    for f in frames:
        lines = f.strip().split("\n")
        out.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return out


async def _run(client, ctx=None, orch=None, outcome=None) -> list[tuple[str, dict]]:
    orch = orch or _orch()
    return _parse([f async for f in orch.stream(client, ctx or _ctx(), outcome)])


def _happy_events():
    return [
        ev("turn.started", 1, prompt=[{"type": "text", "text": "问题"}]),
        ev("thought.delta", 2, text="想一想"),
        *tool_events(3, "c1", SEARCH, output=json.dumps(SEARCH_RESULT)),
        ev("message.delta", 5, text="依据[1]"),
        ev("message.delta", 6, text="。"),
        ev("turn.completed", 7, stop_reason="end_turn", usage=USAGE, final_text="依据[1]。"),
    ]


async def test_full_turn_frame_sequence_matches_frontend_contract():
    client, rec = make_client(lambda r: sse_response(_happy_events()))
    outcome = Outcome()
    frames = await _run(client, outcome=outcome)
    assert [f[0] for f in frames] == [
        "meta",
        "reasoning",
        "tool_start",
        "citations",
        "tool_end",
        "delta",
        "delta",
        "usage",
        "done",
    ]
    meta = frames[0][1]
    assert meta == {
        "session_id": "sess-hex",
        "message_id": meta["message_id"],
        "model": "claude",
        "env_id": "env-1",
    }
    done = frames[-1][1]
    assert done["answer"] == "依据[1]。" and done["finish_reason"] == "end_turn"
    assert done["message_id"] == meta["message_id"] and len(done["citations"]) == 1
    assert done["usage"]["uncached_input_tokens"] == 10 and done["usage"]["turns"] == 1
    assert outcome.answer == "依据[1]。" and outcome.ok is True
    # 只发了一次 POST turns，没有取消 / 其它写调用
    assert [(r.method, r.url.path) for r in rec.requests] == [("POST", "/v1/sessions/ses_1/turns")]


async def test_turn_body_contains_question_and_stream_flag():
    client, rec = make_client(lambda r: sse_response(_happy_events()))
    await _run(client)
    assert rec.bodies()[0] == {"content": [{"type": "text", "text": "问题"}], "stream": True}


# ---- 受理阶段失败 ----


async def test_admission_4xx_becomes_error_then_done_with_no_other_frames():
    client, rec = make_client(
        lambda r: error_response(400, "unsupported", "该 agent 不支持图片输入")
    )
    outcome = Outcome()
    frames = await _run(client, outcome=outcome)
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == "unsupported" and "不支持" in frames[0][1]["message"]
    done = frames[1][1]
    assert done["finish_reason"] == "error" and done["answer"] == "" and done["usage"] == {}
    assert outcome.ok is False and outcome.answer == ""
    assert len(rec.requests) == 1  # POST 不重试


async def test_busy_and_quota_errors_have_readable_messages():
    client, _ = make_client(lambda r: error_response(409, "workspace_busy", "busy"))
    frames = await _run(client)
    assert frames[0][1]["code"] == "workspace_busy" and "正在回答" in frames[0][1]["message"]

    client, _ = make_client(
        lambda r: error_response(429, "quota_exceeded", "q", {"quota": "daily_tokens"})
    )
    frames = await _run(client)
    assert "今日 token" in frames[0][1]["message"]


async def test_gateway_unreachable_becomes_error_then_done():
    def boom(request):
        raise httpx.ConnectError("refused")

    client, rec = make_client(boom)
    frames = await _run(client)
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == "gateway_unreachable"
    assert len(rec.requests) == 1  # POST 非幂等，不重试


async def test_turn_failed_before_started_runtime_unavailable():
    """runtime 拉不起来：没有 turn.started，直接 turn.failed。"""
    client, _ = make_client(
        lambda r: sse_response([ev("turn.failed", 1, code="agent_unavailable", message="镜像缺失")])
    )
    frames = await _run(client)
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == "agent_unavailable"


# ---- @ 文献旁路 ----


async def test_mentions_are_published_per_session_and_cleared_when_absent():
    redis = FakeRedis()
    client, _ = make_client(lambda r: sse_response(_happy_events()))
    mentions = [{"asset_id": ASSET, "name": "文献 A"}]
    await _run(client, _ctx(mentions=mentions), _orch(redis))
    assert json.loads(redis.store[f"{MENTIONS_PREFIX}sess-hex"]) == [ASSET]
    # 键按会话，不是 env
    assert f"{MENTIONS_PREFIX}env-1" not in redis.store

    # 下一轮没有 @：清掉上一轮残留
    await _run(client, _ctx(), _orch(redis))
    assert f"{MENTIONS_PREFIX}sess-hex" not in redis.store


async def test_mentions_publish_failure_aborts_before_calling_gateway():
    redis = FakeRedis()
    redis.fail = True
    client, rec = make_client(lambda r: sse_response(_happy_events()))
    frames = await _run(client, _ctx(mentions=[{"asset_id": ASSET, "name": "A"}]), _orch(redis))
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == "mentions_publish_failed"
    assert rec.requests == []  # 检索范围没设好就不能放行


# ---- 断流续传 ----


def _resume_handler(first_events, resume_events_by_after=None, *, first_error: bool = False):
    calls = {"turns": 0, "events": []}
    resume_events_by_after = resume_events_by_after or {}

    def handler(request):
        if request.url.path.endswith("/turns"):
            calls["turns"] += 1
            if first_error:
                return httpx.Response(
                    200,
                    text=sse_text(first_events, heartbeat=False)
                    + "event: error\ndata: "
                    + json.dumps({"error": {"code": "internal", "message": "事件落库失败"}})
                    + "\n\n",
                    headers={"content-type": "text/event-stream"},
                )
            return sse_response(first_events)  # 没有终态事件就结束 = 流被切断
        after = int(request.url.params["after_seq"])
        calls["events"].append(after)
        return sse_response(resume_events_by_after[after])

    return handler, calls


async def test_stream_cut_without_terminal_resumes_from_last_seq_without_duplicates():
    first = [
        ev("turn.started", 1),
        ev("message.delta", 2, text="前半"),
    ]
    rest = [
        ev("message.delta", 3, text="后半"),
        ev("turn.completed", 4, stop_reason="end_turn", usage=None, final_text="前半后半"),
    ]
    handler, calls = _resume_handler(first, {2: rest})
    client, _ = make_client(handler)
    frames = await _run(client)
    assert [f[0] for f in frames] == ["meta", "delta", "delta", "done"]
    assert frames[-1][1]["answer"] == "前半后半"
    assert calls == {"turns": 1, "events": [2]}  # POST 一次；events 从 seq 2 之后续


async def test_resume_ignores_other_turns_events_in_replay():
    first = [ev("turn.started", 1), ev("message.delta", 2, text="A")]
    rest = [
        ev("turn.completed", 3, turn_id="trn_OTHER", stop_reason="end_turn"),  # 别的回合
        ev("turn.completed", 4, stop_reason="end_turn", usage=None, final_text="A"),
    ]
    handler, _ = _resume_handler(first, {2: rest})
    client, _ = make_client(handler)
    frames = await _run(client)
    assert frames[-1][0] == "done" and frames[-1][1]["answer"] == "A"


async def test_gateway_error_frame_triggers_resume_then_completes():
    first = [ev("turn.started", 1), ev("message.delta", 2, text="ok")]
    rest = [ev("turn.completed", 3, stop_reason="end_turn", usage=None, final_text="ok")]
    handler, calls = _resume_handler(first, {2: rest}, first_error=True)
    client, _ = make_client(handler)
    frames = await _run(client)
    assert [f[0] for f in frames] == ["meta", "delta", "done"]
    assert frames[-1][1]["finish_reason"] == "end_turn" and calls["events"] == [2]


async def test_resume_gives_up_after_max_attempts_and_reports_gateway_error():
    first = [ev("turn.started", 1), ev("message.delta", 2, text="x")]

    def handler(request):
        if request.url.path.endswith("/turns"):
            return httpx.Response(
                200,
                text="id: 1\nevent: turn.started\ndata: "
                + json.dumps(first[0][1])
                + "\n\nevent: error\ndata: "
                + json.dumps({"error": {"code": "internal", "message": "写不进日志"}})
                + "\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            text='event: error\ndata: {"error":{"code":"internal","message":"写不进日志"}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    client, rec = make_client(handler)
    frames = await _run(client, orch=_orch(max_resumes=2))
    assert [f[0] for f in frames] == ["meta", "error", "done"]
    assert frames[1][1]["code"] == "internal" and "写不进日志" in frames[1][1]["message"]
    assert frames[2][1]["finish_reason"] == "error"
    # 1 次 POST + 2 次续传
    assert [r.method for r in rec.requests] == ["POST", "GET", "GET"]


async def test_transport_error_before_any_event_does_not_repost_or_resume():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("reset")
            yield b""  # pragma: no cover

    client, rec = make_client(lambda r: httpx.Response(200, stream=Broken()))
    frames = await _run(client)
    assert [f[0] for f in frames] == ["error", "done"]
    assert len(rec.requests) == 1


async def test_stream_ending_empty_reports_interruption():
    client, rec = make_client(lambda r: sse_response([]))
    frames = await _run(client)
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == "stream_interrupted"
    assert len(rec.requests) == 1


# ---- 断开 / 取消语义 ----


async def test_client_disconnect_stops_forwarding_but_never_cancels_the_turn():
    client, rec = make_client(lambda r: sse_response(_happy_events()))
    gen = _orch().stream(client, _ctx())
    first = await gen.__anext__()
    assert first.startswith("event: meta")
    await gen.aclose()  # 浏览器 / 门面断开 → 生成器被关闭
    paths = [r.url.path for r in rec.requests]
    assert paths == ["/v1/sessions/ses_1/turns"]
    assert not any(p.endswith("/cancel") for p in paths)


async def test_cancelled_turn_flows_through_as_status_and_done():
    client, _ = make_client(
        lambda r: sse_response(
            [
                ev("turn.started", 1),
                ev("message.delta", 2, text="半截"),
                ev("turn.completed", 3, stop_reason="cancelled", usage=None, final_text="半截"),
            ]
        )
    )
    outcome = Outcome()
    frames = await _run(client, outcome=outcome)
    assert [f[0] for f in frames][-2:] == ["status", "done"]
    assert frames[-1][1]["finish_reason"] == "cancelled"
    assert outcome.finish_reason == "cancelled" and outcome.ok is True


# ---- prompt 组装 ----


def test_attachment_uri_percent_encodes_but_keeps_slashes():
    assert attachment_uri(".loomvec/uploads/s/ab12_中 文.txt") == (
        "workspace:///.loomvec/uploads/s/ab12_%E4%B8%AD%20%E6%96%87.txt"
    )


async def test_prompt_blocks_order_mentions_attachments_question():
    png = b"\x89PNG-fake"

    def handler(request):
        assert request.url.path.endswith("/files/.loomvec/uploads/s/aaaaaaaaaaaa_p.png")
        return httpx.Response(200, content=png, headers={"content-length": str(len(png))})

    client, _ = make_client(handler)
    ctx = _ctx(
        mentions=[{"asset_id": ASSET, "name": "文献\nA"}],
        attachments=[
            AttachmentMeta(
                "aaaaaaaaaaaa_p.png",
                ".loomvec/uploads/s/aaaaaaaaaaaa_p.png",
                "p.png",
                len(png),
                "image/png",
                True,
            ),
            AttachmentMeta(
                "bbbbbbbbbbbb_r.pdf",
                ".loomvec/uploads/s/bbbbbbbbbbbb_r.pdf",
                "r.pdf",
                9,
                "application/pdf",
                False,
            ),
        ],
    )
    blocks = await build_prompt_blocks(client, "ws_1", ctx)
    assert [b["type"] for b in blocks] == ["text", "image", "resource_link", "text", "text"]
    assert "<mentioned-documents>" in blocks[0]["text"] and ASSET in blocks[0]["text"]
    assert blocks[1] == {
        "type": "image",
        "data": base64.b64encode(png).decode(),
        "mimeType": "image/png",
    }
    assert blocks[2]["uri"] == "workspace:///.loomvec/uploads/s/bbbbbbbbbbbb_r.pdf"
    assert blocks[2]["name"] == "r.pdf" and blocks[2]["mimeType"] == "application/pdf"
    assert ".loomvec/uploads/s/bbbbbbbbbbbb_r.pdf" in blocks[3]["text"]
    assert blocks[4] == {"type": "text", "text": "问题"}


async def test_prompt_has_no_project_directory_prefix_block():
    """项目目录由会话 cwd 承担，prompt 里不再拼目录说明。"""
    client, _ = make_client(lambda r: sse_response([]))
    blocks = await build_prompt_blocks(client, "ws_1", _ctx())
    assert blocks == [{"type": "text", "text": "问题"}]


async def test_imported_session_first_prompt_carries_replay_prefix_and_history_hides_it():
    """导入的历史会话：agent 看不到导入历史，首个提问把此前对话补成前缀；历史展示时剔除前缀。"""
    from loomvec.agent.gateway.history import fold_history

    history_view = {
        "items": [
            {"role": "user", "turn_id": "t1", "content": [{"type": "text", "text": "旧问题"}]},
            {"role": "assistant", "turn_id": "t1", "text": "旧回答"},
        ],
        "next_cursor": None,
    }
    client, _ = make_client(lambda r: json_response(200, history_view))
    ctx = _ctx(
        prepared=PreparedSession(
            workspace_id="ws_1",
            gateway_session_id="ses_1",
            agent="dsh",
            model="m",
            imported=True,
        ),
        question="新问题",
    )
    blocks = await build_prompt_blocks(client, "ws_1", ctx)
    assert [b["text"] for b in blocks][1:] == ["新问题"]
    note = blocks[0]["text"]
    assert note.startswith("以下是本会话此前的对话记录") and "<user>旧问题</user>" in note
    assert "<assistant>旧回答</assistant>" in note and note.endswith("请基于以上历史继续本会话。")

    # 该 prompt 落进网关历史后，用户消息展示里不含前缀
    stored = {
        "role": "user",
        "turn_id": "t2",
        "content": blocks,
    }
    (entry,) = fold_history([stored])
    assert entry["content"] == "新问题"

    # 非导入会话不查历史、不加前缀
    client2, rec2 = make_client(lambda r: json_response(500, {}))
    plain = await build_prompt_blocks(client2, "ws_1", _ctx())
    assert [b["text"] for b in plain] == ["问题"] and rec2.requests == []
