"""dsh 会话 JSONL 解析（14 文档 §5.6；唯一感知 dsh 会话格式的模块）。

物理布局（third_party/session-persistence-jsonl/src/format.ts 核实）：
    <DSH_HOME>/sessions/--{projectKey(cwd)}--/{encodeSegment(session_id)}/session.v3.jsonl(.zstd)

- 首行 header（type=session），其后每行一个事件 envelope {type, seq, time, data}；
- 逻辑会话（agent_session.id）与物理会话解耦：物理 id = `{logical_hex}-{gen:04d}`，
  目录按 gen 排序聚合即完整历史（runtime 重启后开新物理段，见 orchestrator）。

词汇表（SurfaceEventType）：user/message、assistant/message、system/message、
tool/result + tool/call（按 callId 配对）；轮次边界 turn/end（data.reason.kind）。
assistant/message.data = {turn, step, message: {content: ContentBlock[]}, usage?}；
ContentBlock 变体：text / reasoning / image / file / tool-call / tool-result。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SAFE = re.compile(r"[A-Za-z0-9._-]")
_LOG_NAME = re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl(?:\.zstd)?$")


def encode_segment(raw: str) -> str:
    """port of format.ts encodeSegment：非安全字符 → ~XXXX（大写 hex）。"""
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for ch in raw:
        if ch != "~" and _SAFE.fullmatch(ch):
            out.append(ch)
        else:
            out.append("~" + format(ord(ch), "04X"))
    return "".join(out)


def project_key(cwd: str) -> str:
    """port of format.ts projectKey：路径分隔折叠为 -，截 251，包 --…--。"""
    readable = []
    separator_run = False
    for ch in cwd:
        if ch in "/\\:":
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif ch != "~" and _SAFE.fullmatch(ch):
            readable.append(ch)
            separator_run = False
        else:
            readable.append("~" + format(ord(ch), "04X"))
            separator_run = False
    slug = re.sub(r"^-+", "", "".join(readable)) or "root"
    return f"--{slug[:251]}--"


def physical_gen(logical_hex: str, n: int) -> str:
    """物理会话 id：{logical}-{gen:04d}（hex+连字符安全，编码不变）。"""
    return f"{logical_hex}-{n:04d}"


def _project_dir(sessions_root: Path, workspace: Path) -> Path:
    return sessions_root / project_key(str(workspace))


def physical_session_files(sessions_root: Path, workspace: Path, logical_hex: str) -> list[Path]:
    """该逻辑会话的全部物理 JSONL，按 gen 升序（完整历史）。"""
    pdir = _project_dir(sessions_root, workspace)
    if not pdir.is_dir():
        return []
    prefix = f"{logical_hex}-"
    out: list[tuple[int, Path]] = []
    for entry in pdir.iterdir():
        if not entry.is_dir() or not entry.name.startswith(prefix):
            continue
        suffix = entry.name[len(prefix) :]
        if not suffix.isdigit():
            continue
        logs = sorted(
            p for p in entry.iterdir() if _LOG_NAME.match(p.name) and not p.name.endswith(".zstd")
        )
        if logs:
            out.append((int(suffix), logs[0]))
    return [p for _, p in sorted(out)]


def next_physical_gen(sessions_root: Path, workspace: Path, logical_hex: str) -> int:
    """下一个可用物理段号（0 = 全新逻辑会话）。"""
    return len(physical_session_files(sessions_root, workspace, logical_hex))


# ---------------------------------------------------------------------------
# 解析产物
# ---------------------------------------------------------------------------


@dataclass
class ToolCallEntry:
    call_id: str
    tool: str
    args: str = ""
    ok: bool = True
    result_summary: str = ""
    ref_items: list[dict[str, Any]] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TranscriptMessage:
    """面向 UI/重放的会话消息（user/assistant 文本 + 工具摘要 + 引用快照）。"""

    role: str  # user | assistant
    content: str
    reasoning: str = ""
    tool_calls: list[ToolCallEntry] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    message_id: str = ""
    created_at: str = ""
    turn: int = -1
    finish_reason: str = ""


_SEARCH_TOOL = "mcp__loomvec__search_knowledge"


def _block_text(blocks: list[Any], kind: str) -> str:
    parts = []
    for b in blocks or []:
        if isinstance(b, dict) and b.get("type") == kind:
            parts.append(str(b.get("text") or ""))
    return "".join(parts)


# dsh 以 user/message 形式注入的内部上下文（sdk profile 实测）：
# AGENTS.md 提醒 / 运行时快照 / 技能清单 —— 事实源保留，UI 与重放剔除
_INTERNAL_USER_PREFIXES = (
    "<system-reminder>",
    "Current runtime context.",
)


def _is_internal_user_message(content: str) -> bool:
    stripped = content.lstrip()
    return any(stripped.startswith(p) for p in _INTERNAL_USER_PREFIXES)


def _summarize_tool_result(blocks: list[Any]) -> tuple[str, list[dict], list[dict]]:
    """工具结果文本摘要 + 检索类工具的 ref_items/graph_evidence（内联解析）。"""
    texts: list[str] = []
    ref_items: list[dict] = []
    evidence: list[dict] = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool-result":
            inner = b.get("content") or []
            texts.append(_block_text(inner, "text") if isinstance(inner, list) else str(inner))
        elif b.get("type") == "text":
            texts.append(str(b.get("text") or ""))
    summary = "\n".join(t for t in texts if t)[:400]
    raw = texts[0] if texts else ""
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            ref_items = list(payload.get("ref_items") or [])
            evidence = list(payload.get("graph_evidence") or [])
    except (ValueError, TypeError):
        pass
    return summary, ref_items, evidence


def parse_transcript(path: Path) -> list[TranscriptMessage]:
    """解析单个物理 JSONL → UI 消息序列（含工具调用归属轮次）。"""
    messages: list[TranscriptMessage] = []
    pending_tools: dict[str, ToolCallEntry] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue  # torn tail（崩溃残留），容忍
        etype = envelope.get("type")
        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            continue
        if etype == "user/message":
            message = data.get("message") if isinstance(data.get("message"), dict) else data
            content = _block_text(message.get("content"), "text")
            if not content.strip():
                continue  # 纯附件/内部消息（如 replay 注入头）不进 UI
            if _is_internal_user_message(content):
                continue  # dsh 内部注入（AGENTS.md 提示/运行时快照/技能清单）不进 UI
            messages.append(
                TranscriptMessage(
                    role="user",
                    content=content,
                    created_at=str(envelope.get("time") or ""),
                )
            )
        elif etype == "assistant/message":
            message = data.get("message") or {}
            blocks = message.get("content") or []
            tool_calls = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool-call":
                    entry = ToolCallEntry(
                        call_id=str(b.get("id") or ""),
                        tool=str(b.get("name") or ""),
                        args=str(b.get("arguments") or ""),
                    )
                    pending_tools[entry.call_id] = entry
                    tool_calls.append(entry)
            usage = data.get("usage") or {}
            messages.append(
                TranscriptMessage(
                    role="assistant",
                    content=_block_text(blocks, "text"),
                    reasoning=_block_text(blocks, "reasoning"),
                    tool_calls=tool_calls,
                    usage=dict(usage),
                    message_id=str(message.get("id") or ""),
                    created_at=str(envelope.get("time") or ""),
                    turn=int(data.get("turn") or -1),
                )
            )
        elif etype == "tool/call":
            call_id = str(data.get("callId") or "")
            if call_id and call_id not in pending_tools:
                pending_tools[call_id] = ToolCallEntry(
                    call_id=call_id, tool=str(data.get("name") or "")
                )
            if call_id:
                pending_tools[call_id].args = str(data.get("arguments") or "")
        elif etype == "tool/result":
            message = data.get("message") or {}
            blocks = message.get("content") or []
            call_id = ""
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool-result":
                    call_id = str(b.get("toolCallId") or "")
            entry = pending_tools.pop(call_id, None)
            if entry is None:
                continue
            entry.ok = not bool(data.get("error"))
            summary, ref_items, evidence = _summarize_tool_result(blocks)
            entry.result_summary = summary
            entry.ref_items = ref_items
            entry.graph_evidence = evidence
            _attach_tool_entry(messages, entry)
        elif etype == "turn/end":
            reason = data.get("reason") or {}
            for m in reversed(messages):
                if m.role == "assistant" and m.turn == int(data.get("turn") or -1):
                    m.finish_reason = str(reason.get("kind") or "")
                    break
    return messages


def _attach_tool_entry(messages: list[TranscriptMessage], entry: ToolCallEntry) -> None:
    """工具结果回填到最近一条 assistant 消息（其 content 含对应 tool-call 块）。"""
    for m in reversed(messages):
        if m.role == "assistant":
            m.tool_calls = [t for t in m.tool_calls if t.call_id != entry.call_id]
            m.tool_calls.append(entry)
            if entry.tool == _SEARCH_TOOL and entry.ref_items:
                m.citations = list(entry.ref_items)
                m.graph_evidence = list(entry.graph_evidence)
            return


def aggregate_transcript(sessions_root: Path, workspace: Path, logical_hex: str) -> list[dict]:
    """聚合逻辑会话的全部物理段 → GET messages API 的条目结构。"""
    out: list[dict] = []
    for path in physical_session_files(sessions_root, workspace, logical_hex):
        for m in parse_transcript(path):
            out.append(
                {
                    "role": m.role,
                    "content": m.content,
                    "citations": m.citations,
                    "graph_evidence": m.graph_evidence,
                    "tool_calls": [
                        {
                            "call_id": t.call_id,
                            "tool": t.tool,
                            "ok": t.ok,
                            "result_summary": t.result_summary[:200],
                        }
                        for t in m.tool_calls
                    ],
                    "created_at": m.created_at or None,
                }
            )
    return out


def replay_context(messages: list[dict], max_messages: int = 20, max_chars: int = 8000) -> str:
    """历史重放前缀（runtime 重启后新物理段注入；14 文档 §5.1 回收-恢复路径）。"""
    recent = [m for m in messages if m.get("content")][-max_messages:]
    lines: list[str] = []
    total = 0
    for m in recent:
        tag = "user" if m["role"] == "user" else "assistant"
        text = str(m["content"])
        lines.append(f"<{tag}>{text}</{tag}>")
        total += len(text)
    body = "\n".join(lines)
    if total > max_chars:  # 超限从最旧截断（保尾部）
        body = body[-max_chars:]
    if not body:
        return ""
    return (
        "以下是本会话此前的对话记录（系统重启后恢复上下文，历史以旧到新排列）：\n"
        f"{body}\n"
        "请基于以上历史继续本会话。"
    )
