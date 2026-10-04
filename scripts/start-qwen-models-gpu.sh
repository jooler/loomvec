#!/usr/bin/env bash
# Qwen3-Embedding-0.6B + Qwen3-Reranker-0.6B 本地服务。
# 对外提供与现有 Infinity 相同的 /embeddings、/rerank、/health、/models 接口。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

PYTHON="$ROOT/.venv-infinity/bin/python"
MODELS="$ROOT/tmp/models"
LOG_DIR="$ROOT/tmp"
PID_FILE="$LOG_DIR/models.pids"
PORT="${MODELS_INFINITY_PORT:-38011}"
EMB_DIR="$MODELS/qwen3-embedding-0.6b"
RERANK_DIR="$MODELS/qwen3-reranker-0.6b"
WANT_EMBEDDING="${MODELS_LOAD_EMBEDDING:-1}"
WANT_RERANK="${MODELS_LOAD_RERANK:-1}"
mkdir -p "$LOG_DIR"

ok() { echo "✓ $*"; }
fail() { echo "✗ $*"; }
port_up() { nc -z localhost "$PORT" >/dev/null 2>&1; }
alive() { [ -f "$PID_FILE" ] && grep -q '^qwen=' "$PID_FILE" && kill -0 "$(sed -n 's/^qwen=//p' "$PID_FILE" | head -1)" 2>/dev/null; }
want() { [ "$1" = "1" ]; }
save_pid() {
  [ -f "$PID_FILE" ] && sed -i '/^qwen=/d' "$PID_FILE"
  echo "qwen=$1" >>"$PID_FILE"
}

do_start() {
  [ -x "$PYTHON" ] || { fail "缺少 $PYTHON"; exit 1; }
  want "$WANT_EMBEDDING" || want "$WANT_RERANK" || {
    fail "embedding 和 rerank 均未启用（MODELS_LOAD_EMBEDDING/MODELS_LOAD_RERANK）"
    exit 1
  }
  if want "$WANT_EMBEDDING"; then
    compgen -G "$EMB_DIR/*.safetensors" >/dev/null || compgen -G "$EMB_DIR/*.bin" >/dev/null || {
      fail "缺少 Qwen3 embedding 权重：$EMB_DIR"
      exit 1
    }
  fi
  if want "$WANT_RERANK"; then
    compgen -G "$RERANK_DIR/*.safetensors" >/dev/null || compgen -G "$RERANK_DIR/*.bin" >/dev/null || {
      fail "缺少 Qwen3 reranker 权重：$RERANK_DIR"
      exit 1
    }
  fi
  if port_up; then
    ok "Qwen3 已在运行（:$PORT），跳过"
  else
    local embedding_arg=() reranker_arg=()
    want "$WANT_EMBEDDING" && embedding_arg=(--embedding-model "$EMB_DIR")
    want "$WANT_RERANK" && reranker_arg=(--reranker-model "$RERANK_DIR")
    PYTHONUNBUFFERED=1 nohup "$PYTHON" "$ROOT/scripts/qwen_models_server.py" \
      "${embedding_arg[@]}" "${reranker_arg[@]}" \
      --host 127.0.0.1 --port "$PORT" >"$LOG_DIR/dev-qwen.log" 2>&1 &
    save_pid $!
    ok "Qwen3 已启动（pid $(sed -n 's/^qwen=//p' "$PID_FILE" | head -1)）"
  fi
  local i=0
  while [ "$i" -lt 60 ]; do
    if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health"; then
      ok "Qwen3 :$PORT 就绪"
      return 0
    fi
    i=$((i + 5)); sleep 5
  done
  fail "Qwen3 在 300s 内未就绪；查看 tmp/dev-qwen.log"
  return 1
}

do_stop() {
  if alive; then
    local pid
    pid="$(sed -n 's/^qwen=//p' "$PID_FILE" | head -1)"
    kill "$pid" 2>/dev/null || true
    local i=0
    while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 15 ]; do sleep 2; i=$((i + 2)); done
    kill -9 "$pid" 2>/dev/null || true
    ok "已停止 Qwen3（pid $pid）"
  else
    echo "- Qwen3 未在运行"
  fi
  [ -f "$PID_FILE" ] && sed -i '/^qwen=/d' "$PID_FILE"
}

do_status() {
  if port_up && curl -sf -o /dev/null "http://127.0.0.1:$PORT/health"; then
    ok "Qwen3 :$PORT 健康"
  else
    fail "Qwen3 :$PORT 不可用（./scripts/start-qwen-models-gpu.sh start）"
    return 1
  fi
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  status) do_status ;;
  logs) exec tail -n 40 -F "$LOG_DIR/dev-qwen.log" ;;
  *) echo "用法: $0 start | stop | status | logs"; exit 1 ;;
esac
