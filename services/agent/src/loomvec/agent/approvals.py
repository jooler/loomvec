"""审批策略矩阵（14 文档 §5.5）。

协议事实（third_party 核实）：`--profile sdk` 组合不挂 approval answerer，
dsh 原生 fail-closed——沙箱升级类请求自动拒绝（unavailable → deny），
沙箱内（workspace-write）读写与 MCP 工具默认放行。这恰好构成 P0 矩阵：

    mcp__loomvec__*（只读检索）        → 自动允许（dsh pre-execute 默认 allow）
    workspace 内编辑器/白名单 shell     → 自动允许（沙箱限定范围）
    破坏性 shell / 越界路径 / 网络工具  → 自动拒绝（fail-closed，无审批通道）

本模块承载矩阵的声明式表达：旧 runtime 路径用 `decide()` 查表；经 acp-gateway 的
会话由 `build_permission_policy()` 翻译成网关 `PermissionPolicy`，在建会话时传入
（`default: reject` + 放行规则，语法见网关 docs/api.md「审批策略」）。
**对 dsh 不生效**：docker 下 dsh 以 danger-full-access 运行、不发审批，隔离靠容器。
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


# 网关侧按 ACP 工具类别（kind）放行：读取 / 检索 / 编辑（沙箱=容器，范围限定在 workspace）。
# 其余（execute / fetch / delete / move / other ...）默认拒绝，对应矩阵里「破坏性 shell / 网络工具」
ALLOWED_KINDS = ("read", "search", "edit")


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


def build_permission_policy() -> dict[str, Any]:
    """审批矩阵 → 网关 `PermissionPolicy`（建会话时传入；session 策略不能比 Key 策略更宽）。

    规则按序、第一条匹配生效：`mcp__loomvec__*` 前缀 → 允许；读取 / 检索 / 编辑类
    kind → 允许；矩阵里列出的编辑器工具名 → 允许；其余 `default: reject`。
    """
    rules: list[dict[str, Any]] = [
        {"match": {"tool_name": f"{prefix}*"}, "decision": "allow"}
        for prefix in ALLOWED_TOOL_PREFIXES
    ]
    rules.append({"match": {"kind": list(ALLOWED_KINDS)}, "decision": "allow"})
    rules.extend(
        {"match": {"tool_name": name}, "decision": "allow"} for name in sorted(ALLOWED_TOOL_EXACT)
    )
    return {"default": "reject", "rules": rules}
