#!/usr/bin/env bash
# LoomVec 新环境部署脚本（交互式）
#
# 用法：新环境首次部署运行一次，录入真实 AI 供方后交由 ./dev.sh start 启动：
#   ./deploy.sh    # 交互式录入 对话 LLM / 嵌入 / 重排（可选 VLM / CLIP）的地址、key、模型名，
#                  # 写入 config/loomvec.json（ai.mock=false）；首问选 n 则生成离线 mock 配置
#
# 行为约定：
# - 幂等：config/loomvec.json 已存在时逐项回显现值，直接回车保留原值（覆盖前备份到 tmp/）；
# - 依赖安装：uv sync（core/api/worker/agent workspace）+ pnpm install（apps/* + packages/*）
#   全量拉取子项目依赖，脚本完成后环境即可直接开发或部署；依赖已满足时快速跳过；
# - 端口迁移：历史环境的旧默认端口（5433/9000/8080/5173…）自动迁移到 3xxxx 系列，精确匹配、幂等；
# - 部署标记：成功完成后写入 tmp/loomvec-deployed.stamp，./dev.sh start 依此免于重复拉起部署；
#   被 dev.sh 调起时（LOOMVEC_DEPLOY_INVOKED_BY_DEV=1）跳过"立即启动"询问，返回调用方继续；
# - 用户取消（确认门禁选 n）以退出码 1 结束，dev.sh 会终止本次启动；
# - 不主动启动服务：部署只落配置，启动/停止/状态用 ./dev.sh start|stop|status；
# - 未配置 VLM 时自动置 image.caption_enabled=false（图片描述关闭；管线本就不被 caption 阻断）；
#   未配置 CLIP 时以文搜图在检索侧自动降级，无需处理；
# - 密钥输入不回显；base_url / model 必填，api_key 可留空（本地无鉴权端点）。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
CFG="config/loomvec.json"; EXAMPLE="config/loomvec.example.json"
STAMP="tmp/loomvec-deployed.stamp"  # 成功完成的部署标记；./dev.sh start 依此判断是否需要先部署
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; DIM=$'\033[2m'; RST=$'\033[0m'
ok()   { echo "${GRN}✓${RST} $*"; }
warn() { echo "${YLW}!${RST} $*"; }
fail() { echo "${RED}✗${RST} $*"; }
step() { echo "\n${DIM}── $* ──${RST}"; }

trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

# ask PROMPT CURRENT —— 读一行到 REPLY；空输入 = 保留 CURRENT
ask() {
  local hint=""
  [ -n "$2" ] && hint="${YLW} [当前: $2，回车保留]${RST}"
  read -r -p "$1${hint}： " REPLY; REPLY="$(trim "$REPLY")"
  [ -z "$REPLY" ] && REPLY="$2"
}
# ask_secret PROMPT HAS_CURRENT VAR —— 不回显读取到 VAR；空输入 = 保留原值（VAR 置空）
ask_secret() {
  local hint="" v
  [ "$2" = "1" ] && hint="${YLW} [已设置，回车保留]${RST}"
  read -r -s -p "$1${hint}： " v; echo
  printf -v "$3" '%s' "$(trim "$v")"
}
# ask_url PROMPT CURRENT VAR —— 必填 http(s) URL，非法则重问
ask_url() {
  local v
  while :; do
    ask "$1" "$2"; v="$REPLY"
    case "$v" in http://*|https://*) printf -v "$3" '%s' "${v%/}"; return 0 ;; esac
    fail "base_url 需以 http:// 或 https:// 开头"
  done
}
# ask_url_opt PROMPT CURRENT VAR —— 可留空表示跳过该通道
ask_url_opt() {
  local v
  while :; do
    ask "$1" "$2"; v="$REPLY"
    case "$v" in http://*|https://*) v="${v%/}" ;; "" ) ;; * ) fail "需以 http(s):// 开头，或直接回车跳过"; continue ;; esac
    printf -v "$3" '%s' "$v"; return 0
  done
}
# ask_nonempty PROMPT CURRENT VAR
ask_nonempty() {
  while :; do
    ask "$1" "$2"; [ -n "$REPLY" ] && { printf -v "$3" '%s' "$REPLY"; return 0; }
    fail "$1 不能为空"
  done
}
# ask_int PROMPT CURRENT DEFAULT VAR
ask_int() {
  local v
  while :; do
    ask "$1（默认 $3）" "$2"; v="$REPLY"; [ -z "$v" ] && v="$3"
    [[ "$v" =~ ^[1-9][0-9]*$ ]] && { printf -v "$4" '%s' "$v"; return 0; }
    fail "需为正整数"
  done
}
# read_cfg KEY（形如 ai.llm.base_url）—— 从 CFG 读现值；缺失 / 模板占位 sk-xxx 视为空
read_cfg() {
  python3 - "$CFG" "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    for k in sys.argv[2].split("."):
        d = d.get(k) if isinstance(d, dict) else None
    v = "" if d is None else str(d)
    print("" if v == "sk-xxx" else v)
except Exception:
    print("")
PY
}
# cur_cfg KEY —— FRESH 工作副本（模板原样）时一律视为空，避免把模板示例当现值回显
cur_cfg() {
  if [ "$FRESH" = "1" ]; then echo ""; else read_cfg "$1"; fi
}
mask_key() {
  local k="$1"
  if [ -z "$k" ]; then echo "(未设置)"; else echo "${k:0:6}****"; fi
}
# kept KEY KEYVAL —— KEYVAL 为空但配置里有现值 = 本次回车保留了原 key
kept() { [ -z "$2" ] && [ -n "$(read_cfg "$1")" ] && echo 1 || echo 0; }
keydesc() {
  if [ -n "$2" ]; then mask_key "$2"
  elif [ "$(kept "$1" "$2")" = "1" ]; then echo "(保留原值)"
  else echo "(未设置)"; fi
}
maybe_start() {
  if [ "${LOOMVEC_DEPLOY_INVOKED_BY_DEV:-0}" = "1" ]; then
    echo "  由 ./dev.sh 调起：部署完成，返回 dev.sh 继续启动"
    return 0
  fi
  echo ""
  read -r -p "是否现在执行 ./dev.sh start 启动全部服务？[y/N]：" GO
  GO="$(trim "$GO")"
  if [[ "$GO" =~ ^[Yy] ]]; then exec ./dev.sh start; fi
  echo "  下一步   ./dev.sh start   # 拉起基础设施 + api/agent/worker + 三个前端"
  echo "  状态     ./dev.sh status"
}
stamp_deploy() {
  mkdir -p tmp
  date "+%Y-%m-%d %H:%M:%S" > "$STAMP"
  ok "已写入部署标记 ${STAMP}（dev.sh 检测到它不再重复拉起部署）"
}

# ---------------------------------------------------------------- 前置检查
step "前置检查"
[ -f "$EXAMPLE" ] || { fail "请在仓库根目录运行（缺少 ${EXAMPLE}）"; exit 1; }
for cmd in python3 uv pnpm; do
  command -v "$cmd" >/dev/null 2>&1 || { fail "缺少 ${cmd}（Python 用 uv 管理，前端用 pnpm）"; exit 1; }
done
ok "python3 / uv / pnpm 就绪"

# ---------------------------------------------------------------- 环境文件（与 dev.sh 同规则，缺失才生成）
[ -f deploy/compose/.env ] || { cp deploy/compose/.env.example deploy/compose/.env; ok "生成 deploy/compose/.env"; }
[ -f .env ] || { cp .env.example .env; ok "生成根 .env（基础设施连接默认值；可后续手工调整）"; }

# ---------------------------------------------------------------- 端口迁移（旧默认端口 → 3xxxx 系列，幂等）
# 2026-09 端口规划：宿主机端口统一加 3 前缀（5433→35433、9000→39000、8080→38080…），
# Milvus 19530→39530（319530 超出 65535 上限，改为首位对齐 3 段）。
# 仅精确命中旧值才改写，重复运行无副作用；容器端口变更在 ./dev.sh start 重建容器时生效。
step "端口迁移（旧默认端口 → 3xxxx）"
MIGRATED=$(python3 - <<'PY'
import json, re, pathlib

PAIRS = [
    # 根 .env（应用端口单源；含注释行，保留行尾注释）
    (r"^(\s*#?\s*LOOMVEC_API_PORT=)8080\b", r"\g<1>38080"),
    (r"^(\s*#?\s*LOOMVEC_WEB_PORT=)5173\b", r"\g<1>35173"),
    (r"^(\s*#?\s*LOOMVEC_ADMIN_PORT=)5174\b", r"\g<1>35174"),
    (r"^(\s*#?\s*LOOMVEC_OPS_PORT=)5175\b", r"\g<1>35175"),
    (r"^(\s*#?\s*LOOMVEC_AGENT_PORT=)8090\b", r"\g<1>38090"),
    (r"^(\s*#?\s*LOOMVEC_WORKER__METRICS_PORT=)9808\b", r"\g<1>39808"),
    (r"^(\s*#?\s*LOOMVEC_OTEL__ENDPOINT=.*)localhost:4317", r"\g<1>localhost:34317"),
    (r"^(\s*#?\s*LOOMVEC_POSTGRES__URL=.*)localhost:5433", r"\g<1>localhost:35433"),
    (r"^(\s*#?\s*LOOMVEC_REDIS__URL=.*)localhost:6379", r"\g<1>localhost:36379"),
    (r"^(\s*#?\s*LOOMVEC_MILVUS__URI=.*)localhost:19530", r"\g<1>localhost:39530"),
    (r"^(\s*#?\s*LOOMVEC_STORAGE__ENDPOINT=.*)localhost:9000", r"\g<1>localhost:39000"),
    (r"^(\s*#?\s*LOOMVEC_AGENT_SERVICE__SERVICE_URL=.*)127\.0\.0\.1:8090", r"\g<1>127.0.0.1:38090"),
    (r"^(\s*#?\s*LOOMVEC_STORAGE__CORS_ALLOWED_ORIGINS=.*)localhost:5175", r"\g<1>localhost:35175"),
    # deploy/compose/.env（基础设施发布端口）
    (r"^(POSTGRES_PORT=)5433\b", r"\g<1>35433"),
    (r"^(REDIS_PORT=)6379\b", r"\g<1>36379"),
    (r"^(RUSTFS_PORT=)9000\b", r"\g<1>39000"),
    (r"^(RUSTFS_CONSOLE_PORT=)9001\b", r"\g<1>39001"),
    (r"^(MILVUS_PORT=)19530\b", r"\g<1>39530"),
    (r"^(MILVUS_METRICS_PORT=)9091\b", r"\g<1>39091"),
    (r"^(MINERU_PORT=)8000\b", r"\g<1>38000"),
    (r"^(ATTU_PORT=)3001\b", r"\g<1>33001"),
    (r"^(KEYCLOAK_PORT=)8088\b", r"\g<1>38088"),
    (r"^(PROMETHEUS_PORT=)9090\b", r"\g<1>39090"),
    (r"^(ALERTMANAGER_PORT=)9093\b", r"\g<1>39093"),
    (r"^(GRAFANA_PORT=)3002\b", r"\g<1>33002"),
    (r"^(LOKI_PORT=)3100\b", r"\g<1>33100"),
]


def migrate_env(path):
    p = pathlib.Path(path)
    if not p.is_file():
        return 0
    text = p.read_text(encoding="utf-8")
    n = 0
    for pat, repl in PAIRS:
        text, k = re.subn(pat, repl, text, flags=re.MULTILINE)
        n += k
    if n:
        p.write_text(text, encoding="utf-8")
    return n


def migrate_json(path):
    """config/loomvec.json 内的端口化 URL（mineru / agent.mcp），精确旧值才改。"""
    p = pathlib.Path(path)
    if not p.is_file():
        return 0
    cfg = json.loads(p.read_text(encoding="utf-8"))
    n = 0
    mineru = cfg.get("mineru") or {}
    if mineru.get("base_url") == "http://localhost:8000":
        mineru["base_url"] = "http://localhost:38000"
        n += 1
    mcp = (cfg.get("agent") or {}).get("mcp") or {}
    url = mcp.get("url")
    if isinstance(url, str) and url.endswith(":8080/api/v1/mcp"):
        mcp["url"] = url[: -len(":8080/api/v1/mcp")] + ":38080/api/v1/mcp"
        n += 1
    if n:
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return n


print(migrate_env(".env") + migrate_env("deploy/compose/.env") + migrate_json("config/loomvec.json"))
PY
) || { fail "端口迁移脚本执行失败"; exit 1; }
if [ "${MIGRATED:-0}" -gt 0 ]; then
  ok "已迁移 ${MIGRATED} 处旧端口 → 3xxxx 系列（容器端口将在 ./dev.sh start 重建容器时生效）"
else
  ok "端口无需迁移"
fi

# ---------------------------------------------------------------- 依赖安装（Python + 前端，全部子项目）
# uv workspace 一条 uv sync 覆盖 core/api/worker/agent 四个后端包；
# pnpm workspace 一条 install 覆盖 apps/* 与 packages/*。二者幂等，依赖已满足时快速跳过。
# 完成后环境即具备开发/生产运行条件，无需再手动装依赖。
step "依赖安装（Python + 前端）"
if uv sync; then
  ok "Python 依赖就绪（uv sync：core/api/worker/agent workspace）"
else
  fail "uv sync 失败（网络或 lockfile 问题，见上方输出）"; exit 1
fi
if pnpm install --frozen-lockfile; then
  ok "前端依赖就绪（pnpm：apps/* + packages/*）"
else
  warn "pnpm --frozen-lockfile 失败，回退普通 install"
  pnpm install || { fail "pnpm install 失败"; exit 1; }
  ok "前端依赖就绪（pnpm）"
fi

# ---------------------------------------------------------------- 工作副本
FRESH=0
[ -f "$CFG" ] || { cp "$EXAMPLE" "$CFG"; FRESH=1; ok "以模板创建工作副本 $CFG"; }

echo ""
echo "${DIM}LoomVec AI 供方配置：对话 LLM / 嵌入 / 重排为必配（RAG 主链路），"
echo "VLM（图片描述）/ CLIP（以文搜图）可选。均走 OpenAI 兼容端点；"
echo "重排与 CLIP 另支持 DashScope 原生协议（api_style=dashscope）。${RST}"
read -r -p "是否配置真实 AI 供方？[Y/n]（n = 离线 mock 模式）：" MODE
MODE="$(trim "$MODE")"; MODE="${MODE:-Y}"

# 已有配置：写入前显式确认（本脚本以自身所在目录为仓库根，勿在他处误调）
if [ "$FRESH" = "0" ]; then
  warn "已存在 ${CFG}：写入前会自动备份到 tmp/；逐项回车 = 保留原值"
  read -r -p "确认修改该文件？[Y/n]：" CONFIRM
  CONFIRM="$(trim "$CONFIRM")"; CONFIRM="${CONFIRM:-Y}"
  [[ "$CONFIRM" =~ ^[Yy] ]] || { echo "已取消部署（未做任何修改）；dev.sh 将终止本次启动"; exit 1; }
fi

if [[ ! "$MODE" =~ ^[Yy] ]]; then
  python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    cfg = json.load(f)
cfg.setdefault("ai", {})["mock"] = True
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  ok "已置 ai.mock=true（确定性本地供方，离线跑通全链路）"
  stamp_deploy
  maybe_start
  exit 0
fi

# ---------------------------------------------------------------- 对话 LLM
step "对话 LLM（OpenAI 兼容 /chat/completions）"
ask_url     "base_url（如 https://api.deepseek.com）" "$(cur_cfg ai.llm.base_url)" LLM_URL
ask_nonempty "模型名（如 deepseek-chat）"             "$(cur_cfg ai.llm.model)" LLM_MODEL
ask_secret  "API Key" "$([ -n "$(cur_cfg ai.llm.api_key)" ] && echo 1 || echo 0)" LLM_KEY

# ---------------------------------------------------------------- 嵌入
step "嵌入模型（OpenAI 兼容 /embeddings；dim 决定 Milvus 集合维度，更换模型/维度需重建集合）"
ask_url     "base_url（如 https://dashscope.aliyuncs.com/compatible-mode/v1）" "$(cur_cfg ai.embedding.base_url)" EMB_URL
ask_nonempty "模型名（如 text-embedding-v4）" "$(cur_cfg ai.embedding.model)" EMB_MODEL
ask_secret  "API Key" "$([ -n "$(cur_cfg ai.embedding.api_key)" ] && echo 1 || echo 0)" EMB_KEY
ask_int     "向量维度 dim"          "$(cur_cfg ai.embedding.dim)" 1024 EMB_DIM
ask_int     "单次批量 batch_size"   "$(cur_cfg ai.embedding.batch_size)" 16 EMB_BATCH

# ---------------------------------------------------------------- 重排
step "重排模型（cross-encoder；OpenAI 兼容端点用 openai，阿里百炼原生用 dashscope）"
ask_url     "base_url（DashScope 原生: https://dashscope.aliyuncs.com/api/v1）" "$(cur_cfg ai.rerank.base_url)" RR_URL
ask_nonempty "模型名（如 qwen3-vl-rerank / bge-reranker-v2-m3）" "$(cur_cfg ai.rerank.model)" RR_MODEL
ask_secret  "API Key" "$([ -n "$(cur_cfg ai.rerank.api_key)" ] && echo 1 || echo 0)" RR_KEY
RR_STYLE_CUR="$(cur_cfg ai.rerank.api_style)"; RR_STYLE_CUR="${RR_STYLE_CUR:-openai}"
RR_STYLE=""
while :; do
  ask "api_style（openai / dashscope）" "$RR_STYLE_CUR"
  case "$REPLY" in openai|dashscope) RR_STYLE="$REPLY"; break ;; esac
  fail "只能填 openai 或 dashscope"
done

# ---------------------------------------------------------------- 可选：VLM / CLIP
step "可选：VLM（图片描述）/ CLIP（以文搜图）——base_url 直接回车即跳过"
echo "${DIM}跳过 VLM 将置 image.caption_enabled=false；跳过 CLIP 检索侧自动降级。${RST}"
ask_url_opt "VLM base_url" "$(cur_cfg ai.vlm.base_url)" VLM_URL
VLM_MODEL=""; VLM_KEY=""
if [ -n "$VLM_URL" ]; then
  ask_nonempty "VLM 模型名（如 qwen3-vl-plus）" "$(cur_cfg ai.vlm.model)" VLM_MODEL
  ask_secret   "VLM API Key" "$([ -n "$(cur_cfg ai.vlm.api_key)" ] && echo 1 || echo 0)" VLM_KEY
fi
ask_url_opt "CLIP base_url" "$(cur_cfg ai.clip.base_url)" CLIP_URL
CLIP_MODEL=""; CLIP_KEY=""
if [ -n "$CLIP_URL" ]; then
  ask_nonempty "CLIP 模型名（如 multimodal-embedding-v1）" "$(cur_cfg ai.clip.model)" CLIP_MODEL
  ask_secret   "CLIP API Key" "$([ -n "$(cur_cfg ai.clip.api_key)" ] && echo 1 || echo 0)" CLIP_KEY
fi

# ---------------------------------------------------------------- 写入配置
mkdir -p tmp
if [ "$FRESH" = "0" ]; then
  BAK="tmp/loomvec.json.bak.$(date +%Y%m%d%H%M%S)"
  cp "$CFG" "$BAK" && ok "原配置已备份：$BAK"
fi

python3 - "$CFG" \
  "$LLM_URL" "$LLM_MODEL" "$LLM_KEY" \
  "$EMB_URL" "$EMB_MODEL" "$EMB_KEY" "$EMB_DIM" "$EMB_BATCH" \
  "$RR_URL" "$RR_MODEL" "$RR_KEY" "$RR_STYLE" \
  "$VLM_URL" "$VLM_MODEL" "$VLM_KEY" \
  "$CLIP_URL" "$CLIP_MODEL" "$CLIP_KEY" <<'PY'
import json, sys

path = sys.argv[1]
(llm_url, llm_model, llm_key,
 emb_url, emb_model, emb_key, emb_dim, emb_batch,
 rr_url, rr_model, rr_key, rr_style,
 vlm_url, vlm_model, vlm_key,
 clip_url, clip_model, clip_key) = sys.argv[2:]

with open(path, encoding="utf-8") as f:
    cfg = json.load(f)

ai = cfg.setdefault("ai", {})
ai["mock"] = False


PLACEHOLDER = "sk-xxx"  # 模板占位密钥：残留会让通道被误判为已配置（带假凭据请求而非降级）


def provider(name, url, model, key, extra=None):
    """url 为空表示本次跳过该通道（保留用户现值，仅清理模板占位）。"""
    d = ai.setdefault(name, {})
    if not url:
        if d.get("api_key") == PLACEHOLDER:
            d["base_url"] = d["model"] = d["api_key"] = None
        return
    d["base_url"], d["model"] = url, model
    if key:
        d["api_key"] = key
    elif d.get("api_key") == PLACEHOLDER:
        d["api_key"] = None
    for k, v in (extra or {}).items():
        if v in (None, ""):
            continue
        d[k] = int(v) if k in ("dim", "batch_size") else v


provider("llm", llm_url, llm_model, llm_key)
provider("embedding", emb_url, emb_model, emb_key, {"dim": emb_dim, "batch_size": emb_batch})
provider("rerank", rr_url, rr_model, rr_key, {"api_style": rr_style})
provider("vlm", vlm_url, vlm_model, vlm_key)
provider("clip", clip_url, clip_model, clip_key)

# VLM 未配置 → 关闭图片描述（caption 失败本就降级不阻断，关掉免得逐图报错）
cfg.setdefault("image", {})["caption_enabled"] = bool(vlm_url)

with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

python3 -m json.tool "$CFG" >/dev/null 2>&1 || { fail "写入的 $CFG 不是合法 JSON"; exit 1; }
step "部署完成"
ok "已写入 ${CFG}（ai.mock=false）"
echo "  对话 LLM   $LLM_URL   $LLM_MODEL   key=$(keydesc ai.llm.api_key "$LLM_KEY")"
echo "  嵌入       $EMB_URL   $EMB_MODEL   dim=$EMB_DIM batch=$EMB_BATCH key=$(keydesc ai.embedding.api_key "$EMB_KEY")"
echo "  重排       $RR_URL    $RR_MODEL    style=$RR_STYLE key=$(keydesc ai.rerank.api_key "$RR_KEY")"
if [ -n "$VLM_URL" ]; then echo "  VLM        $VLM_URL   $VLM_MODEL   key=$(keydesc ai.vlm.api_key "$VLM_KEY")"
else echo "  VLM        （未配置，image.caption_enabled 已置 false）"; fi
if [ -n "$CLIP_URL" ]; then echo "  CLIP       $CLIP_URL   $CLIP_MODEL   key=$(keydesc ai.clip.api_key "$CLIP_KEY")"
else echo "  CLIP       （未配置，以文搜图自动降级）"; fi
warn "改动了 AI 配置时，需重启 api/agent/worker 才生效（./dev.sh stop && ./dev.sh start）"
stamp_deploy
maybe_start
