"""网关 SSE 事件 → loomvec SSE 帧（前端契约不变，见 apps/web/src/agent/agent.ts）。

纯状态机：`TurnTranslator.feed(event, data)` 吃一个网关事件，吐出 0..n 个输出；
不做 IO。检索工具的引用需要读 Redis 旁路，翻译层只吐 `CitationsLookup` 标记，
由调用方 `await translator.resolve_citations(...)` 兑现（保持本类可同步单测）。

翻译表（P2 合同 §3.5）：

    turn.started     → meta {session_id(逻辑), message_id, model, env_id}
    message.delta    → delta {text}
    thought.delta    → reasoning {text}
    tool.started     → tool_start {call_id, tool, args}
    tool.completed   → [citations] + tool_end {call_id, tool, ok, result_summary}
    turn.completed   → [usage] + [status cancelled] + done {...}
    turn.failed      → error {message, code} + done {finish_reason: "error"}
    event: error     → 同 turn.failed（hijack 之后的服务端错误帧）
    其余（permission.* / plan / usage(上下文占用) / raw / tool.updated ...）→ 忽略

注意 `usage` 事件是上下文窗口占用，不是回合用量，**不**映射成 `usage` 帧；
回合用量取 `turn.completed.usage`（dsh 为 null → done.usage 为空对象，不发 usage 帧）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loomvec.agent.citations import (
    SEARCH_TOOL,
    CitationAggregator,
    parse_search_result,
    strip_out_of_range_citations,
)
from loomvec.agent.gateway.usage import usage_to_event
from loomvec.agent.sse import sse_event

MAX_TOOL_SUMMARY = 200
MAX_TOOL_ARGS = 400
_BATCH_ID_RE = re.compile(r'"batch_id"\s*:\s*"([0-9A-Za-z_-]+)"')


@dataclass(frozen=True)
class Frame:
    """一个待发给前端的 SSE 帧。"""

    event: str
    data: dict[str, Any]

    def to_sse(self) -> str:
        return sse_event(self.event, self.data)


@dataclass(frozen=True)
class CitationsLookup:
    """检索工具完成：需要按 batch_id 读 Redis 旁路，并合入 inline 的 ref_items。"""

    batch_id: str
    ref_items: list[dict[str, Any]] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)


Output = Frame | CitationsLookup


# ---------------------------------------------------------------------------
# 工具载荷解析（history 也复用）
# ---------------------------------------------------------------------------


def is_search_tool(name: str | None) -> bool:
    return bool(name) and (name == SEARCH_TOOL or str(name).endswith("__search_knowledge"))


def content_text(content: Any) -> str:
    """ACP `ToolCallContent[]` 里的文本部分：`{type:"content", content:{type:"text", text}}`。"""
    parts: list[str] = []
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict) or item.get("type") != "content":
            continue
        inner = item.get("content")
        if isinstance(inner, dict) and inner.get("type") == "text" and inner.get("text"):
            parts.append(str(inner["text"]))
    return "\n".join(parts)


def raw_output_text(raw: Any) -> str:
    """`raw_output` 可能是字符串 / `[{type:"text",text}]` / 任意 JSON。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and all(isinstance(x, dict) and "text" in x for x in raw) and raw:
        return "\n".join(str(x.get("text") or "") for x in raw)
    if isinstance(raw, dict) and isinstance(raw.get("text"), str):
        return raw["text"]
    return json.dumps(raw, ensure_ascii=False, default=str)


def tool_result_text(content: Any, raw_output: Any) -> str:
    return content_text(content) or raw_output_text(raw_output)


def tool_args_text(raw_input: Any) -> str:
    if raw_input in (None, "", {}, []):
        return ""
    text = raw_input if isinstance(raw_input, str) else json.dumps(raw_input, ensure_ascii=False)
    return text[:MAX_TOOL_ARGS]


def search_payload(content: Any, raw_output: Any) -> tuple[str, list[dict], list[dict]]:
    """检索工具结果 → (batch_id, ref_items, graph_evidence)。

    工具返回体是 JSON；输出过大被网关截断时 JSON 不完整，退回正则抓 batch_id，
    引用由 Redis 旁路兜底。
    """
    texts = [content_text(content), raw_output_text(raw_output)]
    for text in texts:
        batch_id, items, evidence = parse_search_result(text)
        if batch_id or items:
            return batch_id, items, evidence
    for text in texts:
        m = _BATCH_ID_RE.search(text)
        if m:
            return m.group(1), [], []
    return "", [], []


_FAILURE_TEXT = {
    "turn_timeout": "回答超时，已中止，请缩小问题范围后重试",
    "runtime_crashed": "智能体运行时异常中断，请重试",
    "gateway_restarted": "服务重启导致本次回答中断，请重试",
    "agent_unavailable": "智能体运行环境暂不可用，请稍后重试",
    "quota_exceeded": "已达到配额上限，请稍后再试",
}


def failure_message(code: str, message: str) -> str:
    """`turn.failed` / `event: error` → 面向用户的文案（底层原因尽量透出）。"""
    base = _FAILURE_TEXT.get(code)
    if base:
        return f"{base}（{message[:200]}）" if message and code != "turn_timeout" else base
    if code == "unsupported":
        return f"当前智能体不支持该输入：{message[:300]}"
    return f"模型调用失败：{message[:300]}" if message else "模型调用失败"


# ---------------------------------------------------------------------------
# 翻译器
# ---------------------------------------------------------------------------


class TurnTranslator:
    """一个回合的事件翻译状态机。"""

    def __init__(self, *, session_id: str, message_id: str, model: str, env_id: str) -> None:
        self.session_id = session_id  # 逻辑会话（agent_session.id.hex）
        self.message_id = message_id
        self.model = model
        self.env_id = env_id
        self.aggregator = CitationAggregator()
        self.answer_parts: list[str] = []
        self.finished = False
        self.finish_reason: str | None = None
        self.last_seq = 0
        self._tools: dict[str, str] = {}
        self._started = False

    # ---- 对外 ----

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)

    def feed(self, event: str, data: dict[str, Any]) -> list[Output]:
        if self.finished:
            return []
        seq = data.get("seq")
        if isinstance(seq, int):
            if seq <= self.last_seq:
                return []  # 重连回放与实时之间的重叠
            self.last_seq = seq
        handler = getattr(self, "_on_" + event.replace(".", "_"), None)
        return handler(data) if handler else []

    def fail(self, code: str, message: str) -> list[Output]:
        """终止回合（流异常中断 / 网关错误帧）：error + done(finish_reason=error)。"""
        if self.finished:
            return []
        self.finished = True
        self.finish_reason = "error"
        return [
            Frame("error", {"message": failure_message(code, message), "code": code}),
            self._done({}, "error"),
        ]

    async def resolve_citations(self, lookup: CitationsLookup, redis: Any) -> Frame | None:
        """兑现检索引用：Redis 旁路优先，inline ref_items 兜底（unit_id 去重，编号沿用首现）。"""
        await self.aggregator.load_batch(redis, self.session_id, lookup.batch_id)
        if lookup.ref_items:
            self.aggregator.add_batch(lookup.ref_items, lookup.graph_evidence)
        if self.aggregator.citations:
            return Frame("citations", self.aggregator.to_event())
        return None

    # ---- 事件处理 ----

    def _on_turn_started(self, _data: dict[str, Any]) -> list[Output]:
        if self._started:
            return []
        self._started = True
        return [
            Frame(
                "meta",
                {
                    "session_id": self.session_id,
                    "message_id": self.message_id,
                    "model": self.model,
                    "env_id": self.env_id,
                },
            )
        ]

    def _on_message_delta(self, data: dict[str, Any]) -> list[Output]:
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return []  # 非文本内容块（image 等）前端无承载
        self.answer_parts.append(text)
        return [Frame("delta", {"text": text})]

    def _on_thought_delta(self, data: dict[str, Any]) -> list[Output]:
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return []
        return [Frame("reasoning", {"text": text})]

    def _on_tool_started(self, data: dict[str, Any]) -> list[Output]:
        call_id = str(data.get("tool_call_id") or "")
        name = str(data.get("tool_name") or data.get("title") or "")
        self._tools[call_id] = name
        return [
            Frame(
                "tool_start",
                {"call_id": call_id, "tool": name, "args": tool_args_text(data.get("raw_input"))},
            )
        ]

    def _on_tool_completed(self, data: dict[str, Any]) -> list[Output]:
        call_id = str(data.get("tool_call_id") or "")
        name = str(data.get("tool_name") or self._tools.get(call_id) or data.get("title") or "")
        self._tools[call_id] = name
        content, raw_output = data.get("content"), data.get("raw_output")
        out: list[Output] = []
        if is_search_tool(name):
            batch_id, items, evidence = search_payload(content, raw_output)
            out.append(CitationsLookup(batch_id, items, evidence))
        out.append(
            Frame(
                "tool_end",
                {
                    "call_id": call_id,
                    "tool": name,
                    "ok": data.get("status") != "failed",
                    "result_summary": tool_result_text(content, raw_output)[:MAX_TOOL_SUMMARY],
                },
            )
        )
        return out

    def _on_turn_completed(self, data: dict[str, Any]) -> list[Output]:
        self.finished = True
        stop_reason = str(data.get("stop_reason") or "")
        self.finish_reason = stop_reason or None
        usage = usage_to_event(data.get("usage"))
        if not self.answer_parts and isinstance(data.get("final_text"), str):
            self.answer_parts.append(data["final_text"])  # 增量一帧没收到时用终态汇总兜底
        out: list[Output] = []
        if usage:
            out.append(Frame("usage", usage))
        if stop_reason == "cancelled":
            out.append(Frame("status", {"status": "cancelled"}))
        out.append(self._done({**usage, "turns": 1} if usage else {}, stop_reason or None))
        return out

    def _on_turn_failed(self, data: dict[str, Any]) -> list[Output]:
        return self.fail(str(data.get("code") or "internal"), str(data.get("message") or ""))

    def _on_error(self, data: dict[str, Any]) -> list[Output]:
        err = data.get("error") if isinstance(data.get("error"), dict) else data
        return self.fail(str(err.get("code") or "internal"), str(err.get("message") or ""))

    # ---- 辅助 ----

    def _done(self, usage: dict[str, Any], finish_reason: str | None) -> Frame:
        answer = strip_out_of_range_citations(self.answer, self.aggregator.citations)
        return Frame(
            "done",
            {
                "message_id": self.message_id,
                "answer": answer,
                "citations": self.aggregator.citations,
                "graph_evidence": self.aggregator.graph_evidence,
                "usage": usage,
                "finish_reason": finish_reason,
            },
        )
