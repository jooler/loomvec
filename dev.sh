#!/usr/bin/env bash
# LoomVec 开发环境一键启动（macOS/Linux）
#
# 用法：
#   ./dev.sh            # 检查并启动全部：基础设施栈 → 迁移 → api/worker/web/admin
#   ./dev.sh obs        # 启动可选监控栈（Grafana/Prometheus/Alertmanager/Loki/Promtail）
#   ./dev.sh obs stop   # 停止监控栈
#   ./dev.sh stop       # 停止本脚本启动的四个应用进程（基础设施保留）
#   ./dev.sh status     # 查看各组件运行状态
#
# 行为约定：
# - 幂等：已在运行的组件自动跳过，不会重复拉起；
# - 日志：应用进程输出到 tmp/dev-{api,worker,web,admin}.log；
# - MinerU 首次构建/启动较慢（模型下载 1~2GB），未就绪只告警不阻塞（解析功能暂不可用）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
LOG_DIR="$ROOT/tmp"; PID_FILE="$LOG_DIR/dev.pids"; mkdir -p "$LOG_DIR"

API_PORT=8080; WEB_PORT=5173; ADMIN_PORT=5174
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

do_stop() {
  if [ ! -f "$PID_FILE" ]; then echo "无运行记录（${PID_FILE} 不存在）"; exit 0; fi
  while IFS= read -r line; do
    pid_name="${line%%=*}"; pid="${line#*=}"
    if kill -0 "$pid" 2>/dev/null; then kill "$pid" && ok "已停止 $pid_name (pid $pid)"; else echo "- $pid_name (pid $pid) 未在运行"; fi
  done < "$PID_FILE"
  rm -f "$PID_FILE"
  echo "基础设施容器保留（docker compose -f deploy/compose/compose.yaml stop 可停）"
}

do_status() {
  for probe in "PostgreSQL:5433" "Redis:6379" "RustFS:9000" "Milvus:19530" "MinerU:8000" "API:8080" "Web:5173" "Admin:5174"; do
    port_up "${probe##*:}" && ok "$probe" || fail "$probe"
  done
  for probe in "Grafana:3002" "Prometheus:9090"; do
    port_up "${probe##*:}" && ok "$probe" || warn "$probe 未运行（可选监控栈：./dev.sh obs）"
  done
  if alive worker; then ok "worker（本脚本启动）"
  elif docker exec loomvec-redis redis-cli exists loomvec:worker:heartbeat 2>/dev/null | grep -q 1; then ok "worker（心跳在，非本脚本进程）"
  else fail "worker"; fi
}

do_obs() { # $1=up(默认)/stop；监控栈为可选 profile，默认不随一键启动
  COMPOSE="docker compose -f deploy/compose/compose.yaml --profile observability"
  if [ "$1" = "stop" ]; then
    $COMPOSE stop || { fail "监控栈停止失败"; exit 1; }
    ok "监控栈已停止（容器保留，可 ./dev.sh obs 再拉起）"
    return 0
  fi
  step "监控栈（observability profile）"
  $COMPOSE up -d || { fail "监控栈启动失败"; exit 1; }
  wait_http "Grafana (3002)"    "http://localhost:3002/api/health" 1 120
  wait_http "Prometheus (9090)" "http://localhost:9090/-/ready"    1 120
  ok "Grafana    http://localhost:3002 （admin / GRAFANA_ADMIN_PASSWORD，默认 admin）"
  ok "Prometheus http://localhost:9090"
  echo "  停止监控栈：./dev.sh obs stop"
}

case "${1:-start}" in
  stop)   do_stop; exit 0 ;;
  status) do_status; exit 0 ;;
  obs)    do_obs "${2:-up}"; exit 0 ;;
  start|"") : ;;
  *) echo "用法: ./dev.sh [start|stop|status|obs]"; exit 1 ;;
esac

# ---------------------------------------------------------------- 前置检查
step "前置检查"
for cmd in docker uv pnpm; do
  command -v "$cmd" >/dev/null 2>&1 || { fail "缺少 $cmd（Python 用 uv 管理，前端用 pnpm）"; exit 1; }
done
ok "docker / uv / pnpm 就绪"
docker info --format ok >/dev/null 2>&1 || { fail "Docker 未运行——请先启动 Docker Desktop"; exit 1; }
ok "Docker daemon 运行中"

# ---------------------------------------------------------------- 环境文件
[ -f deploy/compose/.env ] || { cp deploy/compose/.env.example deploy/compose/.env; ok "生成 deploy/compose/.env"; }
[ -f .env ] || { cp .env.example .env; ok "生成根 .env（默认 mock AI 供方，可离线开发）"; }

# ---------------------------------------------------------------- 基础设施栈
step "基础设施栈（docker compose）"
if ! docker image inspect loomvec/postgres-age:18 >/dev/null 2>&1; then
  warn "postgres-age 镜像缺失，准备构建（需 AGE 源码，约数分钟）"
  [ -d deploy/compose/postgres-age/age-src ] || make age-src
  docker compose -f deploy/compose/compose.yaml build postgres || { fail "postgres-age 镜像构建失败"; exit 1; }
fi
if ! docker image inspect loomvec/mineru:3.4.5-cpu >/dev/null 2>&1; then
  warn "mineru 镜像缺失，准备构建（体积较大，耐心等待）"
  docker compose -f deploy/compose/compose.yaml build mineru || { fail "mineru 镜像构建失败"; exit 1; }
fi
docker compose -f deploy/compose/compose.yaml up -d || { fail "compose 启动失败"; exit 1; }

step "基础设施健康等待"
docker exec loomvec-postgres pg_isready -U loomvec >/dev/null 2>&1 || { fail "PostgreSQL 未就绪"; exit 1; }
ok "PostgreSQL:5433 就绪"
i=0; while [ "$i" -lt 15 ]; do
  docker exec loomvec-redis redis-cli ping 2>/dev/null | grep -q PONG && break
  i=$((i + 1)); sleep 2
done
[ "$i" -lt 15 ] && ok "Redis 就绪" || { fail "Redis 未就绪"; exit 1; }
wait_http "RustFS"     "http://localhost:9000/health"   1 60
wait_http "Milvus"     "http://localhost:9091/healthz"  1 120
wait_http "MinerU"     "http://localhost:8000/health"   0 20

# ---------------------------------------------------------------- 数据迁移
step "数据库迁移（alembic upgrade head）"
if make migrate >/tmp/loomvec-migrate.log 2>&1; then ok "迁移到位（head）"
else fail "迁移失败：$(tail -3 /tmp/loomvec-migrate.log)"; exit 1; fi

# ---------------------------------------------------------------- 依赖检查
[ -d .venv ] || { step "安装后端依赖（uv sync）"; uv sync; }
[ -d node_modules ] || { step "安装前端依赖（pnpm install）"; pnpm install --frozen-lockfile; }

# ---------------------------------------------------------------- 应用进程
start_bg() { # $1=名称 $2=pid名 $3=端口 $4=命令...
  local name="$1" pid_name="$2" port="$3"; shift 3
  if port_up "$port"; then ok "$name 已在运行（:${port}），跳过"; return 0; fi
  if alive "$pid_name"; then ok "$name 进程存活但端口未监听，等待中…"; return 0; fi
  echo "启动 $name …"
  nohup "$@" >"$LOG_DIR/dev-$name.log" 2>&1 &
  save_pid "$pid_name" $!
}

step "应用进程"
start_bg api    api    "$API_PORT"    uv run uvicorn loomvec.api.main:app --reload --port "$API_PORT"
external_worker=$(pgrep -f "loomvec.worker.celery_app" | head -1)
if [ -n "$external_worker" ]; then
  if alive worker; then ok "worker 运行中（本脚本）"
  else warn "检测到外部 worker 进程（pid ${external_worker}），不重复启动；如需由本脚本接管请先手动停止"; fi
else
  start_bg worker worker 0 uv run celery -A loomvec.worker.celery_app:celery_app worker -l info -B -Q pipeline,pipeline_high,pipeline_low
fi
start_bg web    web    "$WEB_PORT"    pnpm dev:web
start_bg admin  admin  "$ADMIN_PORT"  pnpm dev:admin

# worker 无端口，用进程+心跳判定
if alive worker || docker exec loomvec-redis redis-cli exists loomvec:worker:heartbeat 2>/dev/null | grep -q 1; then
  ok "worker 运行中（celery + beat）"
else
  warn "worker 心跳未就绪（beat 每 30s 写一次，稍后自行恢复）"
fi

step "就绪等待"
wait_http "API (8080)"  "http://localhost:8080/readyz" 1 120
wait_http "Web (5173)"  "http://localhost:$WEB_PORT/"  1 60
wait_http "Admin (5174)" "http://localhost:$ADMIN_PORT/" 1 60

step "完成"
echo "  用户端     http://localhost:$WEB_PORT    （dev 登录：任意用户名）"
echo "  运维端     http://localhost:$ADMIN_PORT  （dev 登录默认 super_admin）"
echo "  API 文档   http://localhost:$API_PORT/docs"
echo "  监控栈     ./dev.sh obs（admin 首页「打开 Grafana」按钮依赖，默认不随一键启动）"
echo "  日志       tmp/dev-{api,worker,web,admin}.log"
echo "  停止应用   ./dev.sh stop（基础设施保留）"
