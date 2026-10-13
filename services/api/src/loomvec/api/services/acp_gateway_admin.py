"""ACP 网关管理接口（/admin/v1/*）的受限代理客户端。

只服务 `/api/v1/admin/acp-gateway/*` 代理路由，不是通用转发：

- 白名单：只能调用 `_OPERATIONS` 里登记的 (方法, 路径模板)；路径参数必须匹配 id 格式，
  拒绝 `..`、斜杠、冒号等能改写路径的字符（网关的 `sessions:import` 等迁移接口不在表内）；
- 凭据：管理 token 取 admin 动态配置 `agent.gateway.admin_token`（DB，无文件回落），
  只出现在发往网关的 Authorization 头里——不进日志、不进异常信息、不跟随重定向；
- 网关返回的错误转换为平台统一错误结构（见 `_map_error`），网关的 `details` 不透传，
  消息里的 token / API Key 形状字符串会被抹掉；
- 网关地址取 `agent.gateway.url`（config/loomvec.json，只读）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import get_app_config
from loomvec.core.errors import (
    ConflictError,
    LoomvecError,
    NotFoundError,
    RateLimitedError,
    UpstreamUnavailableError,
    ValidationError,
)

UPSTREAM_NAME: Final = "acp-gateway"

# 两个敏感项的 system_config 键（admin_settings 注册表同名登记）
SETTING_API_KEY: Final = "agent.gateway.api_key"
SETTING_ADMIN_TOKEN: Final = "agent.gateway.admin_token"

DEFAULT_TIMEOUT_S: Final = 30.0

# loomvec 租户 Key 需要的 scope（P2 合同 §2.2：会话 + 文件）
REQUIRED_KEY_SCOPES: Final = ("sessions:read", "sessions:write", "files:read", "files:write")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# 网关 API Key 明文形状；错误消息里出现即抹掉
_KEY_SHAPE_RE = re.compile(r"acpgw_[A-Za-z0-9_-]+")
_MAX_MESSAGE_CHARS = 300


class GatewayNotConfiguredError(LoomvecError):
    """网关管理凭据或地址未配置 / 已失效：需要运维先在「设置 → agent」处理。"""

    code = "acp_gateway_not_configured"
    http_status = 409
    message = "ACP 网关管理凭据未配置"


class GatewayAuthError(LoomvecError):
    """网关拒绝了管理 token（401/403）：配置问题，不用 401 以免触发前端登出。"""

    code = "acp_gateway_auth_failed"
    http_status = 409
    message = "ACP 网关拒绝了管理凭据（检查 agent.gateway.admin_token 与网关的来源白名单）"


@dataclass(frozen=True)
class Operation:
    method: str
    path: str  # 含 {name} 占位的网关路径


_OPERATIONS: Final[dict[str, Operation]] = {
    "tenants.list": Operation("GET", "/admin/v1/tenants"),
    "tenants.create": Operation("POST", "/admin/v1/tenants"),
    "tenants.get": Operation("GET", "/admin/v1/tenants/{tenant_id}"),
    "tenants.update": Operation("PATCH", "/admin/v1/tenants/{tenant_id}"),
    "keys.list": Operation("GET", "/admin/v1/tenants/{tenant_id}/api-keys"),
    "keys.issue": Operation("POST", "/admin/v1/tenants/{tenant_id}/api-keys"),
    "keys.revoke": Operation("DELETE", "/admin/v1/api-keys/{key_id}"),
    "usage.get": Operation("GET", "/admin/v1/tenants/{tenant_id}/usage"),
    "runtimes.list": Operation("GET", "/admin/v1/runtimes"),
    "workspaces.stop": Operation("POST", "/admin/v1/workspaces/{workspace_id}/stop"),
}

OPERATION_NAMES: Final = frozenset(_OPERATIONS)


def _clean_message(text: str, secrets: tuple[str, ...]) -> str:
    """网关错误消息脱敏：抹掉管理 token 与 API Key 形状串，截断长度。"""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    text = _KEY_SHAPE_RE.sub("acpgw_***", text)
    return text[:_MAX_MESSAGE_CHARS]


def _map_error(resp: httpx.Response, secrets: tuple[str, ...]) -> LoomvecError:
    """网关错误 → 平台统一错误（详情只保留网关错误码与状态，不透传 details）。"""
    gw_code: str | None = None
    gw_message = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            gw_code = err.get("code") if isinstance(err.get("code"), str) else None
            gw_message = err.get("message") if isinstance(err.get("message"), str) else ""
    except ValueError:
        pass
    status = resp.status_code
    info: dict[str, Any] = {"gateway_status": status}
    if gw_code:
        info["gateway_code"] = gw_code
    message = _clean_message(gw_message, secrets) if gw_message else None
    if status in (401, 403):
        return GatewayAuthError(**info)
    if status == 404:
        return NotFoundError(message or "ACP 网关资源不存在", **info)
    if status == 409:
        return ConflictError(message or "ACP 网关资源状态冲突", **info)
    if status == 429:
        return RateLimitedError(message or "ACP 网关限流", **info)
    if status in (400, 413, 422):
        return ValidationError(reason=message or "ACP 网关拒绝了该请求", **info)
    return UpstreamUnavailableError(UPSTREAM_NAME, "ACP 网关返回错误", **info)


def _gateway_app_cfg() -> tuple[str, float]:
    """（url, request_timeout_s）：config/loomvec.json 的 agent.gateway。"""
    gw = get_app_config().agent.gateway
    return gw.url, gw.request_timeout_s


def gateway_url() -> str:
    return _gateway_app_cfg()[0]


def mcp_sandbox_target() -> dict[str, str | None]:
    """agent.mcp.url_sandbox 的主机信息：租户 settings.mcp.allowed_hosts 必须覆盖它。"""
    url = get_app_config().agent.mcp.url_sandbox
    parts = urlsplit(url)
    return {
        "url": url,
        "hostname": parts.hostname,
        "host": parts.netloc.rpartition("@")[2]
        or None,  # host[:port]，与网关 url.host 比较口径一致
    }


async def _secret_setting(session: AsyncSession, key: str) -> str:
    from loomvec.api.services.admin_settings import effective_value

    value = await effective_value(session, key)
    return value.strip() if isinstance(value, str) else ""


async def is_configured(session: AsyncSession, key: str) -> bool:
    return bool(await _secret_setting(session, key))


class GatewayAdminClient:
    """网关管理接口客户端（每次调用一个短连接；管理面低频）。"""

    def __init__(
        self,
        *,
        base_url: str,
        admin_token: str,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise GatewayNotConfiguredError("agent.gateway.url 不是合法的 http(s) 地址")
        self._base_url = base_url.rstrip("/")
        self._token = admin_token
        self._timeout = timeout_s
        self._transport = transport

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def __repr__(self) -> str:  # 避免 token 经 repr 进日志
        return f"GatewayAdminClient(base_url={self._base_url!r})"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                return await client.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as e:
            # 只带异常类型名：httpx 异常文本可能含 URL，不含凭据，但保守起见不透传
            raise UpstreamUnavailableError(
                UPSTREAM_NAME, "ACP 网关不可达或响应超时", error=type(e).__name__
            ) from None

    async def call(
        self,
        op: str,
        *,
        path_params: Mapping[str, str] | None = None,
        json: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        """执行白名单操作；成功返回解析后的 JSON（空响应为 {}），失败抛平台错误。"""
        if not self._token:
            raise GatewayNotConfiguredError(
                "未配置 ACP 网关管理 token：请在「设置 → agent」填写 agent.gateway.admin_token"
            )
        spec = _OPERATIONS.get(op)
        if spec is None:  # 编程错误：路由层只能用登记过的操作
            raise ValueError(f"未登记的网关操作: {op}")
        values = dict(path_params or {})
        for name, value in values.items():
            if not _ID_RE.fullmatch(value):
                raise ValidationError(reason=f"{name} 格式不合法")
        try:
            path = spec.path.format(**values)
        except KeyError as e:
            raise ValueError(f"网关操作 {op} 缺少路径参数 {e.args[0]}") from None
        resp = await self._send(spec.method, path, json=json, params=params)
        if resp.status_code >= 400:
            raise _map_error(resp, (self._token,))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise UpstreamUnavailableError(UPSTREAM_NAME, "ACP 网关返回了非 JSON 响应") from None

    async def healthz(self) -> bool:
        """GET /healthz（无需鉴权，不带 token）；2xx 为健康；连不上抛 UpstreamUnavailableError。"""
        resp = await self._send("GET", "/healthz", auth=False)
        return resp.status_code < 300


async def build_client(
    session: AsyncSession, *, transport: httpx.AsyncBaseTransport | None = None
) -> GatewayAdminClient:
    """按当前配置（文件 url + DB 管理 token）构造客户端；token 缺失时 call() 才报错。"""
    url, timeout = _gateway_app_cfg()
    token = await _secret_setting(session, SETTING_ADMIN_TOKEN)
    return GatewayAdminClient(
        base_url=url, admin_token=token, timeout_s=timeout, transport=transport
    )
