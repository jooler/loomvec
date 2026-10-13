"""网关回合用量 → 前端 usage 形状（`AgentUsage`，apps/web/src/agent/agent.ts）。

网关 `turn.completed.usage`：`{input_tokens, output_tokens, cache_read_tokens,
cache_write_tokens, thought_tokens, total_tokens}`。其中 `input_tokens` 是**未缓存**输入
（计费输入 = input + cache_read + cache_write）。dsh 不报用量（`usage` 为 null）。
"""

from __future__ import annotations

from typing import Any


def _n(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, int | float) else 0


def usage_to_event(usage: dict[str, Any] | None, *, steps: int = 1) -> dict[str, Any]:
    """单回合用量 → 前端形状；用量缺失（dsh）返回空对象。"""
    if not usage:
        return {}
    return fold_usages([usage], steps=steps)


def fold_usages(usages: list[dict[str, Any] | None], *, steps: int | None = None) -> dict[str, Any]:
    """多回合用量折叠（会话统计）；全部缺失时返回全零结构。"""
    present = [u for u in usages if u]
    cache_read = sum(_n(u, "cache_read_tokens") for u in present)
    cache_write = sum(_n(u, "cache_write_tokens") for u in present)
    uncached = sum(_n(u, "input_tokens") for u in present)
    billed_input = cache_read + cache_write + uncached
    return {
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "uncached_input_tokens": uncached,
        "output_tokens": sum(_n(u, "output_tokens") for u in present),
        "reasoning_tokens": sum(_n(u, "thought_tokens") for u in present),
        "steps": len(present) if steps is None else steps,
        "cache_hit_rate": round(cache_read / billed_input, 4) if billed_input else 0.0,
    }
