#!/usr/bin/env bash
# Local MinerU API with NVIDIA GPU (replaces compose CPU image when Docker GPU unavailable).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VENV="$ROOT/.venv-mineru"
MODELS="$ROOT/tmp/mineru-models"
PORT="${MINERU_PORT:-38000}"
export PATH="$HOME/.local/bin:$PATH"
export MINERU_DEVICE_MODE="${MINERU_DEVICE_MODE:-cuda}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export MINERU_MODEL_SOURCE="${MINERU_MODEL_SOURCE:-modelscope}"
export MINERU_TOOLS_CONFIG_JSON="${MINERU_TOOLS_CONFIG_JSON:-$MODELS/mineru.json}"
export MODELSCOPE_CACHE="${MODELSCOPE_CACHE:-$MODELS/modelscope}"
export HF_HOME="${HF_HOME:-$MODELS/huggingface}"
export MINERU_API_OUTPUT_ROOT="${MINERU_API_OUTPUT_ROOT:-$ROOT/tmp/mineru-output}"
mkdir -p "$MODELS" "$MINERU_API_OUTPUT_ROOT" "$MODELSCOPE_CACHE" "$HF_HOME"

if [ ! -x "$VENV/bin/mineru-api" ]; then
  echo "MinerU venv missing; run: uv pip install --python .venv-mineru -e './third_party/mineru[pipeline]' six" >&2
  exit 1
fi

# First-run model download for pipeline backend
if [ ! -f "$MODELS/.models-ready" ]; then
  echo "[mineru-gpu] downloading pipeline models to $MODELS ..."
  "$VENV/bin/mineru-models-download" -s modelscope -m pipeline
  touch "$MODELS/.models-ready"
fi

echo "[mineru-gpu] MINERU_DEVICE_MODE=$MINERU_DEVICE_MODE CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
echo "[mineru-gpu] listening http://127.0.0.1:${PORT}"
exec "$VENV/bin/mineru-api" --host 127.0.0.1 --port "$PORT"
