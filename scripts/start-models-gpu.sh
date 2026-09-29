#!/usr/bin/env bash
# 本地模型推理服务（宿主机 NVIDIA GPU）：vLLM（文本生成）+ Infinity（向量 / 重排 / CLIP）。
# 与 scripts/start-mineru-gpu.sh 同模式：独立 venv、模型权重预下载到 tmp/models、只监听 127.0.0.1。
#
# 用法：
#   ./scripts/start-models-gpu.sh start          # 首次自动下载模型；启动并等待健康（vLLM 首次加载 1~2 分钟）
#   ./scripts/start-models-gpu.sh stop
#   ./scripts/start-models-gpu.sh status
#   ./scripts/start-models-gpu.sh logs [vllm|infinity|all]
#
# 端口（env 可覆盖）：
#   vLLM     38010（MODELS_LLM_PORT）      OpenAI 兼容 /v1/chat/completions → ai.llm
#   Infinity 38011（MODELS_INFINITY_PORT）  /embeddings + /rerank           → ai.embedding / ai.rerank / ai.clip
# 通道选择性加载（env 开关，0 = 跳过该通道的权重下载与服务启动；手动运行缺省全载。
# ./dev.sh start 按 config/loomvec.json 的 ai.*.base_url 是否指向本地端口自动注入）：
#   MODELS_LOAD_LLM / MODELS_LOAD_EMBEDDING / MODELS_LOAD_RERANK / MODELS_LOAD_CLIP
# 配套模板 config/loomvec.local-models.example.json：把 ai 段合并进 config/loomvec.json 后
# 重启 api/worker/agent 生效（AiGateway 启动时快照配置）。详见 docs/16-本地模型推理.md。
#
# 首次安装（两 venv 共约 20GB 磁盘，脚本不代装）：
#   uv venv .venv-vllm     --python 3.12 && uv pip install --python .venv-vllm/bin/python vllm
#   uv venv .venv-infinity --python 3.12 && uv pip install --python .venv-infinity/bin/python \
#     "infinity-emb[server,torch]" modelscope "typer>=0.16.1" einops timm torchvision aiohttp
#   （typer 需 >=0.16.1：infinity-emb 0.0.77 声明的旧 typer 与 click>=8.3 冲突，
#    启动报 "Secondary flag is not valid for non-boolean flag"；
#    einops/timm/torchvision/aiohttp 是 jina-clip-v2 远程代码与图片输入的 import 依赖）
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH"

VLLM_VENV="$ROOT/.venv-vllm"
INF_VENV="$ROOT/.venv-infinity"
MODELS="$ROOT/tmp/models"
LOG_DIR="$ROOT/tmp"; PID_FILE="$LOG_DIR/models.pids"; mkdir -p "$MODELS" "$LOG_DIR"

LLM_PORT="${MODELS_LLM_PORT:-38010}"
INF_PORT="${MODELS_INFINITY_PORT:-38011}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
# 下载走 HF：禁用 Xet（其 CDN 在部分网络不可达）；国内默认 hf-mirror（可覆盖）。
# 不要装 hf_transfer（高并发下载会中途报错）
export HF_HUB_DISABLE_XET=1
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

# 对话模型（HF repo id；换档位改这里或 export MODELS_LLM_ID，如 Qwen/Qwen3-4B-Instruct-2507-FP8）。
# 默认 1.5B AWQ 小模型：与 Infinity 三模型 + MinerU 共 16GB 卡实测校准（向量/重排优先保显存；
# 选非思考型 instruct，避免 <think> 烧掉 max_tokens）。未来对话走云供方时 ai.llm.base_url
# 切回云端即可，本地服务可停
LLM_ID="${MODELS_LLM_ID:-Qwen/Qwen2.5-1.5B-Instruct-AWQ}"
LLM_DIR="$MODELS/${LLM_ID##*/}"
LLM_ALIAS="${MODELS_LLM_ALIAS:-qwen2.5-1.5b}"
# vLLM 显存预算（16GB 卡实测校准）：util 0.22 ≈ 3.4GB（AWQ 权重 1.6GB + 激活 + KV）；
# 1.5B 模型 KV 便宜（16384 上下文 ≈ 0.9GB，dsh 提示词+工具约 4.1K token + 4K 输出需要它）
VLLM_GPU_UTIL="${MODELS_VLLM_GPU_UTIL:-0.22}"
VLLM_MAX_LEN="${MODELS_VLLM_MAX_MODEL_LEN:-16384}"

EMB_DIR="$MODELS/bge-m3"
RERANK_DIR="$MODELS/bge-reranker-v2-m3"
CLIP_DIR="$MODELS/jina-clip-v2"

# 通道选择性加载：0 = 跳过该通道（下载 + 启动都跳）；缺省 1 = 全载
WANT_LLM="${MODELS_LOAD_LLM:-1}"
WANT_EMBEDDING="${MODELS_LOAD_EMBEDDING:-1}"
WANT_RERANK="${MODELS_LOAD_RERANK:-1}"
WANT_CLIP="${MODELS_LOAD_CLIP:-1}"
want() { [ "$1" = "1" ]; }
want_infinity() { want "$WANT_EMBEDDING" || want "$WANT_RERANK" || want "$WANT_CLIP"; }

RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
ok()   { echo "${GRN}✓${RST} $*"; }
warn() { echo "${YLW}!${RST} $*"; }
fail() { echo "${RED}✗${RST} $*"; }
step() { echo "\n${DIM}── $* ──${RST}"; }

port_up() { nc -z localhost "$1" >/dev/null 2>&1; }
alive()   { [ -f "$PID_FILE" ] && grep -q "^$1=" "$PID_FILE" && kill -0 "$(sed -n "s/^$1=//p" "$PID_FILE" | head -1)" 2>/dev/null; }
save_pid() {
  if [ -f "$PID_FILE" ]; then sed -i "/^$1=/d" "$PID_FILE"; fi
  echo "$1=$2" >>"$PID_FILE"
}

wait_http() { # $1=名称 $2=url $3=超时秒
  local i=0 total=$3
  while [ "$i" -lt "$total" ]; do
    curl -sf -o /dev/null "$2" && { ok "$1 就绪（$2）"; return 0; }
    i=$((i + 5)); sleep 5
  done
  fail "$1 在 ${total}s 内未就绪（$2）；查看日志：./scripts/start-models-gpu.sh logs $1"
  return 1
}

download() { # $1=HF repo id $2=本地目录（""=只进 HF 缓存） $3=可选额外参数；哨兵 .download-ok 防重复
  local sent="$MODELS/$2/.download-ok"
  # $1 含 /（如 jinaai/jina-embeddings-v3）：换掉斜杠，保证哨兵路径可创建
  [ -n "$2" ] || sent="$MODELS/.dl-${1//\//-}-ok"
  if [ -f "$sent" ]; then return 0; fi
  local extra_args=()
  # $3 可选：set -u 下未传时须用 ${3:-} 判空，否则整脚本 unbound variable 退出
  [ -n "${3:-}" ] && extra_args=($3)
  [ -n "$2" ] && extra_args+=(--local-dir "$MODELS/$2")
  local attempt
  for attempt in 1 2 3; do
    echo "下载 $1 ${extra_args[*]:-（进 HF 缓存）}（仅首次，断点续传；尝试 $attempt/3）"
    "$INF_VENV/bin/hf" download "$1" ${extra_args[@]+"${extra_args[@]}"} \
      && touch "$sent" && return 0
    sleep 3
  done
  fail "下载失败：$1（网络不畅可调整 HF_ENDPOINT 后重跑）"
  exit 1
}

# 权重校验：hf 在仓库不可达时会"成功"返回本地目录，必须确认权重真的落盘
# （ls 任一 glob 不命中即非零退出，纯 safetensors 目录会被误判，故用 compgen 逐个测）
require_weights() {
  compgen -G "$1/*.safetensors" >/dev/null || compgen -G "$1/*.bin" >/dev/null \
    || { fail "权重文件缺失：$1（删除该目录的 .download-ok 后重跑 start）"; exit 1; }
}

do_start() {
  step "前置检查"
  if want "$WANT_LLM"; then
    [ -x "$VLLM_VENV/bin/vllm" ] || { fail "缺少 .venv-vllm（见文件头安装命令）"; exit 1; }
  fi
  if want_infinity; then
    [ -x "$INF_VENV/bin/infinity_emb" ] || { fail "缺少 .venv-infinity（见文件头安装命令）"; exit 1; }
  fi
  want "$WANT_LLM" || want_infinity || { fail "四个通道均未启用（MODELS_LOAD_*），无事可做"; exit 1; }
  command -v nc >/dev/null 2>&1 || { fail "缺少 nc（端口探测用）"; exit 1; }
  ok "venv 就绪"

  step "模型权重（HuggingFace，断点续传；首次需联网）"
  if want "$WANT_LLM"; then download "$LLM_ID" "${LLM_ID##*/}"; fi
  if want "$WANT_EMBEDDING"; then download "BAAI/bge-m3" "bge-m3" "--exclude onnx/*"; fi
  if want "$WANT_RERANK"; then download "BAAI/bge-reranker-v2-m3" "bge-reranker-v2-m3"; fi
  if want "$WANT_CLIP"; then
    # jina-clip-v2 的文本塔权重在 jina-embeddings-v3，远程代码在两个 *-implementation
    # 仓——后三者为嵌套依赖，须进 HF 缓存（~/.cache/huggingface）供离线加载
    download "jinaai/jina-clip-v2" "jina-clip-v2" "--exclude onnx/* --exclude pytorch_model.bin"
    download "jinaai/jina-embeddings-v3" "" "--exclude onnx/* --exclude pytorch_model.bin"
    download "jinaai/jina-clip-implementation" ""
    download "jinaai/xlm-roberta-flash-implementation" ""
  fi
  if want "$WANT_LLM"; then require_weights "$MODELS/${LLM_ID##*/}"; fi
  if want "$WANT_EMBEDDING"; then require_weights "$MODELS/bge-m3"; fi
  if want "$WANT_RERANK"; then require_weights "$MODELS/bge-reranker-v2-m3"; fi
  if want "$WANT_CLIP"; then require_weights "$MODELS/jina-clip-v2"; fi

  if want "$WANT_LLM"; then
    step "启动 vLLM（:${LLM_PORT}）"
    if port_up "$LLM_PORT"; then
      ok "vLLM 已在运行（:${LLM_PORT}），跳过"
    else
      # deepseek-chat 别名：agent/dsh 经 ai.llm.base_url 注入 DEEPSEEK_BASE_URL，模型名固定
      # deepseek-chat——别名让它免改配置直连本地 vLLM。
      # VLLM_USE_FLASHINFER_SAMPLER=0：无系统 nvcc 时 flashinfer 采样器 JIT 编译会失败
      #（本机未装 CUDA 工具链；torch 采样器够用）；kernel-config 同理关闭 autotune
      VLLM_USE_FLASHINFER_SAMPLER=0 PYTHONUNBUFFERED=1 nohup "$VLLM_VENV/bin/vllm" serve "$LLM_DIR" \
        --served-model-name "$LLM_ALIAS" deepseek-chat \
        --enable-auto-tool-choice --tool-call-parser hermes \
        --host 127.0.0.1 --port "$LLM_PORT" \
        --gpu-memory-utilization "$VLLM_GPU_UTIL" \
        --max-model-len "$VLLM_MAX_LEN" \
        --kernel-config '{"enable_flashinfer_autotune": false}' \
        >"$LOG_DIR/dev-vllm.log" 2>&1 &
      save_pid vllm $!
      ok "已启动（pid $(sed -n 's/^vllm=//p' "$PID_FILE" | head -1)），日志 tmp/dev-vllm.log"
    fi
  fi

  if want_infinity; then
    step "启动 Infinity（:${INF_PORT}）"
    if port_up "$INF_PORT"; then
      ok "Infinity 已在运行（:${INF_PORT}），跳过"
    else
      # 按通道拼 --model-id 对（只加载所选模型，省显存）
      local inf_args=()
      if want "$WANT_EMBEDDING"; then inf_args+=(--model-id "$EMB_DIR" --served-model-name bge-m3); fi
      if want "$WANT_RERANK"; then inf_args+=(--model-id "$RERANK_DIR" --served-model-name bge-reranker-v2-m3); fi
      if want "$WANT_CLIP"; then inf_args+=(--model-id "$CLIP_DIR" --served-model-name jina-clip-v2); fi
      # INFINITY_BETTERTRANSFORMER=false：optimum 未装时 infinity 0.0.77 的守卫有缺陷
      # （NameError: BetterTransformerManager）；HF_HUB_OFFLINE=1：权重全本地，
      # 且 jina 远程代码的 etag 校验直连 HF 会超时（嵌套依赖须已进 HF 缓存）
      HF_HUB_OFFLINE=1 INFINITY_BETTERTRANSFORMER=false \
        PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True PYTHONUNBUFFERED=1 \
        nohup "$INF_VENV/bin/infinity_emb" v2 "${inf_args[@]}" \
        --engine torch --device cuda --dtype float16 \
        --host 127.0.0.1 --port "$INF_PORT" \
        >"$LOG_DIR/dev-infinity.log" 2>&1 &
      save_pid infinity $!
      ok "已启动（pid $(sed -n 's/^infinity=//p' "$PID_FILE" | head -1)），日志 tmp/dev-infinity.log"
    fi
  fi

  step "健康等待"
  if want "$WANT_LLM"; then wait_http "vLLM" "http://127.0.0.1:${LLM_PORT}/health" 600 || exit 1; fi
  if want_infinity; then wait_http "Infinity" "http://127.0.0.1:${INF_PORT}/health" 300 || exit 1; fi
  local channels=""
  want "$WANT_LLM" && channels="LLM :${LLM_PORT}"
  want_infinity && channels="${channels:+$channels / }嵌入·重排·CLIP（按需子集）:${INF_PORT}"
  ok "本地模型服务就绪：$channels"
  echo "接线：把 config/loomvec.local-models.example.json 的 ai 段合并进 config/loomvec.json 后重启应用"
}

do_stop() {
  local name pid
  for name in vllm infinity; do
    if alive "$name"; then
      pid="$(sed -n "s/^$name=//p" "$PID_FILE" | head -1)"
      kill "$pid" 2>/dev/null
      local i=0
      while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 15 ]; do sleep 2; i=$((i + 2)); done
      kill -9 "$pid" 2>/dev/null || true
      ok "已停止 $name (pid $pid)"
    else
      echo "- $name 未在运行"
    fi
  done
  rm -f "$PID_FILE"
}

do_status() {
  if want "$WANT_LLM"; then
    if port_up "$LLM_PORT" && curl -sf -o /dev/null "http://127.0.0.1:${LLM_PORT}/health"; then
      ok "vLLM :$LLM_PORT 健康"
    else
      fail "vLLM :$LLM_PORT 不可用（./scripts/start-models-gpu.sh start）"
    fi
  fi
  if want_infinity; then
    if port_up "$INF_PORT" && curl -sf -o /dev/null "http://127.0.0.1:${INF_PORT}/health"; then
      ok "Infinity :$INF_PORT 健康"
    else
      fail "Infinity :$INF_PORT 不可用（./scripts/start-models-gpu.sh start）"
    fi
  fi
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader
}

do_logs() { # $1=vllm|infinity|all
  local pick="${1:-all}" files=()
  case "$pick" in
    vllm)     files=("$LOG_DIR/dev-vllm.log") ;;
    infinity) files=("$LOG_DIR/dev-infinity.log") ;;
    all)      files=("$LOG_DIR/dev-vllm.log" "$LOG_DIR/dev-infinity.log") ;;
    *) fail "未知日志名：$pick（可选 vllm/infinity/all）"; exit 1 ;;
  esac
  local f found=0
  for f in "${files[@]}"; do [ -f "$f" ] && found=1; done
  [ "$found" = "1" ] || { warn "暂无日志文件，先运行 start"; exit 0; }
  echo "${DIM}跟踪：${files[*]}（Ctrl-C 退出跟踪，服务继续运行）${RST}"
  trap 'echo; ok "已退出日志跟踪"; exit 0' INT
  tail -n 40 -F "${files[@]}"
}

usage() { echo "用法: ./scripts/start-models-gpu.sh start | stop | status | logs [vllm|infinity|all]"; }

case "${1:-}" in
  start)  do_start ;;
  stop)   do_stop; exit 0 ;;
  status) do_status; exit 0 ;;
  logs)   do_logs "${2:-all}"; exit 0 ;;
  *)      usage; exit 1 ;;
esac
