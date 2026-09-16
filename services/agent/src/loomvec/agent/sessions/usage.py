"""usage 折叠：步级 TokenUsage → 轮次/会话统计 + cache_hit_rate（14 文档 §5.8）。

dsh TokenUsage（packages/llm/llm/src/types.ts:162-176 核实）：
inputTokens = **仅未缓存输入**；cacheReadTokens + cacheWriteTokens + inputTokens
= 计费输入合计。cache_hit_rate = cache_read / (cache_read + cache_write + uncached)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UsageFold:
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    steps: int = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.steps += 1
        self.uncached_input_tokens += int(usage.get("inputTokens") or 0)
        self.cache_read_tokens += int(usage.get("cacheReadTokens") or 0)
        self.cache_write_tokens += int(usage.get("cacheWriteTokens") or 0)
        self.output_tokens += int(usage.get("outputTokens") or 0)
        self.reasoning_tokens += int(usage.get("reasoningTokens") or 0)

    @property
    def billed_input(self) -> int:
        return self.cache_read_tokens + self.cache_write_tokens + self.uncached_input_tokens

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_read_tokens / self.billed_input if self.billed_input else 0.0

    def to_event(self, ttft_ms: int | None = None) -> dict[str, Any]:
        return {
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "steps": self.steps,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            **({"ttft_ms": ttft_ms} if ttft_ms is not None else {}),
        }


@dataclass
class TurnStats:
    """一轮对话的实时折叠（SSE usage 事件与 done 汇总共用）。"""

    usage: UsageFold = field(default_factory=UsageFold)
    turns: int = 0  # dsh turn/end 计数（多步轮次通常为 1）
    ttft_ms: int | None = None  # 首个 assistant/message 到达耗时
    llm_text_chars: int = 0
