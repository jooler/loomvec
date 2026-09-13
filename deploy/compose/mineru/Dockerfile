# MinerU 3.4 CPU 镜像：pipeline 后端 + mineru-api（FastAPI HTTP 服务）
# 构建上下文为仓库内 vendored 源码（third_party/mineru）。
# GPU/VLM 后端见 MinerU 官方 docker/（vllm 基础镜像，需 NVIDIA GPU）。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 模型缓存卷约定：下载缓存与 mineru.json 全部落在 /models（compose 卷 mineru_models）
    MINERU_MODEL_SOURCE=modelscope \
    MINERU_TOOLS_CONFIG_JSON=/models/mineru.json \
    MODELSCOPE_CACHE=/models/modelscope \
    HF_HOME=/models/huggingface

# opencv 运行库 + 中文字体 + 健康检查 curl
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libgl1 libglib2.0-0 libgomp1 curl fonts-noto-cjk fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app/mineru-src
# six：pipeline 依赖链中部分包运行时隐式依赖，未在 mineru 元数据中声明
RUN pip install --no-cache-dir "/app/mineru-src[pipeline]" six

COPY --from=deploy entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
