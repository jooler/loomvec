"""本地账号密码登录：`POST /api/v1/auth/login` → 平台 JWT。

- 凭证：users.password_hash（core/passwords.py 的 scrypt 哈希）；密码由
  `scripts/set_password.py` 设置；
- 失败统一返回 401「用户名或密码错误」，不区分账号不存在 / 未设密码 / 密码错
  （不存在与未设密码时补做一次等价哈希校验，耗时一致）；
- 防暴力破解：校验前先原子 INCR 该用户名的尝试计数（Redis，窗口 LOGIN_LOCK_SECONDS，
  键无 TTL 时补设），超过 LOGIN_MAX_FAILURES 次即锁定直至窗口结束（429），成功即清零；
  先计数后校验，并发请求也突破不了上限。Redis 不可用时 fail-open（与全局限流一致）；
- scrypt 校验约 100ms+，放到线程里跑，避免未认证接口占满事件循环；
- 成功 / 失败 / 锁定都写审计（失败只在账号存在时记用户名，免得把误填进用户名框的密码落库）；
- 签发的 JWT 不带平台角色：平台角色单源为 DB user_role，由 resolve_user_row 合并。
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.audit import record_audit
from loomvec.api.auth import PlatformTokenProvider, TokenResponse
from loomvec.api.deps import get_session
from loomvec.core.config import Settings, get_settings
from loomvec.core.db.models import UserStatus
from loomvec.core.db.repos import UserRepo
from loomvec.core.errors import RateLimitedError, UnauthenticatedError
from loomvec.core.passwords import MAX_PASSWORD_LENGTH, verify_dummy, verify_password

logger = structlog.get_logger("loomvec.api.auth_password")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

LOGIN_MAX_FAILURES = 10
LOGIN_LOCK_SECONDS = 15 * 60
_BAD_CREDENTIALS = "用户名或密码错误"


class PasswordLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


def _fail_key(username: str) -> str:
    return f"loomvec:login:fail:{username.lower()}"


async def _count_attempt(redis, username: str) -> int:
    """原子累加本窗口内的尝试次数并返回；Redis 不可用 → 0（fail-open）。"""
    try:
        key = _fail_key(username)
        count = int(await redis.incr(key))
        # 键没有 TTL 就补上（首次创建，或 incr 与 expire 之间进程中断留下的无 TTL 键），
        # 避免账号被永久锁死
        if count == 1 or await redis.ttl(key) < 0:
            await redis.expire(key, LOGIN_LOCK_SECONDS)
        return count
    except Exception as e:
        logger.warning("login_lock_check_failed", error=str(e))
        return 0


async def _clear_failures(redis, username: str) -> None:
    try:
        await redis.delete(_fail_key(username))
    except Exception as e:
        logger.warning("login_lock_clear_failed", error=str(e))


async def _audit(session: AsyncSession, request: Request, action: str, user=None) -> None:
    """登录事件留痕；审计写入失败不影响登录结果。"""
    try:
        await record_audit(
            session,
            identity=None,
            action=action,
            object_type="user",
            object_id=str(user.id) if user is not None else None,
            after={"username": user.username} if user is not None else None,
            request=request,
            commit=True,
        )
    except Exception as e:
        logger.warning("login_audit_failed", action=action, error=str(e))


@router.post("/login", response_model=TokenResponse)
async def password_login(
    body: PasswordLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = await UserRepo(session).get_by_username(body.username)
    redis = getattr(request.app.state, "redis", None)
    attempts = await _count_attempt(redis, body.username) if redis is not None else 0
    if attempts > LOGIN_MAX_FAILURES:
        if attempts == LOGIN_MAX_FAILURES + 1:  # 每个窗口只记首次锁定，免得被刷爆审计表
            await _audit(session, request, "auth.login_locked", user)
        raise RateLimitedError(
            "登录失败次数过多，请稍后再试",
            limit=LOGIN_MAX_FAILURES,
            window=f"{LOGIN_LOCK_SECONDS}s",
        )

    valid = False
    if user is not None and user.password_hash and user.status == UserStatus.ACTIVE:
        valid = await asyncio.to_thread(verify_password, body.password, user.password_hash)
    else:
        await asyncio.to_thread(verify_dummy)
    if not valid:
        logger.warning("password_login_failed", known_user=user is not None)
        await _audit(session, request, "auth.login_failed", user)
        raise UnauthenticatedError(_BAD_CREDENTIALS)

    if redis is not None:
        await _clear_failures(redis, body.username)
    await _audit(session, request, "auth.login_success", user)
    token, claims = PlatformTokenProvider(settings).issue_token(
        username=user.username,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    return TokenResponse(access_token=token, expires_in=claims["exp"] - claims["iat"])
