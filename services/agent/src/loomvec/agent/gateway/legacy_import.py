"""L2 迁移的纯逻辑：旧 dsh 会话 JSONL → 网关 `sessions:import` 请求体；旧 workspace → 文件清单。

只服务 `scripts/migrate_agent_to_gateway.py`，无 IO 以外的副作用（读本地文件）。
依赖 `sessions/transcript.py` 的 JSONL 解析，随 L3 一起删除。

映射（P2 合同 §6）：
- 一条 user 消息 = 一个回合的开始；其后的 assistant 步（含 reasoning / 文本 / 工具调用）
  按发生顺序折成该回合的 `items`（同一步内 thought → text → tool）；
- @ 文献还原成 prompt 里的提示块（历史回读时 `split_mentions` 据此还原 mentions）；
- 检索工具的 `ref_items` / `graph_evidence` 写进 `raw_output` 的 JSON，历史回读据此还原引用；
- dsh 的 usage 不迁移（网关对 dsh 本就不记用量，导入的历史也不计入配额）。
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loomvec.agent.mentions import build_mention_block
from loomvec.agent.sessions.transcript import (
    ToolCallEntry,
    TranscriptMessage,
    parse_transcript,
    physical_session_files,
)

SEARCH_TOOL = "mcp__loomvec__search_knowledge"
MAX_TURNS = 5000  # 网关限制
MAX_ITEMS_PER_TURN = 2000
UPLOADS_DIR = ".loomvec/uploads"
INSTRUCTIONS_FILE = "AGENTS.md"  # 网关里是 instructions 端点，不走文件接口

_FAILED_KINDS = frozenset({"error", "failed", "crashed"})
_CANCELLED_KINDS = frozenset({"cancelled", "canceled", "aborted", "interrupted"})


def iso_z(value: str | None) -> str | None:
    """JSONL 时间戳 → 网关要求的 `YYYY-MM-DDTHH:MM:SS(.fff)Z`；解析不了返回 None（字段省略）。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _tool_item(entry: ToolCallEntry) -> dict[str, Any]:
    raw_input: Any = entry.args
    if entry.args:
        try:
            raw_input = json.loads(entry.args)
        except ValueError:
            raw_input = entry.args
    if entry.tool == SEARCH_TOOL and entry.ref_items:
        raw_output: Any = json.dumps(
            {"ref_items": entry.ref_items, "graph_evidence": entry.graph_evidence},
            ensure_ascii=False,
        )
    else:
        raw_output = entry.result_summary
    item: dict[str, Any] = {
        "type": "tool",
        "tool_call_id": (entry.call_id or "legacy")[:256],
        "tool_name": entry.tool[:256] or None,
        "title": entry.tool[:4096] or None,
        "status": "completed" if entry.ok else "failed",
        "raw_output": raw_output,
    }
    if raw_input not in ("", None):
        item["raw_input"] = raw_input
    return item


def _step_items(step: TranscriptMessage) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if step.reasoning.strip():
        items.append({"type": "thought", "text": step.reasoning})
    if step.content.strip():
        items.append({"type": "text", "text": step.content})
    items.extend(_tool_item(t) for t in step.tool_calls)
    return items


def _prompt_text(user: TranscriptMessage) -> str:
    text = user.content.strip()
    if user.mentions:
        block = build_mention_block(user.mentions)
        return f"{block}\n\n{text}" if text else block
    return text


def _close_turn(user: TranscriptMessage, steps: list[TranscriptMessage]) -> dict[str, Any]:
    turn: dict[str, Any] = {"prompt": [{"type": "text", "text": _prompt_text(user) or "(空)"}]}
    started = iso_z(user.created_at)
    if started:
        turn["started_at"] = started
    items: list[dict[str, Any]] = []
    for step in steps:
        items.extend(_step_items(step))
    turn["items"] = items[-MAX_ITEMS_PER_TURN:]  # 超限丢开头，保留最终答案
    if steps and (ended := iso_z(steps[-1].created_at)):
        turn["ended_at"] = ended

    kind = next((s.finish_reason for s in reversed(steps) if s.finish_reason), "")
    if kind in _FAILED_KINDS:
        turn["status"] = "failed"
        turn["stop_reason"] = kind
        turn["error"] = {"code": "legacy_error", "message": "历史会话中该回合异常结束"}
    elif kind in _CANCELLED_KINDS or not steps:
        # 没有任何 assistant 输出：被取消 / 进程被杀（旧版取消 = 杀 runtime）
        turn["status"] = "cancelled"
        turn["stop_reason"] = kind or "cancelled"
    else:
        turn["status"] = "completed"
        turn["stop_reason"] = kind or "end_turn"
    return turn


def build_turns(messages: list[TranscriptMessage]) -> list[dict[str, Any]]:
    """消息序列 → 导入回合（每个 user 消息开一个回合；开头孤立的 assistant 步丢弃）。"""
    turns: list[dict[str, Any]] = []
    user: TranscriptMessage | None = None
    steps: list[TranscriptMessage] = []
    for m in messages:
        if m.role == "user":
            if user is not None:
                turns.append(_close_turn(user, steps))
            user, steps = m, []
        elif user is not None:
            steps.append(m)
    if user is not None:
        turns.append(_close_turn(user, steps))
    return turns[-MAX_TURNS:]  # 超限保留最近的


def load_session_messages(
    sessions_root: Path, workspace: Path, logical_hex: str
) -> list[TranscriptMessage]:
    """逻辑会话全部物理段（按 gen 升序）解析后的消息。"""
    out: list[TranscriptMessage] = []
    for path in physical_session_files(sessions_root, workspace, logical_hex):
        out.extend(parse_transcript(path))
    return out


def build_import_body(
    messages: list[TranscriptMessage],
    *,
    import_key: str,
    title: str,
    cwd: str,
    model: str | None,
    created_at: datetime | None,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """`sessions:import` 请求体；没有任何回合（空会话）返回 None。"""
    turns = build_turns(messages)
    if not turns:
        return None
    body: dict[str, Any] = {
        "agent": "dsh",
        "import_key": import_key,
        "title": title[:512],
        "metadata": metadata,
        "turns": turns,
    }
    if cwd:
        body["cwd"] = cwd
    if model:
        body["model"] = model
    if created_at is not None and (ts := iso_z(created_at.isoformat())):
        body["created_at"] = ts
    return body


# ---------------------------------------------------------------------------
# workspace 文件
# ---------------------------------------------------------------------------


def open_regular_nofollow(path: Path):
    """只打开普通文件且不跟随符号链接（防检查与读取之间被换成指向宿主文件的链接）。"""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"不是普通文件：{path.name}")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


@dataclass(frozen=True)
class LocalFile:
    rel: str  # workspace 相对路径（正斜杠）
    path: Path
    size: int


def _keep_dir(rel: str) -> bool:
    """含隐藏段的目录整体跳过；唯一例外是 `.loomvec/uploads`（会话附件）及其上级 `.loomvec`。"""
    if not any(seg.startswith(".") for seg in rel.split("/")):
        return True
    return rel == ".loomvec" or rel == UPLOADS_DIR or rel.startswith(UPLOADS_DIR + "/")


def iter_workspace_files(workspace: Path) -> Iterator[LocalFile]:
    """待迁移的文件：跳过符号链接、隐藏路径（附件目录除外）和根部 AGENTS.md。

    任一路径段以 `.` 开头即视为隐藏——但 `.loomvec/uploads/**` 保留；
    隐藏文件名（以 `.` 开头）一律跳过。
    """
    root = workspace.resolve()
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        kept = []
        for d in sorted(dirnames):
            child = f"{rel_dir}/{d}" if rel_dir else d
            full = here / d
            if full.is_symlink():
                continue
            if not _keep_dir(child):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            full = here / name
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if full.is_symlink() or not full.is_file():
                continue
            if name.startswith("."):
                continue
            if rel == INSTRUCTIONS_FILE:
                continue
            if rel_dir == ".loomvec":  # 只有 uploads 子目录迁移，其余是旧运行时内部文件
                continue
            yield LocalFile(rel=rel, path=full, size=full.stat().st_size)


# ---------------------------------------------------------------------------
# workspace/AGENTS.md
# ---------------------------------------------------------------------------

CARRIED_HEADING = "## 迁移自旧版工作区的自定义内容"


def _body_after_intro(text: str) -> str:
    """去掉前 4 行（标题 + 含租户名 / 用户名的介绍句），其余作为模板正文。"""
    return "\n".join(text.splitlines()[4:]).strip()


def is_unmodified_legacy_agents_md(text: str, legacy_render: str) -> bool:
    """是否仍是旧 `envs.py` 渲染的 workspace 层（租户改名 / 用户改名后头部会不同，只比正文）。"""
    return _body_after_intro(text) == _body_after_intro(legacy_render)


def carried_instructions(rendered_template: str, user_text: str) -> str:
    """新模板（去掉可被升级覆盖的标记行）+ 用户原文附段：保住企业规范层与用户的自定义内容。

    不带模板标记，模板升级不会覆盖它；代价是之后的模板升级不会自动补进来。
    """
    lines = rendered_template.splitlines()
    if lines and lines[0].startswith("<!-- loomvec-template:"):
        lines = lines[1:]
    return "\n".join(lines).rstrip() + f"\n\n{CARRIED_HEADING}\n\n" + user_text.strip() + "\n"
