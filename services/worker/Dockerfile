# LoomVec Worker 镜像（构建上下文 = 仓库根）
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY services/core/pyproject.toml services/core/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
RUN uv sync --frozen --no-install-workspace --no-dev

COPY services ./services
RUN uv sync --frozen --no-dev

ENV LOOMVEC_POSTGRES__URL=postgresql+asyncpg://loomvec:loomvec@postgres:5432/loomvec \
    LOOMVEC_REDIS__URL=redis://redis:6379/0 \
    LOOMVEC_STORAGE__ENDPOINT=http://rustfs:9000

# ffmpeg/ffprobe：视频场景关键帧与懒转码（P3-WRK-03/05）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# P3 可选重型依赖（场景切分/转写/Leiden）；构建体积敏感时可移除 media/communities extra
RUN uv sync --frozen --no-dev --all-extras

CMD ["uv", "run", "--no-sync", "celery", "-A", "loomvec.worker.celery_app:celery_app", "worker", "-l", "info", "-Q", "pipeline,pipeline_high,pipeline_low"]
