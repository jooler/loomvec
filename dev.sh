#!/usr/bin/env bash
# LoomVec 开发环境管理脚本（macOS/Linux）
#
# 用法（子命令，必须显式指定）：
#   ./dev.sh start             # 一键启动全部：部署门禁 → 镜像检查（缺失自动拉取/构建）→ 基础设施+监控栈
#                              #   → 迁移 → api/agent/worker + 三个前端（web/admin/ops）；交互终端下
#                              #   完成后实时跟随 FastAPI 日志（Ctrl-C 退出跟踪，服务继续运行；--no-follow 关闭）
#   ./dev.sh logs [名称]       # 跟踪服务日志：api（默认）/agent/worker/web/admin/ops/mineru/models/all
#   ./dev.sh stop              # 关闭应用进程（api/agent/worker/三个前端/本地 MinerU），并停止基础设施+监控容器（数据卷保留）
#   ./dev.sh status            # 查看各组件运行状态
#
# 新环境首次部署：先运行 ./deploy.sh（交互式配置 AI 供方 + 安装全部依赖，写入 config/loomvec.json），再 ./dev.sh start；
# 未部署过时 ./dev.sh start 也会在交互终端下自动先拉起 ./deploy.sh（见下方"部署门禁"）。
#
# 行为约定：
# - 幂等：已在运行的组件自动跳过，不会重复拉起；
# - 监控栈（Grafana/Prometheus/Alertmanager/Loki/Promtail）随一键启动一起拉起；
# - 日志：应用进程输出到 tmp/dev-{api,agent,worker,web,admin,ops,mineru}.log（MinerU 容器版时在 docker）；
#   应用进程以 PYTHONUNBUFFERED=1 运行，日志实时落盘可即时 tail；
# - 应用参数 config/loomvec.json（不入库）缺失时从模板自动生成（ai.mock=true，离线可跑）；
# - MinerU 双路线按配置路由（config/loomvec.json 的 mineru.device，./deploy.sh 交互写入）：
#   cpu（默认）= compose 容器版随 compose 拉起；gpu = 宿主机 GPU 版（scripts/start-mineru-gpu.sh），
#   start 时把容器版 mineru 移出 compose 服务集（tmp/compose-mineru-gpu.yaml，幂等生成不入库）；
#   compose.override.yaml 手工以 profile 隔离 mineru 的旧用法继续兼容（叠加时以并集为准）；
# - 本地模型服务（scripts/start-models-gpu.sh：vLLM/Infinity）按配置派生：ai.llm/embedding/
#   rerank/clip 的 base_url 指向本地推理端口（127.0.0.1:MODELS_LLM_PORT/MODELS_INFINITY_PORT，
#   缺省 38010/38011）的通道自动后台拉起（只加载所选通道）；由本脚本拉起的随 stop 一并停止；
# - MinerU / 本地模型首次启动需下载权重（1~2GB / 约 13GB），未就绪只告警不阻塞（相应功能暂不可用）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
LOG_DIR="$ROOT/tmp"; PID_FILE="$LOG_DIR/dev.pids"; mkdir -p "$LOG_DIR"
DEPLOY_STAMP="$LOG_DIR/loomvec-deployed.stamp"  # ./deploy.sh 成功完成时写入；缺失则 dev.sh 先拉起部署

# ---------------------------------------------------------------- 应用参数读取（启动按配置走）
# config/loomvec.json 缺失/非法时取默认：mineru.device=cpu（容器版）+ 无本地模型通道。
# 本地模型派生：ai.<通道>.base_url 指向本地推理端口即视为该通道走本地 GPU（dev.sh 自动拉起
# scripts/start-models-gpu.sh 并只加载所选通道）；改回云端地址即不再拉起——启动不再重复询问。
CFG_VALUES="$(python3 - "$ROOT/config/loomvec.json" <<'PY'
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    cfg = {}
mineru = cfg.get("mineru") or {}
ai = cfg.get("ai") or {}
print("MINERU_DEVICE\t" + str(mineru.get("device") or "cpu"))
for name, channel in (("LLM_URL", "llm"), ("EMB_URL", "embedding"), ("RR_URL", "rerank"), ("CLIP_URL", "clip")):
    v = (ai.get(channel) or {}).get("base_url")
    print(f"{name}\t{v if isinstance(v, str) else ''}")
PY
)"
MINERU_DEVICE="cpu"
LLM_BASE_URL=""; EMB_BASE_URL=""; RR_BASE_URL=""; CLIP_BASE_URL=""
while IFS=$'\t' read -r k v; do
  case "$k" in
    MINERU_DEVICE) MINERU_DEVICE="${v:-cpu}" ;;
    LLM_URL)  LLM_BASE_URL="$v" ;;
    EMB_URL)  EMB_BASE_URL="$v" ;;
    RR_URL)   RR_BASE_URL="$v" ;;
    CLIP_URL) CLIP_BASE_URL="$v" ;;
  esac
done <<< "$CFG_VALUES"

# 本地推理端口单源：与 scripts/start-models-gpu.sh 相同的 env 覆盖（不读 .env，避免两处漂移）
MODELS_LLM_PORT="${MODELS_LLM_PORT:-38010}"
MODELS_INFINITY_PORT="${MODELS_INFINITY_PORT:-38011}"
is_local_base() { # $1=base_url $2=port —— host 限 127.0.0.1/localhost 且端口匹配
  local url="${1%/}"
  case "$url" in
    "http://127.0.0.1:$2" | "http://127.0.0.1:$2"/* | "http://localhost:$2" | "http://localhost:$2"/*) return 0 ;;
  esac
  return 1
}
LOAD_LLM=0; LOAD_EMBEDDING=0; LOAD_RERANK=0; LOAD_CLIP=0
[ -n "$LLM_BASE_URL" ]  && is_local_base "$LLM_BASE_URL"  "$MODELS_LLM_PORT"     && LOAD_LLM=1
[ -n "$EMB_BASE_URL" ]  && is_local_base "$EMB_BASE_URL"  "$MODELS_INFINITY_PORT" && LOAD_EMBEDDING=1
[ -n "$RR_BASE_URL" ]   && is_local_base "$RR_BASE_URL"   "$MODELS_INFINITY_PORT" && LOAD_RERANK=1
[ -n "$CLIP_BASE_URL" ] && is_local_base "$CLIP_BASE_URL" "$MODELS_INFINITY_PORT" && LOAD_CLIP=1
USE_LOCAL_MODELS=0
[ "$LOAD_LLM" = 1 -o "$LOAD_EMBEDDING" = 1 -o "$LOAD_RERANK" = 1 -o "$LOAD_CLIP" = 1 ] && USE_LOCAL_MODELS=1

# compose.override.yaml 不入库（本机开发定制，如用 profile 关掉容器版 MinerU 改走宿主机 GPU）：
# 存在才并入；缺失时新环境按纯 compose.yaml 跑（MinerU 随容器启动）
COMPOSE_FILES=(-f deploy/compose/compose.yaml)
if [ -f deploy/compose/compose.override.yaml ]; then COMPOSE_FILES+=(-f deploy/compose/compose.override.yaml); fi
# mineru.device=gpu：把容器版 mineru 移出默认服务集（宿主机 GPU 脚本接管），幂等生成不入库
if [ "$MINERU_DEVICE" = "gpu" ]; then
  cat > "$LOG_DIR/compose-mineru-gpu.yaml" <<'YAML'
# dev.sh 依 config/loomvec.json 的 mineru.device=gpu 生成：容器版 mineru 移出默认服务集，
# 由宿主机 scripts/start-mineru-gpu.sh 接管（手动拉容器版：docker compose --profile cpu-mineru up -d mineru）
services:
  mineru:
    profiles: ["cpu-mineru"]
YAML
  COMPOSE_FILES+=(-f "$LOG_DIR/compose-mineru-gpu.yaml")
fi
COMPOSE="docker compose ${COMPOSE_FILES[*]} --profile observability"
# MinerU 路线判定：compose 解析出的服务集含 mineru（device=cpu 且未被 profile 隔离）→ 容器版；
# 否则（device=gpu 生成的 tmp override 或用户 override 把 mineru 移出默认集）→ 宿主机 GPU 脚本。
# 以 compose 实际解析结果为准，不硬编码 override 文件内容。
mineru_in_compose() { $COMPOSE config --services 2>/dev/null | grep -qx mineru; }
# MinerU 端口单源：deploy/compose/.env 的 MINERU_PORT（容器端口映射与本地脚本共用；默认 38000）
MINERU_PORT="$(grep -E '^MINERU_PORT=' deploy/compose/.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"')"
MINERU_PORT="${MINERU_PORT:-38000}"
export PYTHONUNBUFFERED=1  # uvicorn/celery 日志实时落盘，tail 即时可见
# 智能体 env 根目录：开发默认落仓库内 tmp/（配置默认 /data/... 在开发机通常不可写）；显式 export 可覆盖
export LOOMVEC_AGENT_STORAGE_ROOT="${LOOMVEC_AGENT_STORAGE_ROOT:-$ROOT/tmp/agent-envs}"

# 服务端口单源 .env（LOOMVEC_API_PORT/WEB/ADMIN/OPS_PORT；缺省 38080/35173/35174/35175）
load_env_var() {  # load_env_var KEY DEFAULT —— 从仓库根 .env 读取（存在时）
  local val
  val=$(grep -E "^$1=" "$ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"'"')
  echo "${val:-$2}"
}
API_PORT=$(load_env_var LOOMVEC_API_PORT 38080)
AGENT_PORT=$(load_env_var LOOMVEC_AGENT_PORT 38090)
WEB_PORT=$(load_env_var LOOMVEC_WEB_PORT 35173)
ADMIN_PORT=$(load_env_var LOOMVEC_ADMIN_PORT 35174)
OPS_PORT=$(load_env_var LOOMVEC_OPS_PORT 35175)
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
ok()   { echo "${GRN}✓${RST} $*"; }
warn() { echo "${YLW}!${RST} $*"; }
fail() { echo "${RED}✗${RST} $*"; }
step() { echo "\n${DIM}── $* ──${RST}"; }

port_up() { nc -z localhost "$1" >/dev/null 2>&1; }
alive()   { [ -f "$PID_FILE" ] && grep -q "^$1=" "$PID_FILE" && kill -0 "$(sed -n "s/^$1=//p" "$PID_FILE" | head -1)" 2>/dev/null; }
wait_http() { # $1=名称 $2=url $3=必填(1)/选填(0) $4=超时秒
  local i=0 total=$4
  while [ "$i" -lt "$total" ]; do
    curl -sf -o /dev/null "$2" && { ok "$1 就绪"; return 0; }
    i=$((i + 2)); sleep 2
  done
  if [ "$3" = "1" ]; then fail "$1 在 ${total}s 内未就绪（${2}）"; exit 1
  else warn "$1 未就绪（继续；首次启动需下载模型，稍后自动恢复）"; fi
}
save_pid(){
  if [ -f "$PID_FILE" ]; then
    sed -i '' "/^$1=/d" "$PID_FILE" 2>/dev/null || sed -i "/^$1=/d" "$PID_FILE"
  fi
  echo "$1=$2" >> "$PID_FILE"
}
start_bg() { # $1=名称 $2=pid名 $3=端口 $4=命令...（端口已监听/进程存活则跳过；日志 dev-{名称}.log）
  local name="$1" pid_name="$2" port="$3"; shift 3
  if port_up "$port"; then ok "$name 已在运行（:${port}），跳过"; return 0; fi
  if alive "$pid_name"; then ok "$name 进程存活但端口未监听，等待中…"; return 0; fi
  echo "启动 $name …"
  nohup "$@" >"$LOG_DIR/dev-$name.log" 2>&1 &
  save_pid "$pid_name" $!
}

do_stop() {
  if [ -f "$PID_FILE" ]; then
    while IFS= read -r line; do
      pid_name="${line%%=*}"; pid="${line#*=}"
      if kill -0 "$pid" 2>/dev/null; then kill "$pid" && ok "已停止 $pid_name (pid $pid)"; else echo "- $pid_name (pid $pid) 未在运行"; fi
    done < "$PID_FILE"
    rm -f "$PID_FILE"
  else
    echo "- 无本脚本启动的应用进程（${PID_FILE} 不存在）"
  fi
  # 本地模型服务：仅停由本脚本拉起的（标记文件）；外部手动 start 的不动（同 MinerU GPU 语义）
  if [ -f "$LOG_DIR/dev-models.started" ]; then
    step "停止本地模型服务（scripts/start-models-gpu.sh）"
    bash scripts/start-models-gpu.sh stop || warn "模型服务停止失败"
    rm -f "$LOG_DIR/dev-models.started"
  fi
  step "停止基础设施 + 监控容器"
  if docker info --format ok >/dev/null 2>&1; then
    $COMPOSE stop || warn "compose stop 失败"
    ok "基础设施与监控容器已停止（数据卷保留；彻底清理用 $COMPOSE down）"
  else
    warn "Docker 未运行，跳过容器停止"
  fi
}

do_status() {
  for probe in "PostgreSQL:35433" "Redis:36379" "RustFS:39000" "Milvus:39530" "MinerU:$MINERU_PORT" "Grafana:33002" "Prometheus:39090" "API:$API_PORT" "Agent:$AGENT_PORT" "Web:$WEB_PORT" "Admin:$ADMIN_PORT" "Ops:$OPS_PORT"; do
    port_up "${probe##*:}" && ok "$probe" || fail "$probe"
  done
  if alive worker; then ok "worker（本脚本启动）"
  elif docker exec loomvec-redis redis-cli exists loomvec:worker:heartbeat 2>/dev/null | grep -q 1; then ok "worker（心跳在，非本脚本进程）"
  else fail "worker"; fi
  # 本地模型服务：启动过（models.pids 在）才探测，纯云端环境不显示噪音
  if [ -f "$LOG_DIR/models.pids" ]; then
    bash scripts/start-models-gpu.sh status
  fi
}

usage() { echo "用法: ./dev.sh start [--no-follow] | stop | status | logs [api|agent|worker|web|admin|ops|mineru|models|all]"; }

do_logs() { # $1=api|agent|worker|web|admin|ops|mineru|models|all —— tail -F 跟踪，Ctrl-C 退出不影响服务
  local pick="$1"
  local files=()
  case "$pick" in
    api|agent|worker|web|admin|ops) files=("$LOG_DIR/dev-$pick.log") ;;
    mineru)
      if [ -f "$LOG_DIR/dev-mineru.log" ]; then files=("$LOG_DIR/dev-mineru.log")
      elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx loomvec-mineru; then
        echo "MinerU 为容器版，日志在容器内：docker logs -f loomvec-mineru"; exit 0
      else fail "暂无本地 MinerU 日志（dev-mineru.log 不存在；容器版用 docker logs loomvec-mineru）"; exit 1; fi ;;
    models)
      files=("$LOG_DIR/dev-models.log" "$LOG_DIR/dev-vllm.log" "$LOG_DIR/dev-infinity.log") ;;
    all)
      files=("$LOG_DIR"/dev-api.log "$LOG_DIR"/dev-agent.log "$LOG_DIR"/dev-worker.log "$LOG_DIR"/dev-web.log "$LOG_DIR"/dev-admin.log "$LOG_DIR"/dev-ops.log)
      [ -f "$LOG_DIR/dev-mineru.log" ] && files+=("$LOG_DIR/dev-mineru.log")
      for f in dev-models.log dev-vllm.log dev-infinity.log; do [ -f "$LOG_DIR/$f" ] && files+=("$LOG_DIR/$f"); done ;;
    *) fail "未知日志名：${pick}（可选 api/agent/worker/web/admin/ops/mineru/models/all）"; usage; exit 1 ;;
  esac
  local f found=0
  for f in "${files[@]}"; do [ -f "$f" ] && found=1; done
  [ "$found" = "1" ] || { warn "暂无日志文件，先运行 ./dev.sh start"; exit 0; }
  echo "${DIM}跟踪：${files[*]}（Ctrl-C 退出跟踪，服务继续运行）${RST}"
  trap 'echo; ok "已退出日志跟踪（服务仍在运行；./dev.sh stop 停止全部）"; exit 0' INT
  tail -n 40 -F "${files[@]}"
}

NO_FOLLOW=0
case "${1:-}" in
  start)
    shift
    for arg in "$@"; do
      case "$arg" in
        --no-follow) NO_FOLLOW=1 ;;
        *) fail "未知参数：$arg"; usage; exit 1 ;;
      esac
    done ;;
  stop)   do_stop; exit 0 ;;
  status) do_status; exit 0 ;;
  logs)   do_logs "${2:-api}"; exit 0 ;;
  *)      usage; exit 1 ;;
esac

# ---------------------------------------------------------------- 前置检查
step "前置检查"
for cmd in docker uv pnpm; do
  command -v "$cmd" >/dev/null 2>&1 || { fail "缺少 ${cmd}（Python 用 uv 管理，前端用 pnpm）"; exit 1; }
done
ok "docker / uv / pnpm 就绪"

# ---------------------------------------------------------------- 部署门禁（deploy-first）
# 新环境未部署过（无 tmp/loomvec-deployed.stamp）：交互终端下自动先执行 ./deploy.sh
# （AI 供方录入 / 端口迁移 / 应用参数初始化），完成后继续本脚本；取消或部署失败则终止
# 启动。非交互环境（CI / 脚本管道）跳过自动部署，走下方兜底初始化并告警。
if [ ! -f "$DEPLOY_STAMP" ]; then
  if [ -t 0 ]; then
    step "检测到尚未部署，先执行 ./deploy.sh（完成后自动继续启动）"
    LOOMVEC_DEPLOY_INVOKED_BY_DEV=1 ./deploy.sh \
      || { fail "部署未完成（已取消或出错）；完成后重新运行 ./dev.sh start"; exit 1; }
  else
    warn "尚未运行 ./deploy.sh（非交互环境，跳过自动部署；将按离线 mock 兜底初始化）"
  fi
fi

docker info --format ok >/dev/null 2>&1 || { fail "Docker 未运行——请先启动 Docker Desktop"; exit 1; }
ok "Docker daemon 运行中"

# ---------------------------------------------------------------- 环境文件
[ -f deploy/compose/.env ] || { cp deploy/compose/.env.example deploy/compose/.env; ok "生成 deploy/compose/.env"; }
[ -f .env ] || { cp .env.example .env; ok "生成根 .env（基础设施连接；AI 供方在 config/loomvec.json）"; }

# ---------------------------------------------------------------- 应用参数文件
# config/loomvec.json 不入库（含密钥），AI 供方等应用参数只认它（env 不生效）：
# 缺失时从模板生成并置 ai.mock=true（离线可跑通全链路；接真实供方见 README「AI 供方」）
if [ ! -f config/loomvec.json ]; then
  step "应用参数文件（config/loomvec.json）"
  [ -f config/loomvec.example.json ] || { fail "缺少模板 config/loomvec.example.json"; exit 1; }
  cp config/loomvec.example.json config/loomvec.json
  sed -i '' 's/"mock": false/"mock": true/' config/loomvec.json 2>/dev/null \
    || sed -i 's/"mock": false/"mock": true/' config/loomvec.json
  ok "生成 config/loomvec.json（ai.mock=true，离线开发；接真实 AI 供方见 README）"
elif grep -q '"mock": false' config/loomvec.json && grep -q 'sk-xxx' config/loomvec.json; then
  warn "config/loomvec.json 仍是模板占位密钥（sk-xxx）且 ai.mock=false——上传管线 embed 步骤会失败；离线开发请置 ai.mock=true，或填入真实供方密钥"
fi

# ---------------------------------------------------------------- 镜像检查
# 本地构建镜像（无仓库源）缺失则构建；仓库镜像缺失由 compose pull 拉取
step "镜像检查（缺失自动构建/拉取）"
if ! docker image inspect loomvec/postgres-age:18 >/dev/null 2>&1; then
  warn "postgres-age 镜像缺失，准备构建（需 AGE 源码，约数分钟）"
  [ -d deploy/compose/postgres-age/age-src ] || make age-src
  $COMPOSE build postgres || { fail "postgres-age 镜像构建失败"; exit 1; }
fi
if mineru_in_compose && ! docker image inspect loomvec/mineru:3.4.5-cpu >/dev/null 2>&1; then
  warn "mineru 镜像缺失，准备构建（体积较大，耐心等待）"
  $COMPOSE build mineru || { fail "mineru 镜像构建失败"; exit 1; }
fi
$COMPOSE pull --quiet --ignore-buildable || warn "部分镜像拉取失败（缺失镜像会在启动阶段重试）"

# ---------------------------------------------------------------- 基础设施栈 + 监控栈
step "基础设施栈 + 监控栈（docker compose）"
$COMPOSE up -d || { fail "compose 启动失败"; exit 1; }

step "基础设施健康等待"
docker exec loomvec-postgres pg_isready -U loomvec >/dev/null 2>&1 || { fail "PostgreSQL 未就绪"; exit 1; }
ok "PostgreSQL:35433 就绪"
i=0; while [ "$i" -lt 15 ]; do
  docker exec loomvec-redis redis-cli ping 2>/dev/null | grep -q PONG && break
  i=$((i + 1)); sleep 2
done
[ "$i" -lt 15 ] && ok "Redis 就绪" || { fail "Redis 未就绪"; exit 1; }
wait_http "RustFS"            "http://localhost:39000/health"     1 60
wait_http "Milvus"            "http://localhost:39091/healthz"    1 120

# MinerU 双路线：容器版已随上方 compose up 拉起；本地 GPU 版在此接管拉起
# （未安装只告警不阻塞，与「未就绪只告警」约定一致——解析功能暂不可用）
if mineru_in_compose; then
  ok "MinerU 随 compose 启动（容器版 loomvec-mineru）"
  wait_http "MinerU ($MINERU_PORT)" "http://localhost:$MINERU_PORT/health" 0 20
else
  step "MinerU（宿主机 GPU：scripts/start-mineru-gpu.sh）"
  if [ -x .venv-mineru/bin/mineru-api ]; then
    start_bg mineru mineru "$MINERU_PORT" env MINERU_PORT="$MINERU_PORT" bash scripts/start-mineru-gpu.sh
  else
    warn "本地 MinerU 未安装（.venv-mineru 缺失）：解析功能暂不可用；安装：uv pip install --python .venv-mineru -e './third_party/mineru[pipeline]' six"
  fi
  wait_http "MinerU ($MINERU_PORT)" "http://localhost:$MINERU_PORT/health" 0 60
fi

# 本地模型服务（vLLM/Infinity）：按 ai.*.base_url 派生的通道后台拉起（幂等，已在运行/下载中
# 自动跳过）。首次需下载权重（约 13GB）+ 加载，不阻塞启动——就绪前指向本地端点的 ai 通道
# 调用会失败并按既有重试/降级路径处理；就绪探测只等 30s，未就绪转后台继续。
if [ "$USE_LOCAL_MODELS" = 1 ]; then
  step "本地模型服务（LLM=$LOAD_LLM 嵌入=$LOAD_EMBEDDING 重排=$LOAD_RERANK CLIP=$LOAD_CLIP）"
  if [ -x scripts/start-models-gpu.sh ]; then
    if alive models_boot; then
      ok "本地模型 bootstrap 进行中（pid $(sed -n 's/^models_boot=//p' "$PID_FILE" | head -1)），跳过"
    else
      nohup env MODELS_LOAD_LLM="$LOAD_LLM" MODELS_LOAD_EMBEDDING="$LOAD_EMBEDDING" \
        MODELS_LOAD_RERANK="$LOAD_RERANK" MODELS_LOAD_CLIP="$LOAD_CLIP" \
        bash scripts/start-models-gpu.sh start >"$LOG_DIR/dev-models.log" 2>&1 &
      save_pid models_boot $!
      touch "$LOG_DIR/dev-models.started"  # stop 标记：只停由本脚本拉起的模型服务
      ok "已后台拉起（日志 tmp/dev-models.log；./scripts/start-models-gpu.sh status 看就绪）"
    fi
    if [ "$LOAD_LLM" = 1 ]; then
      wait_http "vLLM (:${MODELS_LLM_PORT})" "http://127.0.0.1:${MODELS_LLM_PORT}/health" 0 30
    fi
    if [ "$LOAD_EMBEDDING" = 1 ] || [ "$LOAD_RERANK" = 1 ] || [ "$LOAD_CLIP" = 1 ]; then
      wait_http "Infinity (:${MODELS_INFINITY_PORT})" "http://127.0.0.1:${MODELS_INFINITY_PORT}/health" 0 30
    fi
  else
    warn "scripts/start-models-gpu.sh 缺失：指向本地端点的 ai 通道暂不可用"
  fi
fi

wait_http "Grafana (33002)"    "http://localhost:33002/api/health" 1 120
wait_http "Prometheus (39090)" "http://localhost:39090/-/ready"    1 120

# ---------------------------------------------------------------- 数据库初始化
step "数据库初始化（幂等：建最新结构 + 种子，P5 起取代 alembic）"
if make init-db >/tmp/loomvec-initdb.log 2>&1; then ok "结构到位（init_db）"
else fail "初始化失败：$(tail -3 /tmp/loomvec-initdb.log)"; exit 1; fi

# ---------------------------------------------------------------- 依赖检查
[ -d .venv ] || { step "安装后端依赖（uv sync）"; uv sync; }
[ -d node_modules ] || { step "安装前端依赖（pnpm install）"; pnpm install --frozen-lockfile; }

# ---------------------------------------------------------------- 应用进程
# P5.5a：config 里 agent.runtime.provider=docker 时，沙箱网络/镜像预检（缺失则 fail-closed，
# 避免首问时 spawn 报 sandbox_unavailable）；provider=local 零影响
check_sandbox_prereqs() {
  local provider image subnet
  provider="$(python3 -c "
import json
try:
    d = json.load(open('config/loomvec.json', encoding='utf-8'))
    print(((d.get('agent') or {}).get('runtime') or {}).get('provider') or 'local')
except Exception:
    print('local')
")"
  [ "$provider" = "docker" ] || return 0
  command -v docker >/dev/null 2>&1 || { fail "provider=docker 但本机无 docker CLI"; exit 1; }
  subnet="$(grep -E '^AGENT_SANDBOX_SUBNET=' deploy/compose/.env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
  subnet="${subnet:-172.31.77.0/24}"
  docker network inspect agent-sandbox >/dev/null 2>&1 || \
    docker network create --driver bridge --subnet "$subnet" agent-sandbox >/dev/null \
    || { fail "agent-sandbox 网络创建失败（子网 ${subnet} 冲突？可调整 deploy/compose/.env）"; exit 1; }
  image="$(python3 -c "
import json
try:
    d = json.load(open('config/loomvec.json', encoding='utf-8'))
    print((((d.get('agent') or {}).get('runtime') or {}).get('sandbox') or {}).get('image') or 'loomvec/agent-sandbox:stable')
except Exception:
    print('loomvec/agent-sandbox:stable')
")"
  docker image inspect "$image" >/dev/null 2>&1 \
    || { fail "沙箱镜像 $image 不存在：请先运行 ./deploy.sh 构建沙箱镜像"; exit 1; }
  ok "沙箱前置就绪（agent-sandbox 网络 + 镜像 ${image}）"
}

step "应用进程"
check_sandbox_prereqs
start_bg api    api    "$API_PORT"    uv run python -m loomvec.api --reload  # 端口单源 LOOMVEC_API_PORT
start_bg agent  agent  "$AGENT_PORT"  uv run python -m loomvec.agent --reload  # P5 智能体网关（内网 only）
external_worker=$(pgrep -f "loomvec.worker.celery_app" | head -1)
if [ -n "$external_worker" ]; then
  if alive worker; then ok "worker 运行中（本脚本）"
  else warn "检测到外部 worker 进程（pid ${external_worker}），不重复启动；如需由本脚本接管请先手动停止"; fi
else
  start_bg worker worker 0 uv run celery -A loomvec.worker.celery_app:celery_app worker -l info -B -Q pipeline,pipeline_high,pipeline_low
fi
start_bg web    web    "$WEB_PORT"    pnpm dev:web
start_bg admin  admin  "$ADMIN_PORT"  pnpm dev:admin
start_bg ops    ops    "$OPS_PORT"    pnpm dev:ops

# worker 无端口，用进程+心跳判定
if alive worker || docker exec loomvec-redis redis-cli exists loomvec:worker:heartbeat 2>/dev/null | grep -q 1; then
  ok "worker 运行中（celery + beat）"
else
  warn "worker 心跳未就绪（beat 每 30s 写一次，稍后自行恢复）"
fi

step "就绪等待"
wait_http "API ($API_PORT)"  "http://localhost:$API_PORT/readyz" 1 120
wait_http "Agent ($AGENT_PORT)" "http://localhost:$AGENT_PORT/internal/agent/health" 1 120
wait_http "Web ($WEB_PORT)"  "http://localhost:$WEB_PORT/"  1 60
wait_http "Admin ($ADMIN_PORT)" "http://localhost:$ADMIN_PORT/" 1 60
wait_http "Ops ($OPS_PORT)"  "http://localhost:$OPS_PORT/"   1 60

step "完成"
echo "  用户端     http://localhost:$WEB_PORT    （dev 登录：任意用户名）"
echo "  运维端     http://localhost:$ADMIN_PORT  （dev 登录默认 super_admin）"
echo "  运营端     http://localhost:$OPS_PORT    （dev 登录默认 operator）"
echo "  API 文档   http://localhost:$API_PORT/docs"
echo "  Grafana    http://localhost:33002 （admin，密码见 deploy/compose/.env 的 GRAFANA_ADMIN_PASSWORD，默认 admin）"
echo "  Prometheus http://localhost:39090"
echo "  日志       tmp/dev-{api,agent,worker,web,admin,ops,mineru}.log（MinerU 容器版时在 docker；本地模型 tmp/dev-{models,vllm,infinity}.log）"
if [ "$USE_LOCAL_MODELS" = 1 ]; then
  echo "  本地模型   ./scripts/start-models-gpu.sh status（vLLM :$MODELS_LLM_PORT / Infinity :$MODELS_INFINITY_PORT，按配置只载所选通道）"
fi
echo "  停止全部   ./dev.sh stop（应用进程 + 基础设施/监控容器 + 本脚本拉起的本地模型服务；数据卷保留）"

# ---------------------------------------------------------------- 实时日志（交互默认跟随 FastAPI 输出）
# start 就绪后原地 tail -F API 日志，开发时直接观察 uvicorn 请求/重载输出；
# Ctrl-C 只退出跟踪，服务继续运行。脚本化/CI 用 --no-follow（或非交互终端自动跳过）。
if [ "$NO_FOLLOW" = "1" ] || [ ! -t 0 ]; then
  echo "  实时日志   ./dev.sh logs api   （其余：agent/worker/web/admin/ops/mineru/models/all）"
else
  step "实时日志（FastAPI 开发输出；Ctrl-C 退出跟踪，服务继续运行）"
  do_logs api
fi
