"""P5 agent 服务单测（§15.3）：transcript 解析、usage 折叠、citations 聚合、
approvals 矩阵。编码规则已对 dsh TS 实现（format.ts）交叉验证。
"""

from __future__ import annotations

import json
from pathlib import Path

from loomvec.agent.approvals import decide
from loomvec.agent.citations import (
    CitationAggregator,
    parse_search_result,
    strip_out_of_range_citations,
)
from loomvec.agent.sessions.transcript import (
    aggregate_transcript,
    encode_segment,
    parse_transcript,
    physical_gen,
    project_key,
    replay_context,
)
from loomvec.agent.sessions.usage import UsageFold

# ---------------------------------------------------------------------------
# 编码规则（与 dsh format.ts 交叉验证的快照）
# ---------------------------------------------------------------------------


def test_encode_segment_matches_dsh_reference():
    assert encode_segment("abc123-_.") == "abc123-_."
    assert encode_segment("工") == "~5DE5"
    assert encode_segment("~") == "~007E"
    assert encode_segment("a/b") == "a~002Fb"
    assert encode_segment(physical_gen("f" * 32, 3)) == f"{'f' * 32}-0003"


def test_project_key_matches_dsh_reference():
    assert project_key("/Users/x/ws") == "--Users-x-ws--"
    assert project_key("C:\\work\\p") == "--C-work-p--"
    assert project_key("/data/中文名/ws") == "--data-~4E2D~6587~540D-ws--"
    assert project_key("/") == "--root--"
    assert project_key("///a~~b") == "--a~007E~007Eb--"


# ---------------------------------------------------------------------------
# transcript 解析（fixture：单轮 = user → assistant(tool-call) → tool/call →
# tool/result → assistant(text)，与 dsh 事件词汇表一致）
# ---------------------------------------------------------------------------

LOGICAL = "3f2b" * 8  # 32 hex


def _write_fixture(tmp_path: Path, gen: int, lines: list[dict]) -> Path:
    sessions_root = tmp_path / "sessions"
    pdir = sessions_root / project_key(str(tmp_path / "ws"))
    sdir = pdir / encode_segment(physical_gen(LOGICAL, gen))
    sdir.mkdir(parents=True, exist_ok=True)
    log = sdir / "session.v3.jsonl"
    header = {"type": "session", "version": 3, "id": physical_gen(LOGICAL, gen)}
    with log.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for i, ev in enumerate(lines, start=1):
            envelope = {"type": ev["type"], "seq": i, "time": f"2026-09-16T00:00:{i:02d}Z", **ev}
            f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return log


def _fixture_lines(question: str, answer: str, with_search: bool) -> list[dict]:
    lines: list[dict] = [
        {
            "type": "user/message",
            "data": {"message": {"content": [{"type": "text", "text": question}]}},
        }
    ]
    if with_search:
        result = {
            "batch_id": "b1",
            "ref_items": [
                {
                    "n": 1,
                    "unit_id": "u1",
                    "asset_id": "a1",
                    "asset_name": "手册",
                    "locator": {"pages": [3]},
                }
            ],
            "graph_evidence": [{"head": "A", "relation": "uses", "tail": "B"}],
        }
        lines += [
            {
                "type": "assistant/message",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "id": "m1",
                        "content": [
                            {
                                "type": "tool-call",
                                "id": "c1",
                                "name": "mcp__loomvec__search_knowledge",
                            }
                        ],
                    },
                    "usage": {"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 400},
                },
            },
            {
                "type": "tool/call",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "callId": "c1",
                    "name": "mcp__loomvec__search_knowledge",
                    "arguments": "{}",
                },
            },
            {
                "type": "tool/result",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "c1",
                                "content": [
                                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                                ],
                            }
                        ]
                    },
                },
            },
        ]
    lines += [
        {
            "type": "assistant/message",
            "data": {
                "turn": 1,
                "step": 2,
                "message": {
                    "id": "m2",
                    "content": [
                        {"type": "reasoning", "text": "想一想"},
                        {"type": "text", "text": answer},
                    ],
                },
                "usage": {
                    "inputTokens": 50,
                    "outputTokens": 20,
                    "cacheReadTokens": 50,
                    "cacheWriteTokens": 100,
                },
            },
        },
        {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
    ]
    return lines


def test_parse_transcript_roundtrip(tmp_path):
    log = _write_fixture(tmp_path, 0, _fixture_lines("什么是 X？", "X 是 [1] 这样", True))
    messages = parse_transcript(log)
    assert [m.role for m in messages] == ["user", "assistant", "assistant"]
    assistant_tool, assistant_text = messages[1], messages[2]
    assert assistant_tool.tool_calls[0].tool == "mcp__loomvec__search_knowledge"
    assert assistant_tool.tool_calls[0].ref_items[0]["unit_id"] == "u1"
    assert assistant_text.content == "X 是 [1] 这样"
    assert assistant_text.reasoning == "想一想"
    assert assistant_text.finish_reason == "completed"
    # 引用快照挂在含 tool-call 的 assistant 消息上
    assert assistant_tool.citations and assistant_tool.citations[0]["n"] == 1
    assert assistant_tool.graph_evidence == [{"head": "A", "relation": "uses", "tail": "B"}]


def test_aggregate_and_replay_across_generations(tmp_path):
    _write_fixture(tmp_path, 0, _fixture_lines("第一问", "第一答", False))
    _write_fixture(tmp_path, 1, _fixture_lines("第二问", "第二答", False))
    items = aggregate_transcript(tmp_path / "sessions", tmp_path / "ws", LOGICAL)
    assert [i["content"] for i in items] == ["第一问", "第一答", "第二问", "第二答"]
    replay = replay_context(items)
    assert "<user>第一问</user>" in replay and "<assistant>第二答</assistant>" in replay
    assert replay_context([]) == ""


def test_aggregate_ignores_unrelated_dirs(tmp_path):
    _write_fixture(tmp_path, 0, _fixture_lines("q", "a", False))
    # 无关逻辑会话目录（不同前缀）不应混入
    other = tmp_path / "sessions" / project_key(str(tmp_path / "ws")) / "deadbeef-0000"
    other.mkdir(parents=True)
    (other / "session.v3.jsonl").write_text("{}\n", encoding="utf-8")
    items = aggregate_transcript(tmp_path / "sessions", tmp_path / "ws", LOGICAL)
    assert len(items) == 2


# ---------------------------------------------------------------------------
# usage 折叠
# ---------------------------------------------------------------------------


def test_usage_fold_and_cache_hit_rate():
    fold = UsageFold()
    fold.add({"inputTokens": 100, "outputTokens": 10, "cacheReadTokens": 400})
    fold.add(
        {"inputTokens": 50, "outputTokens": 20, "cacheReadTokens": 50, "cacheWriteTokens": 100}
    )
    assert fold.steps == 2
    assert fold.billed_input == 700  # uncached 150 + read 450 + write 100
    assert abs(fold.cache_hit_rate - 450 / 700) < 1e-9
    event = fold.to_event(ttft_ms=120)
    assert event["cache_read_tokens"] == 450 and event["ttft_ms"] == 120
    assert UsageFold().cache_hit_rate == 0.0  # 空数据不除零


# ---------------------------------------------------------------------------
# citations 聚合与 [n] 后处理
# ---------------------------------------------------------------------------


def test_citation_aggregator_dedupe_and_events():
    agg = CitationAggregator()
    agg.add_batch(
        [{"n": 1, "unit_id": "u1"}, {"n": 2, "unit_id": "u2"}],
        [{"head": "A", "relation": "r", "tail": "B"}],
    )
    agg.add_batch(
        [{"n": 1, "unit_id": "u1"}, {"n": 3, "unit_id": "u3"}],  # u1 重复
        [{"head": "A", "relation": "r", "tail": "B"}],  # 证据重复
    )
    assert [c["n"] for c in agg.citations] == [1, 2, 3]
    assert len(agg.graph_evidence) == 1
    assert agg.to_event()["citations"][0]["unit_id"] == "u1"


def test_parse_search_result_and_strip_citations():
    batch_id, refs, ev = parse_search_result(
        json.dumps({"batch_id": "b9", "ref_items": [{"n": 1}], "graph_evidence": []})
    )
    assert batch_id == "b9" and refs == [{"n": 1}] and ev == []
    assert parse_search_result("not json") == ("", [], [])
    citations = [{"n": 1}]
    assert strip_out_of_range_citations("见 [1] 与 [7]", citations) == "见 [1] 与 "
    assert strip_out_of_range_citations("见 [1]", citations) == "见 [1]"


# ---------------------------------------------------------------------------
# approvals 矩阵（P0 fail-closed）
# ---------------------------------------------------------------------------


def test_approval_matrix_p0():
    assert decide("mcp__loomvec__search_knowledge").action == "allow-once"
    assert decide("mcp__loomvec__read_unit").action == "allow-once"
    assert decide("str_replace_editor").action == "allow-once"
    assert decide("bash").action == "reject-once"  # 未知/破坏性 fail-closed
    assert decide("web_search").action == "reject-once"


# ---------------------------------------------------------------------------
# 流式重放计划（dsh 增量补偿）
# ---------------------------------------------------------------------------


def test_replay_plan_extracts_chunks_with_clamped_delays():
    from loomvec.agent.sessions.orchestrator import PromptOrchestrator as O

    plan = O._replay_plan(
        [
            {
                "type": "chunk",
                "chunk": {"type": "block-start", "index": 0, "blockType": "reasoning"},
            },
            {
                "type": "reasoning-chunks",
                "time0": 1000,
                "dt": [43, 10, 0],
                "texts": ["I", " need", " to"],
            },
            {
                "type": "text-chunks",
                "time0": 2000,
                "dt": [8, 12, 500],
                "texts": ["向量", "检索", "是"],
            },
            {"type": "chunk", "chunk": {"type": "block-end"}},
        ]
    )
    assert [e for e, _, _ in plan] == ["reasoning"] * 3 + ["delta"] * 3
    assert [t for _, t, _ in plan] == ["I", " need", " to", "向量", "检索", "是"]
    delays = [round(d * 1000) for _, _, d in plan]
    assert delays == [25, 10, 4, 8, 12, 25]  # clamp 到 [4, 25]ms


def test_replay_plan_budget_compression_and_fallbacks():
    from loomvec.agent.sessions.orchestrator import PromptOrchestrator as O

    big = O._replay_plan(
        [{"type": "text-chunks", "time0": 0, "dt": [30] * 1000, "texts": ["x"] * 1000}]
    )
    assert len(big) == 1000
    assert sum(d for _, _, d in big) <= O._REPLAY_BUDGET_S + 1e-6  # 超预算等比压缩
    assert O._replay_plan(None) == []  # stream 缺失 → 空计划（回退整段帧）
    assert O._replay_plan([{"type": "chunk"}]) == []
