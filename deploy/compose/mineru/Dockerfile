# MinerU 4.0 解析镜像：basic/flash 档位（ONNX 小模型）+ mineru-api（V1 HTTP 服务）
# MinerU 以 PyPI 固定版本依赖安装，源码不入库；升级只改 MINERU_VERSION。
# License（Apache-2.0+附加条款）随 wheel 分发，条款复核见 docs/13。
# CPU 上 small_backend 自动解析为 onnx，无需 torch；GPU/VLM 档位
# （standard/advanced，需 mineru[full]+vllm）见 MinerU 官方 docker/。
FROM python:3.12-slim

ARG MINERU_VERSION=4.0.10

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 模型缓存与 config.yaml 全部落在 /models（compose 卷 mineru_models，经 MINERU_HOME）
    MINERU_HOME=/models \
    MINERU_MODEL_SOURCE=modelscope \
    MODELSCOPE_CACHE=/models/modelscope \
    HF_HOME=/models/huggingface

# opencv 运行库 + 中文字体 + 健康检查 curl
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libgl1 libglib2.0-0 libgomp1 curl fonts-noto-cjk fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir "mineru==${MINERU_VERSION}"

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
