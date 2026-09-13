"""P4-INF-04 安全配置：全局限流中间件（策略定稿）。

限流（定稿）：
- 全局：按身份（JWT sub / API Key id / 客户端 IP 兜底）固定窗口，
  `LOOMVEC_SECURITY__RATE_LIMIT_PER_MIN`（默认 300 req/min），Redis 计数；
- Redis 不可用时 fail-open（不阻塞业务），仅记 warning；
- API Key 级限流维持 P1 语义（key 行 rate_limit_per_min，位于 identity 层，
  更精确；本中间件为全局兜底，覆盖 JWT 与匿名流量）。

密钥管理约定：全部密钥经环境注入（K8s Secret / 外部 KMS → env），
禁止写入镜像与代码库；sensitive 配置键经管理 API 写入时脱敏回显。
"""

from __future__ import annotations

import hashlib
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from loomvec.core.config import Settings
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.api.ratelimit")

RATE_LIMIT_KEY_PREFIX = "loomvec:rl:global"
RATE_WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    """全局固定窗口限流（P4-INF-04）：按身份计数，fail-open。"""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        security = self._settings.security
        if not security.rate_limit_enabled:
            return await call_next(request)

        redis = getattr(request.app.state, "redis", None)
        if redis is None:
            return await call_next(request)

        identity = getattr(request.state, "identity", None)
        subject = getattr(identity, "user_id", None)
        if subject is None:
            api_key = request.headers.get("x-api-key")
            if api_key:
                # 不以明文 key 作 Redis 键，落哈希前缀
                subject = hashlib.sha256(api_key.encode()).hexdigest()[:16]
            else:
                subject = request.client.host if request.client else "anonymous"

        window = int(time.time()) // RATE_WINDOW_SECONDS
        key = f"{RATE_LIMIT_KEY_PREFIX}:{subject}:{window}"
        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, RATE_WINDOW_SECONDS)
            if count > security.rate_limit_per_min:
                return JSONResponse(
                    status_code=429,
                    content={
                        "code": "rate_limited",
                        "message": "请求过于频繁",
                        "details": {
                            "limit": security.rate_limit_per_min,
                            "window": f"{RATE_WINDOW_SECONDS}s",
                        },
                    },
                )
        except Exception as e:  # fail-open：限流组件故障不阻塞业务
            logger.warning("rate_limit_check_failed", error=str(e))
            return await call_next(request)

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(security.rate_limit_per_min)
        return response
