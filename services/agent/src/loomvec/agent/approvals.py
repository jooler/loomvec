"""审批策略矩阵（14 文档 §5.5）。

协议事实（third_party 核实）：`--profile sdk` 组合不挂 approval answerer，
dsh 原生 fail-closed——沙箱升级类请求自动拒绝（unavailable → deny），
沙箱内（workspace-write）读写与 MCP 工具默认放行。这恰好构成 P0 矩阵：

    mcp__loomvec__*（只读检索）        → 自动允许（dsh pre-execute 默认 allow）
    workspace 内编辑器/白名单 shell     → 自动允许（沙箱限定范围）
    破坏性 shell / 越界路径 / 网络工具  → 自动拒绝（fail-closed，无审批通道）

本模块承载矩阵的声明式表达（P2 桥接 `session/request_permission` 时，
answerer 直接查这张表），并对 JSONL 中的 approval/* 事件做翻译辅助。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ALLOWED_TOOL_PREFIXES = ("mcp__loomvec__",)
# 沙箱内编辑器工具（dsh-base str_replace_editor 族）
ALLOWED_TOOL_EXACT = frozenset(
    {
        "str_replace_editor",
        "view",
        "edit",
        "write",
        "read",
        "ls",
        "glob",
        "grep",
    }
)


@dataclass(frozen=True)
class ApprovalDecision:
    action: str  # allow-once | reject-once
    reason: str


def decide(tool: str, args_summary: str = "") -> ApprovalDecision:
    """P0 矩阵：未知一律 reject-once（fail-closed 兜底）。"""
    if tool.startswith(ALLOWED_TOOL_PREFIXES):
        return ApprovalDecision("allow-once", "知识检索只读工具")
    if tool in ALLOWED_TOOL_EXACT:
        return ApprovalDecision("allow-once", "workspace 内文件工具（沙箱限定）")
    return ApprovalDecision("reject-once", "P0 策略：该工具需人工审批通道（P2 开放），默认拒绝")


def translate_approval_event(event_type: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """approval/asked|decided 事件 → SSE permission 事件（P0 仅日志观测）。"""
    if event_type not in ("approval/asked", "approval/decided"):
        return None
    return {
        "request_id": str(data.get("requestId") or ""),
        "tool": str(data.get("tool") or ""),
        "reason": str(data.get("reason") or ""),
        "options": ["allow-once", "reject-once"],
        "p0_auto": True,
    }
