"""P0-CORE-03 仓储与服务层模式：SQLAlchemy 2 async 仓储基类。

约定：
- 服务层持有 Repository，不直接拼 SQL；跨仓储事务用 `transaction()`；
- 软删除：`delete()` 只置 deleted_at；查询默认过滤已删行；
- 领域异常（NotFoundError 等）在仓储层抛出，由 api 层统一映射 HTTP。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.base import Base
from loomvec.core.errors import NotFoundError


class Repository[ModelT: Base]:
    """每个模型一个子类：`class UserRepo(Repository[User]): model = User`。"""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------- 查询 ----------

    async def get(self, id_: uuid.UUID, *, include_deleted: bool = False) -> ModelT | None:
        stmt = select(self.model).where(self.model.id == id_)  # type: ignore[attr-defined]
        if not include_deleted and hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_404(self, id_: uuid.UUID, *, include_deleted: bool = False) -> ModelT:
        obj = await self.get(id_, include_deleted=include_deleted)
        if obj is None:
            raise NotFoundError(resource=self.model.__tablename__, id=str(id_))
        return obj

    async def list(self, *, limit: int = 50, offset: int = 0, **filters: Any) -> list[ModelT]:
        stmt = self._base_select(**filters).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self, **filters: Any) -> int:
        stmt = select(func.count()).select_from(self._base_select(**filters).subquery())
        return (await self.session.execute(stmt)).scalar_one()

    def _base_select(self, **filters: Any):
        stmt = select(self.model)
        if hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)
        return stmt

    # ---------- 写操作 ----------

    async def create(self, **fields: Any) -> ModelT:
        obj = self.model(**fields)  # type: ignore[call-arg]
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def update(self, obj: ModelT, **fields: Any) -> ModelT:
        for key, value in fields.items():
            setattr(obj, key, value)
        await self.session.flush()
        return obj

    async def soft_delete(self, obj: ModelT) -> None:
        if hasattr(obj, "deleted_at"):
            obj.deleted_at = datetime.now()  # type: ignore[attr-defined]
        else:  # 追加写表（如 audit_log）不允许删除
            raise TypeError(f"{type(obj).__name__} 不支持删除")
        await self.session.flush()


class UnitOfWork:
    """事务边界：一个请求/任务 = 一个 session = 一个事务（默认）。"""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session, session.begin():
            yield session
