"""P4-API-05 配置接口：五组配置（AI 供方/检索/上传/SSO/扩展）读写。

- sensitive 键脱敏回显（写入明文，读取 `******`）；
- ai/sso/extensions 组仅 super_admin 可改（operator 无系统配置权限）；
- effect 字段标注即时生效 / 需重启。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.context import Identity
from loomvec.api.deps import get_session, require_admin
from loomvec.api.services import admin_settings as settings_service
from loomvec.core.constants import (
    PLATFORM_ROLE_SUPER_ADMIN,
)
from loomvec.core.errors import PermissionDeniedError

router = APIRouter(tags=["admin-settings"])


class SettingPutRequest(BaseModel):
    value: Any
    reason: str | None = None


@router.get("/settings")
async def list_all(
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    return await settings_service.list_settings(session)


@router.get("/settings/{key}")
async def get_one(
    key: str,
    identity: Identity = Depends(require_admin("admin:read")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await settings_service.get_setting(session, key)


@router.put("/settings/{key}")
async def put_one(
    key: str,
    body: SettingPutRequest,
    request: Request,
    identity: Identity = Depends(require_admin("admin:write")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    d = settings_service.SETTING_REGISTRY.get(key)
    if d is None:
        from loomvec.core.errors import NotFoundError

        raise NotFoundError(resource="setting", id=key)
    if d.admin_only and PLATFORM_ROLE_SUPER_ADMIN not in identity.roles:
        raise PermissionDeniedError(
            reason=f"配置组 {d.group} 仅 super_admin 可修改",
            group=d.group,
        )
    out = await settings_service.put_setting(session, key, body.value, uuid.UUID(identity.user_id))
    await record_audit(
        session,
        identity=identity,
        action="admin.settings.update",
        object_type="system_config",
        object_id=key,
        reason=body.reason,
        after={"key": key},
        request=request,
    )
    await session.commit()
    return out
