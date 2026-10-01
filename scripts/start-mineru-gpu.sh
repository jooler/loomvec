#!/usr/bin/env bash
# Local MinerU API with NVIDIA GPU (replaces compose CPU image when Docker GPU unavailable).
# MinerU 4.x：模型与 config 落 $MINERU_HOME；basic 档（flash+basic，ONNX/torch 小模型），
# standard/advanced 需 VLM 引擎（mineru[full] + vllm），此处不启用。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="$ROOT/.venv-mineru"
MODELS="$ROOT/tmp/mineru-models"
OUTPUT="$ROOT/tmp/mineru-output"
PORT="${MINERU_PORT:-38000}"
export PATH="$HOME/.local/bin:$PATH"
export MINERU_DEVICE_MODE="${MINERU_DEVICE_MODE:-cuda}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export MINERU_MODEL_SOURCE="${MINERU_MODEL_SOURCE:-modelscope}"
export MINERU_HOME="${MINERU_HOME:-$MODELS}"
export MODELSCOPE_CACHE="${MODELSCOPE_CACHE:-$MODELS/modelscope}"
export HF_HOME="${HF_HOME:-$MODELS/huggingface}"
mkdir -p "$MODELS" "$OUTPUT" "$MODELSCOPE_CACHE" "$HF_HOME"

if [ ! -x "$VENV/bin/mineru-api" ]; then
  echo "MinerU venv missing; run: uv pip install --python .venv-mineru 'mineru[full]==4.0.10'" >&2
  exit 1
fi

# First-run model download for basic/flash tiers (small_backend auto → torch on GPU)
if [ ! -f "$MODELS/.models-ready" ]; then
  echo "[mineru-gpu] downloading basic-tier models to $MODELS ..."
  "$VENV/bin/mineru-models-download" --tier basic --source modelscope
  touch "$MODELS/.models-ready"
fi

echo "[mineru-gpu] MINERU_DEVICE_MODE=$MINERU_DEVICE_MODE CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "[mineru-gpu] listening http://127.0.0.1:${PORT}"
exec "$VENV/bin/mineru-api" --host 127.0.0.1 --port "$PORT" --tier basic --upload-dir "$OUTPUT"
