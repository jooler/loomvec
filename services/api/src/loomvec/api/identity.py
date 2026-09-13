"""P1/P2 身份解析（deps 的职责切片）：API Key 认证、dev JWT 用户落库、scope 校验。

- API Key（P1-API-04）：`X-API-Key: lv-...` → sha256 查库 → 过期校验 → Redis 固定
  窗口限流；scopes 即 read/write 两类；
- dev JWT（P2）：首次请求 get_or_create User（挂种子租户）并加入默认空间为 editor
  （P1 单空间行为连续性），Identity.user_id 此后为 DB 用户 ID；DB 不可用时降级
  为无状态身份（仅 /me 等无库接口可用）；
- require_scope / user_uuid：scope 闸门与用户上下文提取（供路由/服务复用）。
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.core.config import get_settings
from loomvec.core.constants import (
    API_KEY_RATE_LIMIT_KEY_PREFIX,
    RATE_WINDOW_SECONDS,
    SCOPE_READ,
    SCOPE_WRITE,
    SEED_TENANT_ID,
)
from loomvec.core.db.models import (
    ApiKey,
    OauthAccessToken,
    Role,
    SpaceMember,
    SpaceRole,
    Tenant,
    TenantStatus,
    User,
    UserRole,
    UserStatus,
)
from loomvec.core.db.repos import SpaceMemberRepo, SpaceRepo, UserRepo
from loomvec.core.errors import PermissionDeniedError, RateLimitedError, UnauthenticatedError

logger = structlog.get_logger("loomvec.api.identity")


def generate_api_key() -> tuple[str, str]:
    """返回 (明文 key, sha256 hash)。明文只在签发响应中出现一次。"""
    raw = f"lv_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def identity_from_api_key(
    raw_key: str, session: AsyncSession, redis: aioredis.Redis
) -> Identity:
    """API Key → Identity：哈希查库、过期/吊销校验、固定窗口限流、更新 last_used。"""
    key = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.key_hash == hash_api_key(raw_key), ApiKey.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if key is None or (key.expires_at and key.expires_at.timestamp() < time.time()):
        raise UnauthenticatedError(reason="API Key 无效或已过期")
    await _ensure_tenant_active(session, key.tenant_id)

    window = int(time.time()) // RATE_WINDOW_SECONDS
    rl_key = f"{API_KEY_RATE_LIMIT_KEY_PREFIX}:{key.id}:{window}"
    count = await redis.incr(rl_key)
    if count == 1:
        await redis.expire(rl_key, RATE_WINDOW_SECONDS)
    if count > key.rate_limit_per_min:
        raise RateLimitedError(limit=key.rate_limit_per_min, window=f"{RATE_WINDOW_SECONDS}s")

    key.last_used_at = datetime.now()
    await session.commit()

    return Identity(
        user_id=f"apikey:{key.id}",
        username=key.name,
        tenant_id=str(key.tenant_id) if key.tenant_id else None,
        roles=("api_key",),
        scopes=tuple(key.scopes or []),
    )


async def identity_from_oauth_token(raw_token: str, session: AsyncSession) -> Identity:
    """OAuth 用户级令牌（P4-API-07）：哈希查库 → 过期/吊销/用户状态校验。

    权限收敛：scopes 即授权时授予的用户级 scopes（不含 admin:*），不超用户本人。
    """
    row = (
        await session.execute(
            select(OauthAccessToken).where(OauthAccessToken.token_hash == hash_api_key(raw_token))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise UnauthenticatedError(reason="OAuth 令牌无效")
    now = datetime.now(UTC)
    if row.expires_at < now:
        raise UnauthenticatedError(reason="OAuth 令牌已过期")
    user = (
        await session.execute(select(User).where(User.id == row.user_id, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None or user.status == UserStatus.DISABLED:
        raise UnauthenticatedError(reason="OAuth 令牌所属用户不可用")
    row.last_used_at = now
    await session.commit()
    return Identity(
        user_id=str(user.id),
        username=user.username,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        roles=(f"oauth:{row.client_id}",),
        scopes=tuple(s for s in (row.scopes or []) if s in (SCOPE_READ, SCOPE_WRITE)),
    )


def user_uuid(identity: Identity) -> uuid.UUID:
    """JWT 身份的 DB 用户 ID；API Key 身份无用户上下文。"""
    if identity.user_id.startswith("apikey:"):
        raise UnauthenticatedError(reason="API Key 无用户上下文")
    try:
        return uuid.UUID(identity.user_id)
    except ValueError as e:
        raise UnauthenticatedError(reason="身份缺少有效用户 ID") from e


async def _ensure_tenant_active(session: AsyncSession, tenant_id: uuid.UUID | None) -> None:
    """冻结语义（P4-API-03）：租户停用后阻断其用户登录与 API 访问（数据保留）。"""
    if tenant_id is None:
        return
    status = await session.scalar(select(Tenant.status).where(Tenant.id == tenant_id))
    if status == TenantStatus.SUSPENDED:
        raise PermissionDeniedError(reason="租户已停用（冻结），请联系平台管理员")


async def resolve_user_row(session: AsyncSession, identity: Identity) -> Identity:
    """dev JWT 身份落库：get_or_create User（挂种子租户）并加入默认空间。

    Identity.user_id 从 uuid5 占位切换为 DB 用户 ID（成员关系的唯一事实源）。
    P4-API-01：DB user_role（is_platform_role）合并进 Identity.roles——
    平台角色的单源是数据库，JWT claims 中的 roles 仅作 dev 便捷注入。
    """
    if identity.user_id.startswith("apikey:"):
        return identity
    try:
        user = await UserRepo(session).get_by_username(identity.username)
    except Exception as e:
        logger.warning(
            "identity_user_resolution_degraded", username=identity.username, error=str(e)
        )
        return identity
    if user is None:
        tenant_id = uuid.UUID(identity.tenant_id) if identity.tenant_id else None
        if tenant_id is None:
            tenant_id = uuid.UUID(SEED_TENANT_ID)
        user = User(
            tenant_id=tenant_id,
            username=identity.username,
            display_name=identity.username,
            auth_source="local",
        )
        session.add(user)
        await session.flush()
    # dev 连续性：新用户自动加入默认空间为 editor（P1 单空间流程不断）
    space = await SpaceRepo(session).get_by_slug(get_settings().default_space_slug)
    if space is not None:
        member = await SpaceMemberRepo(session).get(space.id, user.id)
        if member is None:
            session.add(SpaceMember(space_id=space.id, user_id=user.id, role=SpaceRole.EDITOR))
            await session.flush()
    # 冻结语义：租户停用后阻断登录（数据保留）
    await _ensure_tenant_active(session, user.tenant_id)
    # 平台角色合并（DB 单源；dev token 传入的角色仅在 DB 无记录时兜底）
    db_roles = (
        (
            await session.execute(
                select(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .where(
                    UserRole.user_id == user.id,
                    Role.is_platform_role.is_(True),
                    Role.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    roles = tuple(dict.fromkeys((*db_roles, *identity.roles)))
    await session.commit()
    return Identity(
        user_id=str(user.id),
        username=identity.username,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        roles=roles,
        scopes=identity.scopes,
    )
