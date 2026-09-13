#!/bin/bash
# MinerU 容器入口：首次启动下载 pipeline 模型到 /models（volume 缓存），随后启动 mineru-api。
set -euo pipefail

export MINERU_MODEL_SOURCE=modelscope
export MINERU_TOOLS_CONFIG_JSON=/models/mineru.json
export MODELSCOPE_CACHE=/models/modelscope
export HF_HOME=/models/huggingface

if [ ! -f /models/.models-ready ]; then
  echo "[entrypoint] 下载 pipeline 模型到 /models（首次启动约 1~2GB，请耐心等待）..."
  mineru-models-download -s modelscope -m pipeline
  mkdir -p /models && touch /models/.models-ready
  echo "[entrypoint] 模型下载完成。"
fi

exec mineru-api --host 0.0.0.0 --port 8000
