"""网关 `GET /v1/sessions/{id}/messages` 消息视图 → `AgentMessageOut` 形状。

网关视图是扁平序列（user / assistant / thought / tool / system，按发生顺序）。
loomvec 前端按「一问一答」渲染：每个回合折成一条 user + 一条 assistant
（文本按序拼接，与实时流 `delta` 的拼接方式一致；工具调用与引用归到该条 assistant）。
纯函数，无 IO。
"""

from __future__ import annotations

from typing import Any

from loomvec.agent.citations import CitationAggregator, strip_out_of_range_citations
from loomvec.agent.gateway.translate import (
    MAX_TOOL_SUMMARY,
    failure_message,
    is_search_tool,
    search_payload,
    tool_result_text,
)
from loomvec.agent.mentions import split_mentions


def _user_entry(message: dict[str, Any]) -> dict[str, Any]:
    blocks = message.get("content")
    texts = [
        str(b.get("text") or "")
        for b in (blocks if isinstance(blocks, list) else [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    content, mentions = split_mentions("\n".join(texts))
    return {
        "role": "user",
        "content": content.strip(),
        "mentions": mentions,
        "citations": [],
        "graph_evidence": [],
        "tool_calls": [],
        "created_at": message.get("ts"),
    }


class _AssistantTurn:
    """一个回合内 assistant 侧的累加器。"""

    def __init__(self, ts: str | None) -> None:
        self.ts = ts
        self.text: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.aggregator = CitationAggregator()
        self.failure: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.text or self.tool_calls or self.failure)

    def entry(self) -> dict[str, Any]:
        answer = strip_out_of_range_citations("".join(self.text), self.aggregator.citations)
        return {
            "role": "assistant",
            "content": answer or (self.failure or ""),
            "mentions": [],
            "citations": self.aggregator.citations,
            "graph_evidence": self.aggregator.graph_evidence,
            "tool_calls": self.tool_calls,
            "created_at": self.ts,
        }


def fold_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """网关消息视图 → loomvec 历史条目（旧 → 新）。"""
    out: list[dict[str, Any]] = []
    current: _AssistantTurn | None = None
    current_turn_id: str | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and not current.empty:
            out.append(current.entry())
        current = None

    for m in messages:
        role = m.get("role")
        turn_id = m.get("turn_id")
        if role == "user":
            flush()
            current_turn_id = turn_id
            out.append(_user_entry(m))
            continue
        if role not in ("assistant", "tool", "system"):
            continue  # thought：AgentMessageOut 无 reasoning 字段
        if role == "system" and m.get("kind") != "turn_failed":
            continue  # context_reset 等：不进对话流
        if current is not None and turn_id != current_turn_id:
            flush()
        current_turn_id = turn_id
        if current is None:
            current = _AssistantTurn(m.get("ts"))
        if role == "assistant":
            current.text.append(str(m.get("text") or ""))
        elif role == "tool":
            name = str(m.get("tool_name") or m.get("title") or "")
            summary = tool_result_text(m.get("content"), m.get("raw_output"))
            current.tool_calls.append(
                {
                    "call_id": str(m.get("tool_call_id") or ""),
                    "tool": name,
                    "ok": m.get("status") != "failed",
                    "result_summary": summary[:MAX_TOOL_SUMMARY],
                }
            )
            if is_search_tool(name):
                _, items, evidence = search_payload(m.get("content"), m.get("raw_output"))
                if items:
                    current.aggregator.add_batch(items, evidence)
        else:  # turn_failed
            detail = m.get("detail") if isinstance(m.get("detail"), dict) else {}
            current.failure = failure_message(
                str(detail.get("code") or "internal"), str(detail.get("message") or "")
            )
    flush()
    return out
