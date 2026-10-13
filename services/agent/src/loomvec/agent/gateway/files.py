"""工作区浏览：网关 `tree` 嵌套结构 → 前端的扁平节点列表（纯函数）。

保留旧 `/workspace/tree` 的语义：
- 隐藏段（任一路径段以 `.` 开头）不出树；
- 最多 500 项；
- 带 `prefix` 时只返回该子树，且列表含 prefix 条目本身（前端以它识别根节点）；
- 条目 `type` 只有 file | dir（符号链接 / 其他类型网关不允许读取，直接略去）。
"""

from __future__ import annotations

from typing import Any

MAX_TREE_ITEMS = 500
TREE_DEPTH = 8  # 网关 depth 上限；更深的层级不展开
FILE_PREVIEW_BYTES = 256 * 1024


def is_hidden(path: str) -> bool:
    return any(part.startswith(".") for part in path.split("/") if part)


def _node(entry: dict[str, Any]) -> dict[str, Any]:
    is_dir = entry["type"] == "dir"
    return {
        "path": entry["path"],
        "name": entry["name"],
        "type": "dir" if is_dir else "file",
        "size": 0 if is_dir else int(entry.get("size") or 0),
    }


def prefix_node(prefix: str, entry_type: str = "dir", size: int = 0) -> dict[str, Any]:
    return {
        "path": prefix,
        "name": prefix.rsplit("/", 1)[-1],
        "type": "dir" if entry_type == "dir" else "file",
        "size": 0 if entry_type == "dir" else size,
    }


def flatten_tree(
    prefix: str, entries: list[dict[str, Any]], *, limit: int = MAX_TREE_ITEMS
) -> list[dict[str, Any]]:
    """深度优先展平（每层按网关给的名字序），隐藏段与非 file/dir 条目略去，截到 `limit` 项。

    `prefix` 非空时首项为 prefix 目录本身。
    """
    items: list[dict[str, Any]] = []
    if prefix:
        if is_hidden(prefix):
            return []
        items.append(prefix_node(prefix))

    def walk(level: list[dict[str, Any]]) -> None:
        for entry in level:
            if len(items) >= limit:
                return
            if entry.get("type") not in ("file", "dir") or is_hidden(str(entry.get("path", ""))):
                continue
            items.append(_node(entry))
            if entry["type"] == "dir":
                walk(entry.get("children") or [])

    walk(entries)
    return items[:limit]
