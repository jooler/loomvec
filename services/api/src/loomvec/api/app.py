"""P0-API-01 FastAPI 应用工厂。

启动链路（lifespan）：日志 → tracing → 引擎/Redis/存储 → 中间件 → 路由。
中间件链：RequestID → RateLimit（Redis 固定窗口）→ AccessLog（Prometheus 计数）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import loomvec.api
from loomvec.api import auth as auth_module
from loomvec.api.errors import register_exception_handlers
from loomvec.api.middleware import AccessLogMiddleware, RequestIDMiddleware
from loomvec.api.ratelimit import RateLimitMiddleware
from loomvec.api.routes import agent as agent_routes
from loomvec.api.routes import graph as graph_routes
from loomvec.api.routes import mcp as mcp_routes
from loomvec.api.routes.admin import admin_api_router
from loomvec.api.routes.api_keys import router as api_keys_router
from loomvec.api.routes.assets import router as assets_router
from loomvec.api.routes.auth_oidc import router as oidc_router
from loomvec.api.routes.folders import router as folders_router
from loomvec.api.routes.health import router as health_router
from loomvec.api.routes.jobs import router as jobs_router
from loomvec.api.routes.me import router as me_router
from loomvec.api.routes.members import router as members_router
from loomvec.api.routes.notifications import router as notifications_router
from loomvec.api.routes.oauth import router as oauth_router
from loomvec.api.routes.ops import ops_api_router
from loomvec.api.routes.playback import router as playback_router
from loomvec.api.routes.public_spaces import router as public_spaces_router
from loomvec.api.routes.review import router as review_router
from loomvec.api.routes.search import router as search_router
from loomvec.api.routes.spaces import router as spaces_router
from loomvec.api.routes.units import router as units_router
from loomvec.api.routes.uploads import router as uploads_router
from loomvec.core.ai import AiGateway
from loomvec.core.config import Env, Settings, get_settings
from loomvec.core.db.base import create_engine_and_sessionmaker
from loomvec.core.logging import get_logger, setup_logging
from loomvec.core.metrics import registry
from loomvec.core.retrieval import MilvusStore, Retriever
from loomvec.core.retrieval.graph_retrieval import GraphRetriever
from loomvec.core.storage import ObjectStorage
from loomvec.core.tracing import setup_tracing

logger = get_logger("loomvec.api")


def _make_celery(settings: Settings):
    """仅用于 send_task 的轻量 Celery 句柄（任务体在 worker 进程执行）。"""
    from celery import Celery

    return Celery("loomvec", broker=settings.redis.url, backend=settings.redis.url)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, json_output=settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_tracing(settings)
        engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
        redis_client = aioredis.from_url(settings.redis.url, decode_responses=True)
        storage = ObjectStorage(settings.storage)
        ai = AiGateway(settings.ai)
        milvus = MilvusStore(settings.milvus, settings.ai)
        graph_retriever = GraphRetriever(milvus, settings.graph, settings.search)
        retriever = Retriever(milvus, ai, settings.search, graph=graph_retriever)
        celery = _make_celery(settings)
        # P5：agent facade 转发客户端（长连接池；SSE 透传不设总超时）。
        # trust_env=False：内网服务直连，不读 HTTP(S)_PROXY 等环境代理配置
        # （代理会把 127.0.0.1 的内部转发劫走，表现为 agent 502）。
        agent_client = httpx.AsyncClient(
            base_url=settings.agent_service.service_url,
            timeout=httpx.Timeout(600.0, connect=5.0),
            trust_env=False,
        )
        # P5：MCP endpoint（streamable-http，仅 agent token；挂载须在 state 就绪后）
        mcp_app, mcp_lifespan = mcp_routes.build_mcp_app(
            mcp_routes.McpAppState(
                settings=settings,
                retriever=retriever,
                session_factory=session_factory,
                redis=redis_client,
            )
        )
        app.mount("/api/v1/mcp", mcp_app)
        try:
            if settings.env is Env.DEV:
                storage.ensure_buckets()
        except Exception:  # 启动时存储不可用不应阻塞进程
            logger.warning("storage_ensure_buckets_failed")
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis_client
        app.state.storage = storage
        app.state.ai = ai
        app.state.milvus = milvus
        app.state.graph_retriever = graph_retriever
        app.state.retriever = retriever
        app.state.celery = celery
        app.state.agent_client = agent_client
        logger.info("api_started", service=settings.service_name, version=loomvec.api.__version__)
        async with mcp_lifespan:
            try:
                yield
            finally:
                await agent_client.aclose()
        await redis_client.aclose()
        await ai.aclose()
        await engine.dispose()
        logger.info("api_stopped")

    app = FastAPI(
        title="LoomVec API",
        version=loomvec.api.__version__,
        lifespan=lifespan,
    )

    # ---- 中间件链（后注册的先执行）----
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RateLimitMiddleware, settings=settings)  # P4-INF-04 全局限流
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)

    # ---- 路由 ----
    app.include_router(health_router)
    app.include_router(me_router)
    app.include_router(spaces_router)
    app.include_router(members_router)
    app.include_router(uploads_router)
    app.include_router(assets_router)
    app.include_router(folders_router)  # Finder 文件夹域（目录树/移动/复制/级联删除）
    app.include_router(units_router)  # chunk 管理（查看/新增/编辑/批删，即时向量化）
    app.include_router(jobs_router)
    app.include_router(review_router)
    app.include_router(search_router)
    app.include_router(agent_routes.router)  # P5 智能体对话（dsh 全量接管，SSE）
    app.include_router(public_spaces_router)  # P5 公共空间链接（用户端开关）
    app.include_router(ops_api_router)  # P5 运营 API 域（公共空间/用户分组）
    app.include_router(graph_routes.router)  # P3-API-03 图谱管理
    app.include_router(playback_router)  # P3-API-04 播放（懒转码/关键帧/字幕）
    app.include_router(notifications_router)
    app.include_router(api_keys_router)
    app.include_router(admin_api_router)  # P4-API 管理 API 域
    app.include_router(oauth_router)  # P4-API-07 OAuth 授权码流程
    app.include_router(oidc_router)  # P4-API-06 OIDC SSO
    auth_module.register_auth_routes(app, settings)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        # 抓取时刷新运行时 gauge（队列/组件/配额；失败不阻塞导出）
        from loomvec.api.metrics_runtime import refresh_runtime_metrics

        try:
            await refresh_runtime_metrics(app.state)
        except Exception:
            logger.debug("metrics_refresh_failed")
        # 直接路由而非 Mount：避免 "/metrics" → "/metrics/" 307 重定向（Prometheus 不跟随）
        return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
