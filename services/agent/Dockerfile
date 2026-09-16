# LoomVec Agent 网关镜像（构建上下文 = 仓库根；P5，14 文档 §11）
# deepseek-harness-sdk 的 runtime wheel 自带 Node 运行时与前端资产，无需系统 Node。
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# 先拷贝依赖清单以利用层缓存
COPY pyproject.toml uv.lock .python-version ./
COPY services/core/pyproject.toml services/core/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/agent/pyproject.toml services/agent/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
RUN uv sync --frozen --no-install-workspace --no-dev

# 拷贝源码并安装 workspace 包
COPY services ./services
RUN uv sync --frozen --no-dev

EXPOSE 8090
# 容器内指向 compose 服务名；env 目录树挂命名卷（P2 起 per-env PVC）。
# 应用参数（模型/MCP url 等）经挂载的 config/loomvec.json 注入（来源唯一）。
ENV LOOMVEC_POSTGRES__URL=postgresql+asyncpg://loomvec:loomvec@postgres:5432/loomvec \
    LOOMVEC_REDIS__URL=redis://redis:6379/0 \
    LOOMVEC_AGENT_STORAGE_ROOT=/data/loomvec/agent-envs

CMD ["uv", "run", "--no-sync", "uvicorn", "loomvec.agent.app:app", "--host", "0.0.0.0", "--port", "8090"]
