"""P1/P2 公共依赖：会话/app.state 访问器、身份编排、scope 校验、空间闸门。

职责切片（P2 重构）：
- 身份解析细节（API Key 认证、dev 用户落库、user_uuid）在 api/identity.py；
- 本模块保留 FastAPI 依赖装配：get_identity 编排（JWT vs X-API-Key）、
  require_scope、空间闸门（require_space / body_space）与会话访问器。

空间闸门（P2-CORE-02）：
- `require_space(min_role)` 依赖工厂：路径参数 space_id + 成员角色校验，
  统一返回 SpaceAccess（space + member），路由层不自行拼成员查询；
- API Key 身份无用户行：按 key 的租户做空间隔离（最高 editor 权限）。
"""

from __future__ import annotations

import uuid

import redis.asyncio as aioredis
from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.auth import bearer_scheme, get_identity_provider
from loomvec.api.context import Identity
from loomvec.api.identity import identity_from_api_key, identity_from_oauth_token, resolve_user_row
from loomvec.core.authz import SpaceAccess, require_space_role
from loomvec.core.config import Settings, get_settings
from loomvec.core.constants import (
    PLATFORM_ROLE_AUDITOR,
    SCOPE_ADMIN_READ,
    SCOPE_ADMIN_WRITE,
    SCOPE_READ,
    SCOPE_WRITE,
)
from loomvec.core.db.models import Space, SpaceRole
from loomvec.core.db.repos import resolve_space_id
from loomvec.core.errors import PermissionDeniedError, UnauthenticatedError


def scopes_for_roles(roles: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """平台角色 → scopes（JWT 身份；P4-API-01）。

    - super_admin / operator：read + write + admin:read + admin:write；
    - auditor：read + admin:read（全局只读）；
    - 其余（普通用户）：read + write。
    """
    if PLATFORM_ROLE_AUDITOR in roles and not ("super_admin" in roles or "operator" in roles):
        return (SCOPE_READ, SCOPE_ADMIN_READ)
    if "super_admin" in roles or "operator" in roles:
        return (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN_READ, SCOPE_ADMIN_WRITE)
    return (SCOPE_READ, SCOPE_WRITE)


# ---------------------------------------------------------------------------
# 会话与 app.state 访问器（类型化，替代裸 state 传递）
# ---------------------------------------------------------------------------


async def get_session(request: Request) -> AsyncSession:
    """请求级会话（自动关闭；事务由服务代码按需 begin/commit）。"""
    session: AsyncSession = request.app.state.session_factory()
    try:
        yield session
    finally:
        await session.close()


async def get_redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis


def service_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(request: Request):
    return request.app.state.storage


def get_milvus(request: Request):
    return request.app.state.milvus


def get_retriever(request: Request):
    return request.app.state.retriever


def get_ai(request: Request):
    return request.app.state.ai


def get_graph_retriever(request: Request):
    return request.app.state.graph_retriever


def get_celery(request: Request):
    return request.app.state.celery


# ---------------------------------------------------------------------------
# 身份编排（解析细节见 api/identity.py）
# ---------------------------------------------------------------------------


async def get_identity(
    request: Request,
    credentials=Depends(bearer_scheme),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    session: AsyncSession = Depends(get_session),
    redis: aioredis.Redis = Depends(get_redis),
    provider=Depends(get_identity_provider),
) -> Identity:
    if x_api_key:
        identity = await identity_from_api_key(x_api_key, session, redis)
    elif credentials is not None:
        token = credentials.credentials
        if token.count(".") == 2:
            # JWT（dev/平台登录）：角色落库 + scopes 按平台角色派生
            identity = await provider.exchange({"token": token})
            identity = await resolve_user_row(session, identity)
            identity = Identity(
                user_id=identity.user_id,
                username=identity.username,
                tenant_id=identity.tenant_id,
                roles=identity.roles,
                scopes=scopes_for_roles(identity.roles),
            )
        else:
            # OAuth 用户级令牌（P4-API-07）：scopes 即授权范围（read/write）
            identity = await identity_from_oauth_token(token, session)
    else:
        raise UnauthenticatedError()
    request.state.identity = identity
    return identity


def require_scope(scope: str):
    """依赖工厂：要求当前身份具备指定 scope（dev JWT 默认全量）。"""

    async def _check(identity: Identity = Depends(get_identity)) -> Identity:
        if scope not in identity.scopes:
            raise PermissionDeniedError(required_scope=scope)
        return identity

    return _check


# ---------------------------------------------------------------------------
# 管理域守卫（P4-API-01）：平台角色 × admin scope，写操作强制审计
# ---------------------------------------------------------------------------


def require_platform_role(*roles: str):
    """依赖工厂：要求当前身份持有指定平台角色（super_admin/operator/auditor）。

    平台角色单源为 DB user_role（resolve_user_row 注入 Identity.roles）；
    API Key 身份无用户行，一律拒绝。
    """

    async def _check(identity: Identity = Depends(get_identity)) -> Identity:
        if not any(r in identity.roles for r in roles):
            raise PermissionDeniedError(
                reason="需要平台角色", required_roles=list(roles), roles=list(identity.roles)
            )
        return identity

    return _check


def require_admin(scope: str):
    """依赖工厂：管理域统一闸门 = 平台角色（三角色之一）+ admin scope。

    - super_admin / operator → admin:read + admin:write；
    - auditor → 仅 admin:read（全局只读 + 审计导出）。
    """

    async def _check(identity: Identity = Depends(get_identity)) -> Identity:
        if not identity.is_platform_admin and "auditor" not in identity.roles:
            raise PermissionDeniedError(reason="需要平台角色", roles=list(identity.roles))
        if scope not in identity.scopes:
            raise PermissionDeniedError(required_scope=scope)
        return identity

    return _check


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def admin_audit(
    request: Request,
    identity: Identity = Depends(get_identity),
    session: AsyncSession = Depends(get_session),
) -> Identity:
    """管理域审计兜底依赖（挂 /api/v1/admin 路由域级 dependencies）。

    写方法（POST/PUT/PATCH/DELETE）成功返回后落一条兜底审计（方法+路由模板+
    路径参数）；端点内的显式 record_audit（含 before/after/reason）与之并存，
    重复动作用 "auto:" 前缀区分。路由抛错（4xx/5xx）不记成功审计。
    """
    yield identity
    if request.method not in _WRITE_METHODS:
        return
    route = request.scope.get("route")
    path_template = getattr(route, "path", request.url.path)
    action = f"auto:{request.method.lower()}:{path_template}"
    try:
        await record_audit(
            session,
            identity=identity,
            action=action,
            object_type="admin_request",
            object_id=request.url.path.rsplit("/", 1)[-1] or None,
            after={"path_params": {k: str(v) for k, v in request.path_params.items()}},
            request=request,
            commit=True,
        )
    except Exception:  # 审计兜底失败不影响响应（结构化日志告警）
        from loomvec.core.logging import get_logger

        get_logger("loomvec.api.audit").warning(
            "admin_audit_record_failed", action=action, path=request.url.path
        )


# ---------------------------------------------------------------------------
# 空间闸门（P2-CORE-02）：路径/body 空间 + 成员角色校验
# ---------------------------------------------------------------------------


async def resolve_space_access(
    session: AsyncSession,
    identity: Identity,
    space_id: uuid.UUID,
    min_role: SpaceRole | str = SpaceRole.VIEWER,
) -> SpaceAccess:
    """空间访问解析（含 API Key 身份的租户级兜底）。

    - JWT 身份：成员关系 × 最低角色（authz.require_space_role）；
    - API Key 身份：无用户行，按 key 的租户校验空间归属，权限上限 editor；
      min_role=owner 的管理操作对 API Key 一律拒绝。
    """
    if identity.user_id.startswith("apikey:"):
        space = (
            await session.execute(
                select(Space).where(Space.id == space_id, Space.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if space is None:
            from loomvec.core.errors import NotFoundError

            raise NotFoundError(resource="space", id=str(space_id))
        if space.banned_at is not None:
            raise PermissionDeniedError(reason="空间已被平台封禁", space_id=str(space_id))
        if min_role == SpaceRole.OWNER or identity.tenant_id is None:
            raise PermissionDeniedError(reason="API Key 无空间管理权限", space_id=str(space_id))
        if space.tenant_id is None or str(space.tenant_id) != identity.tenant_id:
            raise PermissionDeniedError(reason="无该空间访问权限", space_id=str(space_id))
        return SpaceAccess(space=space, member=None)  # type: ignore[arg-type]
    # 平台封禁检查（P4-API-03）：封禁空间对全部成员（含 owner）拒绝访问
    banned_at = await session.scalar(select(Space.banned_at).where(Space.id == space_id))
    if banned_at is not None:
        raise PermissionDeniedError(reason="空间已被平台封禁", space_id=str(space_id))
    return await require_space_role(
        session,
        space_id=space_id,
        user_id=uuid.UUID(identity.user_id),
        min_role=min_role,
    )


def require_space(min_role: SpaceRole | str = SpaceRole.VIEWER):
    """依赖工厂：解析路径参数 space_id 并校验成员角色，返回 SpaceAccess。

    仅适用于路径含 {space_id} 的路由；/assets/{id} 类路由先定位资源再调
    resolve_space_access（见 review 路由）。
    """

    async def _dep(
        space_id: uuid.UUID,
        identity: Identity = Depends(require_scope("read")),
        session: AsyncSession = Depends(get_session),
    ) -> SpaceAccess:
        return await resolve_space_access(session, identity, space_id, min_role)

    return _dep


async def get_space_id_or_default(
    session: AsyncSession, settings: Settings, requested: uuid.UUID | None
) -> uuid.UUID:
    """body 中未指定空间时回退默认空间（P1 行为兼容）。"""
    if requested is not None:
        return requested
    return await resolve_space_id(session, settings)


async def body_space(
    session: AsyncSession,
    identity: Identity,
    settings: Settings,
    requested: uuid.UUID | None,
    min_role: SpaceRole | str = SpaceRole.EDITOR,
) -> Space:
    """body.space_id 指定空间 + 最低角色校验（上传/登记类接口）。"""
    sid = await get_space_id_or_default(session, settings, requested)
    access = await resolve_space_access(session, identity, sid, min_role)
    return access.space


def any_settings() -> Settings:
    """非 Depends 场景（脚本/测试）取进程级单例。"""
    return get_settings()
