"""FastAPI 中间件链：请求 ID → 限流 → 访问日志。

限流计数实现在 ratelimit.py（Redis 固定窗口，app 工厂装配）；
tracing instrumentation 亦在 app 工厂装配。
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from loomvec.core.logging import bind_request_id, get_logger, request_id_var
from loomvec.core.metrics import http_request_duration_seconds, http_requests_total

logger = get_logger("loomvec.api.access")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """为每个请求生成/透传请求 ID（X-Request-Id），并绑定到日志 contextvar。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get("x-request-id")
        request_id = bind_request_id(incoming)
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        path = request.scope.get("route").path if "route" in request.scope else request.url.path
        logger.info(
            "http_request",
            method=request.method,
            path=path,
            status=response.status_code,
            duration_ms=round(elapsed * 1000, 2),
        )
        http_requests_total.labels(request.method, path, str(response.status_code)).inc()
        http_request_duration_seconds.labels(request.method, path).observe(elapsed)
        return response
