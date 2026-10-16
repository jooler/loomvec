"""P3-CORE-01 实体规范化与确定性 ID（03 文档 §3.3 阶段一.1）。

- 规范化：Unicode NFKC（全角→半角）、大小写折叠、空白/标点压缩——
  同名不同写法（"A公司" / "Ａ 公司"）归并为同一 name_norm；
- entity_key = sha256(f"{space_id}|{name_norm}|{type}") 十六进制——
  AGE 节点与 Milvus entities 主键共用，跨批次/跨资产幂等（P3-CORE-01）。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from loomvec.core.constants import DEFAULT_ENTITY_TYPE

# 归一化时剔除的分隔符（保留中英数字；公司后缀等语义归并交由向量链接与阶段二合并）
# 实体类型写法：小写字母开头的小写字母/数字/下划线，至多 32 位
ENTITY_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
_TYPE_SLUG_RE = re.compile(ENTITY_TYPE_PATTERN)
_STRIP_RE = re.compile(r"[\s\-—–_·•.,，。;；:：!！?？'\"“”‘’()（）\[\]【】<>《》/\\|]+")


def normalize_entity_name(name: str) -> str:
    """实体规范名归一化：NFKC → 去首尾 → 剔除分隔符 → 小写。"""
    text = unicodedata.normalize("NFKC", str(name or "")).strip()
    text = _STRIP_RE.sub("", text)
    return text.lower()


def canonical_type(entity_type: str | None) -> str:
    """实体类型规范化（与领域无关）：去首尾空白、小写；不是合法 slug 的落 other。

    类型集合由抽取领域配置决定（graph/profiles，解析时按配置白名单校验），这里只保证
    写法规范——entity_key 用它计算，同一类型的不同大小写写法得到同一个键。
    """
    t = str(entity_type or "").strip().lower()
    return t if _TYPE_SLUG_RE.match(t) else DEFAULT_ENTITY_TYPE


def entity_key(space_id: str, canonical_name: str, entity_type: str) -> str:
    """确定性实体 ID：hash(space, canonical_name, type)——幂等 upsert 的根基。"""
    norm = normalize_entity_name(canonical_name)
    raw = f"{space_id}|{norm}|{canonical_type(entity_type)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def entity_embed_text(name: str, entity_type: str, description: str | None = None) -> str:
    """实体链接向量输入：类型先行（消歧同名不同类），描述增强语境。"""
    parts = [f"[{canonical_type(entity_type)}] {str(name or '').strip()}"]
    if description:
        parts.append(str(description).strip())
    return "\n".join(p for p in parts if p)
