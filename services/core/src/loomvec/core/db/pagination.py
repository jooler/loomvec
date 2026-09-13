"""游标分页工具（契约：`items` + `next_cursor`，02 文档 §五）。

游标 = base64(created_at, id) 双字段，稳定；不提供跳页。
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, or_

from loomvec.core.errors import ValidationError


@dataclass(frozen=True)
class Cursor:
    created_at: datetime
    id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> Cursor:
        try:
            padded = value + "=" * (-len(value) % 4)
            created_at_raw, id_raw = base64.urlsafe_b64decode(padded).decode().split("|", 1)
            return cls(created_at=datetime.fromisoformat(created_at_raw), id=uuid.UUID(id_raw))
        except (ValueError, binascii.Error) as e:
            raise ValidationError("游标不合法", cursor=value) from e


def apply_cursor(stmt: Select, cursor: Cursor, *, desc: bool = True) -> Select:
    """按 (created_at, id) 组合游标追加 WHERE；调用方需以相同键排序。"""
    entity = stmt.column_descriptions[0]["entity"]
    created_at_col, id_col = entity.created_at, entity.id
    if desc:
        cond = or_(
            created_at_col < cursor.created_at,
            and_(created_at_col == cursor.created_at, id_col < cursor.id),
        )
    else:
        cond = or_(
            created_at_col > cursor.created_at,
            and_(created_at_col == cursor.created_at, id_col > cursor.id),
        )
    return stmt.where(cond)
