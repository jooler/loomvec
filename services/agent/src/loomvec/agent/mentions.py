"""提问 @ 文献的提示块：组装进 prompt（模型可见），历史回读时剔除并还原为 mentions。

从 `sessions/transcript.py` 抽出（该模块的 JSONL 解析随 L3 删除，本模块长期保留）。
"""

from __future__ import annotations

import re

_MENTION_BLOCK_RE = re.compile(r"<mentioned-documents>.*?</mentioned-documents>\s*", re.DOTALL)
_MENTION_ITEM_RE = re.compile(r"^- (.*) \(asset_id: ([0-9a-f-]{36})\)$", re.MULTILINE)


def build_mention_block(mentions: list[dict[str, str]]) -> str:
    """@ 文献提示块文本（名称里的换行折成空格，保证逐行可回读）。"""
    items = "\n".join(
        f"- {' '.join(str(m['name']).split())} (asset_id: {m['asset_id']})" for m in mentions
    )
    return (
        "<mentioned-documents>\n"
        "用户在本次提问中 @ 了以下文献。search_knowledge 已被限定只在这些文献内检索，"
        "请只依据它们回答；检索不到相关内容时如实说明，不要转而引用其他文献。\n"
        f"{items}\n"
        "</mentioned-documents>"
    )


def split_mentions(content: str) -> tuple[str, list[dict[str, str]]]:
    """用户消息文本 → (剔除提示块后的正文, mentions)。"""
    mentions = [
        {"asset_id": asset_id, "name": name}
        for block in _MENTION_BLOCK_RE.findall(content)
        for name, asset_id in _MENTION_ITEM_RE.findall(block)
    ]
    return _MENTION_BLOCK_RE.sub("", content), mentions
