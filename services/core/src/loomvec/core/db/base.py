"""P0-CORE-03 数据库基建：async Engine/Session、声明基类与通用混入。

数据模型纪律（06 文档 · 工程约定 6）：
- 所有业务表自 P0 起带 `tenant_id` / `created_at` / `updated_at`；
- 软删除约定：`deleted_at` 可空时间戳，查询默认过滤（仓储基类统一处理）；
- 主键统一 UUID，PG18 下由内置 `uuidv7()` 生成（时间有序，利于索引与游标分页）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from loomvec.core.config import PostgresSettings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuidv7() -> text:
    return text("uuidv7()")


class UuidPkMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=_uuidv7(), sort_order=-100
    )


class TenantMixin:
    """租户语义预留：后续业务表一律携带 tenant_id（P2 落地隔离逻辑）。"""

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False, sort_order=90
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), onupdate=lambda: datetime.now(), nullable=False, sort_order=91
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None, sort_order=92
    )


def create_engine_and_sessionmaker(settings: PostgresSettings):
    engine = create_async_engine(
        settings.url,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        echo=settings.echo,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory
