#!/bin/bash
# MinerU 容器入口：首次启动下载 basic/flash 档位模型到 /models（volume 缓存），
# 随后以 basic 服务器档位启动 mineru-api（暴露 flash+basic 请求档位；
# standard/advanced 需 VLM 引擎，CPU 部署不提供）。
set -euo pipefail

export MINERU_MODEL_SOURCE=modelscope
export MINERU_HOME=/models
export MODELSCOPE_CACHE=/models/modelscope
export HF_HOME=/models/huggingface
# The application-level MinerU slot limit must match this server limit.  Keep
# one as the safe image default; deployment may raise it after confirming CPU
# and memory headroom.
export MINERU_CONCURRENCY="${MINERU_CONCURRENCY:-1}"
# ONNX consumes this environment variable when creating CPU inference sessions.
# Keep the image default conservative; deploy overrides must be sized together
# with MINERU_CONCURRENCY to avoid CPU oversubscription.
export MINERU_INTRA_OP_NUM_THREADS="${MINERU_INTRA_OP_NUM_THREADS:-4}"

if [ ! -f /models/.models-ready ]; then
  echo "[entrypoint] 下载解析模型到 /models（首次启动约 1~2GB，请耐心等待）..."
  mineru-models-download --tier basic --source modelscope
  mkdir -p /models && touch /models/.models-ready
  echo "[entrypoint] 模型下载完成。"
fi

exec mineru-api --host 0.0.0.0 --port 8000 --tier basic --concurrency "$MINERU_CONCURRENCY"
