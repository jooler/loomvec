"""models 包内部共享件：枚举持久化辅助。"""

from __future__ import annotations


def enum_values(e) -> list[str]:
    """SQLAlchemy 默认持久化枚举成员 name；PG 枚举标签是小写 value，需显式指定。"""
    return [m.value for m in e]
