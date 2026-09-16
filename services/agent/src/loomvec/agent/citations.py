"""引用聚合：Redis 旁路 → citations 事件（14 文档 §5.4）。

MCP search_knowledge 把每批结果写 `agent:citations:{session_id}:{batch_id}`
（TTL 1h）；本模块在 tool_end 时解析结果里的 batch_id 读旁路聚合。
旁路缺失时（过期/Redis 故障）回退：直接用工具结果内联的 ref_items
（工具返回体自带，协议升级免疫双保险）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

SEARCH_TOOL = "mcp__loomvec__search_knowledge"


@dataclass
class CitationAggregator:
    """轮次内累计引用表（按 unit_id 去重、编号重排，编号与工具结果一致）。"""

    citations: list[dict[str, Any]] = field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = field(default_factory=list)
    _seen_units: dict[str, int] = field(default_factory=dict)
    _seen_ev: set[tuple[str, str, str]] = field(default_factory=set)

    def add_batch(self, ref_items: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> None:
        for item in ref_items:
            unit_id = str(item.get("unit_id") or "")
            if unit_id in self._seen_units:
                continue  # 去重保序（编号沿用首现值）
            self._seen_units[unit_id] = int(item.get("n") or 0)
            self.citations.append(dict(item))
        for ev in evidence or []:
            key = (str(ev.get("head")), str(ev.get("relation")), str(ev.get("tail")))
            if key not in self._seen_ev:
                self._seen_ev.add(key)
                self.graph_evidence.append(dict(ev))

    async def load_batch(self, redis, session_id: str, batch_id: str) -> None:
        """读 Redis 旁路批次；异常/缺失静默（回退路径在调用方）。"""
        if not redis or not batch_id:
            return
        try:
            raw = await redis.get(f"agent:citations:{session_id}:{batch_id}")
        except Exception:
            return
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        self.add_batch(payload.get("ref_items") or [], payload.get("graph_evidence") or [])

    def to_event(self) -> dict[str, Any]:
        return {"citations": self.citations, "graph_evidence": self.graph_evidence}


def parse_search_result(result_text: str) -> tuple[str, list[dict], list[dict]]:
    """从工具结果文本解析 (batch_id, ref_items, graph_evidence)。"""
    try:
        payload = json.loads(result_text)
    except (ValueError, TypeError):
        return "", [], []
    if not isinstance(payload, dict):
        return "", [], []
    return (
        str(payload.get("batch_id") or ""),
        list(payload.get("ref_items") or []),
        list(payload.get("graph_evidence") or []),
    )


def strip_out_of_range_citations(answer: str, citations: list[dict[str, Any]]) -> str:
    """剔除越界 [n] 引用标记（逻辑迁移自 services/qa.py，14 文档 §5.4-4）。"""
    import re

    valid = {int(c["n"]) for c in citations if str(c.get("n", "")).isdigit()}

    def _replace(match: re.Match) -> str:
        n = int(match.group(1))
        return match.group(0) if n in valid else ""

    return re.sub(r"\[(\d{1,3})\]", _replace, answer)
