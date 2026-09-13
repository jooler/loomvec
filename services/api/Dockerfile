# LoomVec API 镜像（构建上下文 = 仓库根）
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# 先拷贝依赖清单以利用层缓存
COPY pyproject.toml uv.lock .python-version ./
COPY services/core/pyproject.toml services/core/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
RUN uv sync --frozen --no-install-workspace --no-dev

# 拷贝源码并安装 workspace 包
COPY services ./services
RUN uv sync --frozen --no-dev

EXPOSE 8000
# 容器内指向 compose 服务名
ENV LOOMVEC_POSTGRES__URL=postgresql+asyncpg://loomvec:loomvec@postgres:5432/loomvec \
    LOOMVEC_REDIS__URL=redis://redis:6379/0 \
    LOOMVEC_STORAGE__ENDPOINT=http://rustfs:9000 \
    LOOMVEC_MINERU__BASE_URL=http://mineru:8000 \
    LOOMVEC_MILVUS__URI=http://milvus:19530

CMD ["uv", "run", "--no-sync", "uvicorn", "loomvec.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
