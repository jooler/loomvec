"""agent 服务 FastAPI 工厂（风格对齐 services/api/app.py）。

内网 only：不挂限流/RequestID（由 api facade 承担），只保留内部令牌闸门。
lifespan：日志 → PG/Redis → RuntimeManager（惰性 spawn + 空闲回收）→ 路由。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette import status

import loomvec.agent
from loomvec.agent.config import load_agent_config
from loomvec.agent.routes import router
from loomvec.agent.runtime.manager import RuntimeManager
from loomvec.agent.sessions.orchestrator import PromptOrchestrator
from loomvec.core.db.base import create_engine_and_sessionmaker
from loomvec.core.errors import LoomvecError
from loomvec.core.logging import get_logger, setup_logging

logger = get_logger("loomvec.agent")


def _payload(code: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "message": message, "details": details or {}}


def create_app() -> FastAPI:
    cfg = load_agent_config()
    setup_logging(cfg.settings.log_level, json_output=cfg.settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine, session_factory = create_engine_and_sessionmaker(cfg.settings.postgres)
        redis_client = aioredis.from_url(cfg.settings.redis.url, decode_responses=True)
        manager = RuntimeManager(cfg, redis_client)
        orchestrator = PromptOrchestrator(cfg, manager, redis_client)
        await manager.start_reaper()
        app.state.agent_config = cfg
        app.state.settings = cfg.settings
        app.state.session_factory = session_factory
        app.state.redis = redis_client
        app.state.runtime_manager = manager
        app.state.orchestrator = orchestrator
        logger.info(
            "agent_started",
            service="loomvec-agent",
            version=loomvec.agent.__version__,
            storage_root=str(cfg.storage_root),
        )
        try:
            yield
        finally:
            await manager.stop_reaper()
            await redis_client.aclose()
            await engine.dispose()
            logger.info("agent_stopped")

    app = FastAPI(
        title="LoomVec Agent Gateway", version=loomvec.agent.__version__, lifespan=lifespan
    )

    # 领域异常 → 契约错误结构（与 services/api/errors.py 同语义；api 侧按
    # 上游状态码透传映射，缺此处理器时校验错误会变成 500/502）
    @app.exception_handler(LoomvecError)
    async def domain_error_handler(_: Request, exc: LoomvecError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        for e in errors:
            e.pop("url", None)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=_payload("validation_error", "请求参数不合法", {"errors": errors}),
        )

    app.include_router(router)
    return app


app = create_app()
