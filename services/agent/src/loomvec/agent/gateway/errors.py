"""网关错误：HTTP 错误体 `{error:{code,message,details?}}` → 异常 → loomvec 领域异常。

API Key 绝不进异常消息 / 日志；本模块只处理响应体里的 code / message / details。
"""

from __future__ import annotations

from typing import Any

import httpx

from loomvec.core.errors import (
    ConflictError,
    LoomvecError,
    NotFoundError,
    RateLimitedError,
    UpstreamUnavailableError,
    ValidationError,
)

UPSTREAM = "acp-gateway"


class GatewayError(Exception):
    """网关返回了 HTTP 错误（4xx / 5xx）。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id

    @classmethod
    def from_response(cls, resp: httpx.Response, body: bytes | None = None) -> GatewayError:
        """从响应构造；`body` 用于流式响应（已手工读完的字节）。"""
        import json

        code, message, details = "internal", f"网关返回 HTTP {resp.status_code}", None
        try:
            raw = body if body is not None else resp.content
            payload = json.loads(raw) if raw else {}
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                code = str(err.get("code") or code)
                message = str(err.get("message") or message)
                details = err.get("details")
        except ValueError:
            pass
        return cls(resp.status_code, code, message, details, resp.headers.get("x-request-id"))

    @property
    def detail_dict(self) -> dict[str, Any]:
        return self.details if isinstance(self.details, dict) else {}


class GatewayPathError(ValueError):
    """文件路径含 `.` / `..` 段：发出前拒绝（HTTP 客户端会把点段折叠成别的路径）。"""


class GatewayTransportError(Exception):
    """连不上网关 / 读写超时 / 连接被中途断开（没有拿到 HTTP 响应）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GatewayConfigError(LoomvecError):
    """网关未配置或凭据无效。

    刻意不用 401：api 门面会把 401 当成「用户登录失效」，导致前端被强制登出；
    503 经门面映射为 UpstreamUnavailable，文案原样给到前端。
    """

    code = "agent_gateway_misconfigured"
    http_status = 503
    message = "ACP 网关未配置"


_QUOTA_TEXT = {
    "max_concurrent_turns": "租户并发回合数已达上限，请稍后再试",
    "daily_tokens": "今日 token 额度已用完",
    "monthly_tokens": "本月 token 额度已用完",
    "max_workspaces": "工作环境数量已达上限",
    "workspace_disk_bytes": "工作区磁盘空间已满，请清理文件后重试",
    "max_disk_bytes": "租户磁盘空间已满，请清理文件后重试",
    "upstream_rpm": "模型请求过于频繁，请稍后再试",
    "upstream_concurrency": "模型并发请求已达上限，请稍后再试",
}


_GENERIC_FORBIDDEN = "没有权限执行该操作"  # 网关 ApiError.forbidden 的缺省文案


def user_message(err: GatewayError) -> str:
    """面向用户的中文文案（SSE error 帧 / 领域异常共用）。"""
    code = err.code
    if code == "quota_exceeded":
        quota = str(err.detail_dict.get("quota") or "")
        return _QUOTA_TEXT.get(quota, f"已达到配额上限（{quota or '未知项'}），请稍后再试")
    if code == "rate_limited":
        return "请求过于频繁，请稍后再试"
    if code == "workspace_busy":
        return "该会话（或工作环境）正在回答中，请等待完成或先停止"
    if code == "unsupported":
        return f"当前智能体不支持该输入：{err.message}"
    if code == "payload_too_large":
        return f"内容过大：{err.message}"
    if code == "path_forbidden":
        return f"路径不合法：{err.message}"
    if code == "agent_unavailable":
        return f"智能体运行环境暂不可用，请稍后重试：{err.message}"
    if err.status in (401, 403) and code in ("unauthorized", "forbidden"):
        if err.status == 403 and err.message and err.message != _GENERIC_FORBIDDEN:
            # 网关给出了具体原因（如 MCP 地址不在租户白名单）：原样带上，别笼统说成凭据问题
            return f"ACP 网关拒绝了请求：{err.message}"
        return "ACP 网关凭据无效或权限不足，请联系管理员检查 API Key 与 scope"
    if err.status >= 500:
        return f"ACP 网关内部错误：{err.message}"
    return err.message


def to_domain_error(err: GatewayError | GatewayTransportError) -> LoomvecError:
    """网关错误 → loomvec 领域异常（HTTP 路由层 raise；SSE 内部用 `user_message` 出帧）。"""
    if isinstance(err, GatewayTransportError):
        return UpstreamUnavailableError(UPSTREAM, f"无法连接 ACP 网关：{err.reason}")
    msg = user_message(err)
    if err.status in (401, 403) and err.code in ("unauthorized", "forbidden"):
        return GatewayConfigError(msg)
    if err.status == 404:
        return NotFoundError(msg)
    if err.status == 429 or err.code in ("quota_exceeded", "rate_limited"):
        return RateLimitedError(msg)
    if err.status == 409:
        return ConflictError(msg)
    if err.status >= 500:
        return UpstreamUnavailableError(UPSTREAM, msg)
    return ValidationError(msg)
