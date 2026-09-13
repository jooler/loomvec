"""P0-CORE-03 错误类型体系：领域异常 → HTTP 映射。

全局错误结构（契约）：`{"code", "message", "details"}`，
REST / SDK / Webhook 共用；api 层只做透传映射，不另造格式。
"""

from __future__ import annotations

from typing import Any


class LoomvecError(Exception):
    """领域异常基类。子类通过类属性声明契约与 HTTP 语义。"""

    code: str = "internal_error"
    http_status: int = 500
    message: str = "Internal server error"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(LoomvecError):
    code = "validation_error"
    http_status = 422
    message = "请求参数不合法"


class UnauthenticatedError(LoomvecError):
    code = "unauthenticated"
    http_status = 401
    message = "未认证或凭证已失效"


class PermissionDeniedError(LoomvecError):
    code = "permission_denied"
    http_status = 403
    message = "无权执行该操作"


class NotFoundError(LoomvecError):
    code = "not_found"
    http_status = 404
    message = "资源不存在"


class ConflictError(LoomvecError):
    code = "conflict"
    http_status = 409
    message = "资源状态冲突"


class RateLimitedError(LoomvecError):
    code = "rate_limited"
    http_status = 429
    message = "请求过于频繁"


class UpstreamUnavailableError(LoomvecError):
    """依赖组件（Milvus/MinerU/AI 网关等）不可用或调用失败。"""

    code = "upstream_unavailable"
    http_status = 502
    message = "依赖服务不可用"

    def __init__(self, upstream: str, message: str | None = None, **details: Any) -> None:
        super().__init__(message, upstream=upstream, **details)


ERROR_TO_PAYLOAD_ATTR = "to_payload"
