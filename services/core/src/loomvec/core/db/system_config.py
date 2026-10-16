"""system_config 键值读取（admin 动态配置的 DB 侧单源）。

api（admin_settings 服务 / 应用启动合并 AI 供方覆盖）与 worker（任务装配 /
chunk 步读取抽取领域配置）共用本模块，避免各自手写 select 造成语义漂移。
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.db.models import SystemConfig
from loomvec.core.graph.profiles import (
    BUILTIN_PROFILES,
    CUSTOM_PROFILES_SETTING_KEY,
    ExtractionProfile,
    available_profiles,
)

logger = structlog.get_logger("loomvec.system_config")


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


async def load_extraction_profiles(session: AsyncSession) -> dict[str, ExtractionProfile]:
    """可用的抽取领域配置（内置 + system_config 中的自定义配置），worker 每次 chunk 步读一次。

    自定义配置写入时已严格校验；库里的值仍不合法（如手工改库）时只告警、只用内置配置，
    不中断管线——引用了自定义配置的空间按 resolve_extraction_profile 回落全局。
    """
    value = (
        await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == CUSTOM_PROFILES_SETTING_KEY)
        )
    ).scalar_one_or_none()
    try:
        return available_profiles(value)
    except ValueError as e:
        logger.warning("extraction_profiles_invalid", error=str(e))
        return dict(BUILTIN_PROFILES)
