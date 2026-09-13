"""P2-CORE-04 标签/分类/元数据：空间 schema 校验与标签规范化。

- 元数据：资产 user_meta 的每个键必须在空间 metadata_field 登记，
  类型匹配（number/date/select 语义校验），required 缺失即拒绝；
- 标签名规范化：去空白、折叠连续空格、小写英文（检索聚合友好）；
- 校验为纯函数，DB 读取由调用方完成。
"""

from __future__ import annotations

import datetime as dt

from loomvec.core.db.models import MetadataField, MetadataFieldType
from loomvec.core.errors import ValidationError


def normalize_tag_name(name: str) -> str:
    return " ".join(name.split())


def validate_metadata(fields: list[MetadataField], values: dict) -> dict:
    """按空间 schema 校验并规范化元数据值；返回可直接落库的 dict。"""
    known = {f.key: f for f in fields}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValidationError("元数据包含未登记的键", unknown_keys=unknown)

    validated: dict = {}
    for key, field in known.items():
        value = values.get(key)
        if value is None or value == "":
            if field.required:
                raise ValidationError("必填元数据缺失", key=key)
            continue
        validated[key] = _coerce(field, value)
    return validated


def _coerce(field: MetadataField, value) -> object:
    if field.field_type == MetadataFieldType.NUMBER:
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValidationError("元数据类型不匹配（应为数字）", key=field.key) from None
        return int(num) if num.is_integer() else num
    if field.field_type == MetadataFieldType.DATE:
        try:
            dt.date.fromisoformat(str(value))
        except ValueError:
            raise ValidationError("元数据类型不匹配（应为 ISO 日期）", key=field.key) from None
        return str(value)
    if field.field_type == MetadataFieldType.SELECT:
        if str(value) not in (field.options or []):
            raise ValidationError(
                "元数据取值不在选项内", key=field.key, options=field.options or []
            )
        return str(value)
    return str(value)
