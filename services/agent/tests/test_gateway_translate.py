"""事件翻译（纯状态机）：网关真实事件载荷 → loomvec SSE 帧。

事件样例的字段名取自网关 `src/events/normalize.ts` 与 `docs/api.md` 事件表。
"""

from __future__ import annotations

import json

import pytest

from loomvec.agent.gateway.translate import (
    CitationsLookup,
    Frame,
    TurnTranslator,
    content_text,
    failure_message,
    raw_output_text,
    search_payload,
)

from ._gw import SEARCH_RESULT, FakeRedis, ev, tool_events

SEARCH = "mcp__loomvec__search_knowledge"


def _t(**kw) -> TurnTranslator:
    return TurnTranslator(
        **{"session_id": "sess-hex", "message_id": "msg-1", "model": "m-1", "env_id": "env-1", **kw}
    )


def _feed(t: TurnTranslator, events) -> list:
    out = []
    for name, data in events:
        out += t.feed(name, data)
    return out


def _frames(out) -> list[tuple[str, dict]]:
    return [(o.event, o.data) for o in out if isinstance(o, Frame)]


# ---- 基本事件 ----


def test_turn_started_emits_meta_once():
    t = _t()
    out = _feed(t, [ev("turn.started", 1, prompt=[]), ev("turn.started", 2)])
    assert _frames(out) == [
        (
            "meta",
            {"session_id": "sess-hex", "message_id": "msg-1", "model": "m-1", "env_id": "env-1"},
        )
    ]


def test_message_and_thought_deltas_pass_through_verbatim():
    t = _t()
    out = _feed(
        t,
        [
            ev("thought.delta", 1, text="先想想"),
            ev("message.delta", 2, text="你"),
            ev("message.delta", 3, text="好"),
            ev("message.delta", 4, content={"type": "image", "data": "x"}),  # 非文本块：忽略
            ev("message.delta", 5, text=""),
        ],
    )
    assert _frames(out) == [
        ("reasoning", {"text": "先想想"}),
        ("delta", {"text": "你"}),
        ("delta", {"text": "好"}),
    ]
    assert t.answer == "你好"


def test_whole_message_delta_is_not_split_into_typewriter_frames():
    """dsh 整段到达：一个 delta 帧，不做打字机回放。"""
    t = _t()
    out = _feed(t, [ev("message.delta", 1, text="这是一整段回答，一次到达。")])
    assert len(_frames(out)) == 1


def test_tool_lifecycle_frames():
    t = _t()
    out = _feed(t, tool_events(1, "c1", "Read", output="file text", raw_input={"path": "a.md"}))
    assert _frames(out) == [
        ("tool_start", {"call_id": "c1", "tool": "Read", "args": '{"path": "a.md"}'}),
        ("tool_end", {"call_id": "c1", "tool": "Read", "ok": True, "result_summary": "file text"}),
    ]


def test_tool_failed_status_and_summary_truncation_and_args_cap():
    t = _t()
    events = tool_events(1, "c1", "Bash", output="x" * 500, status="failed", raw_input="y" * 900)
    out = _frames(_feed(t, events))
    assert out[0][1]["args"] == "y" * 400
    assert out[1][1]["ok"] is False and len(out[1][1]["result_summary"]) == 200


def test_tool_updated_is_ignored_and_name_falls_back_to_title():
    t = _t()
    out = _feed(
        t,
        [
            ev(
                "tool.started",
                1,
                tool_call_id="c",
                title="读取文件",
                kind="read",
                status="pending",
                tool_name=None,
                raw_input=None,
                locations=[],
                content=[],
            ),
            ev("tool.updated", 2, tool_call_id="c", status="in_progress"),
        ],
    )
    assert _frames(out) == [("tool_start", {"call_id": "c", "tool": "读取文件", "args": ""})]


def test_ignored_events_produce_no_frames():
    t = _t()
    out = _feed(
        t,
        [
            ev("usage", 1, context_used=10, context_size=100),  # 上下文占用：不是回合用量
            ev("plan", 2, entries=[]),
            ev("mode.changed", 3, mode_id="plan"),
            ev("config.changed", 4, config_options=[]),
            ev("commands", 5, commands=[]),
            ev("session.info", 6, title="x"),
            ev("permission.requested", 7, request_id="r"),
            ev("permission.resolved", 8, request_id="r", decision="allow"),
            ev("session.context_reset", 9, reason="imported"),
            ev("raw", 10, update={}),
        ],
    )
    assert out == []
    assert not t.finished


# ---- 引用 ----


async def test_search_tool_emits_citations_before_tool_end():
    t = _t()
    redis = FakeRedis()
    out = _feed(
        t,
        tool_events(1, "c1", SEARCH, output=json.dumps(SEARCH_RESULT, ensure_ascii=False)),
    )
    assert isinstance(out[1], CitationsLookup) and out[1].batch_id == "b1"
    assert [type(o).__name__ for o in out] == ["Frame", "CitationsLookup", "Frame"]
    frame = await t.resolve_citations(out[1], redis)
    assert frame is not None and frame.event == "citations"
    assert frame.data["citations"][0]["unit_id"] == "u1"
    assert frame.data["graph_evidence"] == SEARCH_RESULT["graph_evidence"]


async def test_citations_prefer_redis_side_channel_and_fall_back_to_inline():
    batch = {
        "ref_items": [{"n": 1, "unit_id": "u-redis", "asset_id": "a"}],
        "graph_evidence": [],
    }
    redis = FakeRedis({"agent:citations:sess-hex:b1": json.dumps(batch)})
    t = _t()
    out = _feed(t, tool_events(1, "c1", SEARCH, output=json.dumps(SEARCH_RESULT)))
    frame = await t.resolve_citations(out[1], redis)
    # Redis 旁路先入；inline 的 u1 是另一个 unit，也并入（去重按 unit_id）
    assert [c["unit_id"] for c in frame.data["citations"]] == ["u-redis", "u1"]

    # Redis 无旁路：inline 兜底
    t = _t()
    out = _feed(t, tool_events(1, "c1", SEARCH, output=json.dumps(SEARCH_RESULT)))
    frame = await t.resolve_citations(out[1], FakeRedis())
    assert [c["unit_id"] for c in frame.data["citations"]] == ["u1"]


async def test_truncated_search_output_still_resolves_via_redis_batch_id():
    """输出过大被网关截断（JSON 不完整）：正则抓 batch_id，引用靠 Redis 旁路。"""
    broken = '{"batch_id": "b9", "ref_items": [{"n": 1, "unit_id": "u9", "text_sn'
    redis = FakeRedis(
        {
            "agent:citations:sess-hex:b9": json.dumps(
                {"ref_items": [{"n": 1, "unit_id": "u9"}], "graph_evidence": []}
            )
        }
    )
    t = _t()
    out = _feed(t, tool_events(1, "c1", SEARCH, output=broken))
    assert out[1] == CitationsLookup("b9", [], [])
    frame = await t.resolve_citations(out[1], redis)
    assert frame.data["citations"][0]["unit_id"] == "u9"


async def test_search_with_no_results_emits_no_citations_frame():
    t = _t()
    out = _feed(t, tool_events(1, "c1", SEARCH, output='{"batch_id":"","ref_items":[]}'))
    assert await t.resolve_citations(out[1], FakeRedis()) is None


def test_search_tool_detected_by_suffix_for_other_prefixes():
    t = _t()
    out = _feed(t, tool_events(1, "c1", "mcp__other__search_knowledge", output="{}"))
    assert any(isinstance(o, CitationsLookup) for o in out)


# ---- 终态 ----


def test_turn_completed_emits_usage_status_and_done():
    t = _t()
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 300,
        "cache_write_tokens": 100,
        "thought_tokens": 5,
        "total_tokens": 525,
    }
    out = _feed(
        t,
        [
            ev("message.delta", 1, text="答案[1]和[7]"),
            ev("turn.completed", 2, stop_reason="end_turn", usage=usage, final_text="答案[1]和[7]"),
        ],
    )
    frames = _frames(out)
    assert [f[0] for f in frames] == ["delta", "usage", "done"]
    assert frames[1][1] == {
        "cache_read_tokens": 300,
        "cache_write_tokens": 100,
        "uncached_input_tokens": 100,
        "output_tokens": 20,
        "reasoning_tokens": 5,
        "steps": 1,
        "cache_hit_rate": 0.6,
    }
    done = frames[2][1]
    assert done["finish_reason"] == "end_turn" and done["message_id"] == "msg-1"
    assert done["usage"]["turns"] == 1 and done["usage"]["output_tokens"] == 20
    assert t.finished and t.finish_reason == "end_turn"


def test_done_strips_out_of_range_citation_markers():
    t = _t()
    out = _feed(
        t,
        [
            ev("message.delta", 1, text="见[1]，另见[7]"),
            ev("turn.completed", 2, stop_reason="end_turn", usage=None, final_text=""),
        ],
    )
    # 没有任何引用 → 所有 [n] 都越界被剔除
    assert _frames(out)[-1][1]["answer"] == "见，另见"


async def test_done_keeps_valid_citation_markers():
    t = _t()
    out = _feed(t, tool_events(1, "c1", SEARCH, output=json.dumps(SEARCH_RESULT)))
    await t.resolve_citations(out[1], FakeRedis())
    out = _feed(
        t,
        [
            ev("message.delta", 3, text="依据[1]，不是[2]"),
            ev("turn.completed", 4, stop_reason="end_turn", usage=None),
        ],
    )
    done = _frames(out)[-1][1]
    assert done["answer"] == "依据[1]，不是"
    assert done["citations"][0]["unit_id"] == "u1"
    assert done["graph_evidence"] == SEARCH_RESULT["graph_evidence"]


def test_dsh_null_usage_gives_empty_usage_object_and_no_usage_frame():
    t = _t()
    out = _feed(
        t,
        [
            ev("message.delta", 1, text="hi"),
            ev("turn.completed", 2, stop_reason="end_turn", usage=None, final_text="hi"),
        ],
    )
    names = [f[0] for f in _frames(out)]
    assert "usage" not in names
    assert _frames(out)[-1][1]["usage"] == {}


def test_cancelled_turn_emits_cancelled_status_before_done():
    t = _t()
    out = _feed(t, [ev("turn.completed", 1, stop_reason="cancelled", usage=None, final_text="")])
    assert [f[0] for f in _frames(out)] == ["status", "done"]
    assert _frames(out)[0][1] == {"status": "cancelled"}
    assert _frames(out)[1][1]["finish_reason"] == "cancelled"


def test_final_text_backfills_answer_when_no_delta_seen():
    t = _t()
    out = _feed(t, [ev("turn.completed", 5, stop_reason="end_turn", final_text="只有终态汇总")])
    assert _frames(out)[-1][1]["answer"] == "只有终态汇总"


@pytest.mark.parametrize(
    ("code", "needle"),
    [
        ("turn_timeout", "超时"),
        ("runtime_crashed", "异常中断"),
        ("gateway_restarted", "服务重启"),
        ("agent_unavailable", "暂不可用"),
        ("quota_exceeded", "配额"),
        ("unsupported", "不支持"),
        ("agent_error", "模型调用失败"),
    ],
)
def test_turn_failed_emits_error_then_done(code, needle):
    t = _t()
    out = _feed(t, [ev("turn.failed", 1, code=code, message="底层原因 abc", retryable=False)])
    frames = _frames(out)
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == code and needle in frames[0][1]["message"]
    assert frames[1][1]["finish_reason"] == "error" and frames[1][1]["usage"] == {}
    assert t.finished and t.finish_reason == "error"


def test_failure_message_keeps_underlying_reason_for_unknown_codes():
    assert "401 invalid api key" in failure_message("agent_error", "401 invalid api key")
    assert failure_message("agent_error", "") == "模型调用失败"


def test_sse_error_frame_ends_with_error_and_done():
    """hijack 之后的服务端错误帧 `event: error`（data={"error":{code,message}}）。"""
    t = _t()
    out = _feed(t, [("error", {"error": {"code": "internal", "message": "事件落库失败"}})])
    frames = _frames(out)
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == "internal" and "事件落库失败" in frames[0][1]["message"]


def test_events_after_terminal_and_duplicate_seq_are_ignored():
    t = _t()
    _feed(t, [ev("message.delta", 1, text="a"), ev("message.delta", 1, text="DUP")])
    assert t.answer == "a"  # 重连回放的重叠
    _feed(t, [ev("turn.completed", 2, stop_reason="end_turn")])
    assert _feed(t, [ev("message.delta", 3, text="late"), ev("turn.failed", 4, code="x")]) == []
    assert t.answer == "a"


def test_fail_for_interrupted_stream_is_idempotent():
    t = _t()
    first = t.fail("stream_interrupted", "与网关的连接中断")
    assert [f.event for f in first if isinstance(f, Frame)] == ["error", "done"]
    assert t.fail("x", "y") == []


# ---- 载荷解析 ----


def test_tool_text_extraction_variants():
    assert content_text([{"type": "content", "content": {"type": "text", "text": "A"}}]) == "A"
    assert content_text([{"type": "diff", "path": "a"}]) == ""
    assert content_text(None) == ""
    assert raw_output_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert raw_output_text({"text": "t"}) == "t"
    assert json.loads(raw_output_text({"k": 1})) == {"k": 1}
    assert raw_output_text(None) == ""


def test_search_payload_prefers_content_then_raw_output():
    raw = json.dumps(SEARCH_RESULT)
    batch, items, _ = search_payload([], raw)  # content 空，raw_output 是字符串
    assert batch == "b1" and items
    batch, items, _ = search_payload(
        [{"type": "content", "content": {"type": "text", "text": raw}}], None
    )
    assert batch == "b1" and items
    assert search_payload([], "not json") == ("", [], [])
