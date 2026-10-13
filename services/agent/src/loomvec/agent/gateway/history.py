"""网关 `GET /v1/sessions/{id}/messages` 消息视图 → `AgentMessageOut` 形状。

网关视图是扁平序列（user / assistant / thought / tool / system，按发生顺序）。
loomvec 前端按「一问一答」渲染：每个回合折成一条 user + 一条 assistant
（文本按序拼接，与实时流 `delta` 的拼接方式一致；工具调用与引用归到该条 assistant）。
纯函数，无 IO。
"""

from __future__ import annotations

import re
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

# 导入的历史会话首次继续对话时，agent 看不到导入的历史（网关语义）：提问前把此前的对话
# 以文字前缀补给它。前缀只给模型看，历史展示时剔除。
REPLAY_HEADER = "以下是本会话此前的对话记录（系统重启后恢复上下文，历史以旧到新排列）："
REPLAY_FOOTER = "请基于以上历史继续本会话。"
# 贪婪到最后一个 FOOTER：旧 orchestrator 的重放内容取自带前缀的用户消息，重启多次的会话里
# 重放前缀是嵌套的（内层也带 FOOTER）。代价：用户正文自己含 FOOTER 字样时会多吃一段，极罕见
_REPLAY_PREFIX_RE = re.compile(
    rf"\A{re.escape(REPLAY_HEADER)}.*{re.escape(REPLAY_FOOTER)}", re.DOTALL
)
_REPLAY_BLOCK_RE = re.compile(
    rf"\A{re.escape(REPLAY_HEADER)}.*{re.escape(REPLAY_FOOTER)}\Z", re.DOTALL
)


def strip_replay_prefix(text: str) -> str:
    """字符串里开头的重放前缀（旧数据：各 text 块已被直接拼接，只能按文字剥）。"""
    return _REPLAY_PREFIX_RE.sub("", text.lstrip(), count=1).lstrip()


def is_replay_block(text: str) -> bool:
    """整块就是一个重放前缀（新流程：前缀是 prompt 的独立文本块，按块丢弃，不碰正文）。"""
    return bool(_REPLAY_BLOCK_RE.match(text.strip()))


def replay_context(
    items: list[dict[str, Any]], max_messages: int = 20, max_chars: int = 8000
) -> str:
    """`fold_history` 的条目 → 重放前缀（取最近 max_messages 条有文字的消息；超长保尾部）。"""
    recent = [m for m in items if m.get("content")][-max_messages:]
    lines: list[str] = []
    total = 0
    for m in recent:
        tag = "user" if m["role"] == "user" else "assistant"
        text = str(m["content"])
        lines.append(f"<{tag}>{text}</{tag}>")
        total += len(text)
    body = "\n".join(lines)
    if total > max_chars:
        body = body[-max_chars:]
    if not body:
        return ""
    return f"{REPLAY_HEADER}\n{body}\n{REPLAY_FOOTER}"


def _user_entry(message: dict[str, Any]) -> dict[str, Any]:
    blocks = message.get("content")
    texts = [
        str(b.get("text") or "")
        for b in (blocks if isinstance(blocks, list) else [])
        if isinstance(b, dict)
        and b.get("type") == "text"
        and not is_replay_block(str(b.get("text")))
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
