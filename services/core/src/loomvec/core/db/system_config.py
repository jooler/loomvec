"""system_config 键值读取（admin 动态配置的 DB 侧单源）。

api（admin_settings 服务 / 应用启动合并 AI 供方覆盖）与 worker（任务装配）
共用本模块，避免各自手写 select 造成语义漂移。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import SystemConfig


async def load_overrides(session: AsyncSession) -> dict[str, Any]:
    """全量键值（key → value）。"""
    rows = (await session.execute(select(SystemConfig))).scalars().all()
    return {r.key: r.value for r in rows}


async def load_ai_overrides(session: AsyncSession) -> dict[str, Any]:
    """ai.* 键值子集（AI 供方覆盖合并用，见 core.config.apply_ai_overrides）。"""
    rows = (
        (await session.execute(select(SystemConfig).where(SystemConfig.key.like("ai.%"))))
        .scalars()
        .all()
    )
    return {r.key: r.value for r in rows}
