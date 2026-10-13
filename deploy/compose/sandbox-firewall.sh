#!/usr/bin/env bash
# agent-sandbox 沙箱网络防火墙（P5.5a，docs/Research/01 §6.5-2）
#
# 双链白名单（v1.1：FORWARD 与 INPUT 分别下发）：
# - FORWARD（DOCKER-USER）：沙箱 → 外部。仅放 ESTABLISHED 回包与互联网出口；
#   东西向（沙箱互访）、云 metadata（169.254.0.0/16 链路本地、100.64.0.0/10 CGNAT，
#   含阿里云 metadata 100.100.100.200 与 tailscale）、RFC1918 全拒；
# - INPUT（宿主自身）：沙箱经 host-gateway 触达宿主机服务面（F15 拓扑下 api
#   跑在宿主机，DOCKER-USER 管不到目的为宿主自身的流量）。仅放参数指定的端口（默认 api 端口），其余 DROP。
#
# 用法：deploy.sh 幂等下发（Linux + root/iptables 权限）；也可手动执行：
#   sudo deploy/compose/sandbox-firewall.sh [subnet] [port[,port…]]
# 第二个参数是放行给沙箱的宿主端口，可用逗号分隔多个（如 38080,38095：api MCP 回调口 + acp-gateway
# 口）；单端口时行为与过去完全一致。同一子网的 INPUT 规则只能由一个脚本下发（两个脚本各带一条该子网的
# DROP，后跑的排在前面会挡掉另一个放行的端口），所以要放行多个口就在这里列全，不要再叠跑别的脚本。
#
# 规则全部带 loomvec-sbx 注释标记：重跑先清旧再下发，幂等无堆积。清旧与自检都只认「标记 + 本子网」，
# 所以不同子网（如 agent-sandbox 与 acpgw-sandbox）各跑一次互不影响；同一子网重跑以最后一次参数为准。
# 两条链的规则都必须插到链首（-I <序号>），不能 -A 追加：
# - INPUT：宿主既有的宽泛 ACCEPT（ufw/firewalld/手工放行 docker 网段）若排在前面，
#   链尾的 DROP 将永远匹配不到，主机隔离静默失效；
# - DOCKER-USER：dockerd 建链时会先放一条 `-A DOCKER-USER -j RETURN`，追加在它后面的
#   规则永远走不到，沙箱隔离整体静默失效（而「规则存在」的自检照样通过）。
# 脚本末尾的自检因此不只确认规则存在，还核对「带标记的规则正好占据链首、且全部排在
# 第一条 RETURN 之前」，不满足即失败退出。所有 iptables 调用带 -w（等待 xtables 锁）。
# 规则引用网段而非接口名；重启丢失后由 deploy.sh / 定时任务重刷（风险表 §11）。
set -euo pipefail

# -w：等待 xtables 锁（docker / ufw 同时在改规则时不会直接失败）
ipt() { iptables -w "$@"; }

SUBNET="${1:-${AGENT_SANDBOX_SUBNET:-172.31.77.0/24}}"
API_PORT="${2:-${AGENT_SANDBOX_API_PORT:-38080}}"   # 单个端口或逗号分隔的端口列表
MARK="loomvec-sbx"

fail() { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }

# 参数会原样拼进 iptables 命令行：先校验格式，避免把以 - 开头的值当成选项或写出非法规则
cidr_re='^([0-9]{1,3}\.){3}[0-9]{1,3}(/([0-9]|[12][0-9]|3[0-2]))?$'
[[ "$SUBNET" =~ $cidr_re ]] || fail "沙箱子网格式不对（要求 IPv4 CIDR，如 172.31.77.0/24）：$SUBNET"
# 端口列表：逐个校验 1..65535（拒绝空项、非数字），归一成十进制并按出现顺序去重
PORTS=()
IFS=',' read -r -a _raw_ports <<<"$API_PORT"
[ "${#_raw_ports[@]}" -ge 1 ] || fail "端口不合法：$API_PORT"
for _p in "${_raw_ports[@]}"; do
  [[ "$_p" =~ ^[0-9]+$ ]] || fail "端口不合法（要求 1..65535，逗号分隔）：$API_PORT"
  while [[ "$_p" == 0?* ]]; do _p="${_p#0}"; done   # 去前导零（038080 → 38080），也避免 $(( )) 当八进制
  [[ "$_p" =~ ^[0-9]{1,5}$ ]] || fail "端口不合法（要求 1..65535，逗号分隔）：$API_PORT"
  { [ "$_p" -ge 1 ] && [ "$_p" -le 65535 ]; } || fail "端口不合法（要求 1..65535，逗号分隔）：$API_PORT"
  _dup=0
  for _q in ${PORTS[@]+"${PORTS[@]}"}; do [ "$_q" = "$_p" ] && _dup=1; done
  [ "$_dup" = "1" ] || PORTS+=("$_p")
done
[[ "$API_PORT" != *, ]] || fail "端口不合法（末尾多余逗号）：$API_PORT"

command -v iptables >/dev/null 2>&1 || fail "缺少 iptables（非 Linux 环境跳过本脚本）"
[ "$(id -u)" = "0" ] || fail "需要 root 权限（sudo 执行）"

# 前置自检：东西向 DROP 依赖 docker 对桥接流量过 iptables（其自身隔离规则同依赖）；
# 若宿主禁用 bridge-nf-call-iptables，同网桥容器互访将绕过 FORWARD 规则 → 拒绝下发
modprobe br_netfilter 2>/dev/null || true
BR_NF="$(sysctl -n net.bridge.bridge-nf-call-iptables 2>/dev/null || echo 0)"
[ "$BR_NF" = "1" ] || fail "net.bridge.bridge-nf-call-iptables != 1（东西向隔离将失效），拒绝下发；先执行：modprobe br_netfilter && sysctl -w net.bridge.bridge-nf-call-iptables=1"
ok "bridge-nf-call-iptables = 1"

# DOCKER-USER 链必须存在（docker daemon 创建；缺失说明 daemon 未运行过）
ipt -L DOCKER-USER -n >/dev/null 2>&1 || fail "DOCKER-USER 链不存在（docker daemon 是否运行过？）"

# 幂等清旧（按注释标记 + 本子网逐条删除），再下发。只清本子网的规则：同一台宿主可以对多个沙箱网段
# 各跑一次本脚本（如 agent-sandbox 与 acpgw-sandbox 并存），后者不能把前者的规则清掉。
# iptables -S 会把单主机 -s 1.2.3.4 回显成 1.2.3.4/32，匹配键按此归一。
# 没有旧规则是正常情况（首次运行）：管道里的 grep 无匹配会返回 1，在 set -e + pipefail 下
# 会让脚本静默退出、一条规则都不下发，所以先落到变量里并显式 || true
SUBNET_KEY="$SUBNET"
[[ "$SUBNET_KEY" == */* ]] || SUBNET_KEY="$SUBNET_KEY/32"
own_rules() { grep -F -- "$MARK" | grep -F -- " -s $SUBNET_KEY " || true; } # stdin=iptables -S 输出；无匹配不算失败
flush_marked() { # $1=链名
  local chain="$1" rules rule
  while :; do
    rules="$(ipt -S "$chain" 2>/dev/null || true)"
    rule="$(own_rules <<<"$rules" | head -n1)"
    [ -z "$rule" ] && break
    # shellcheck disable=SC2086
    ipt -D "$chain" ${rule#-A $chain } || fail "无法删除 $chain 的旧规则：$rule"
  done
}

flush_marked INPUT
flush_marked DOCKER-USER

# ---- INPUT 链：沙箱 → 宿主机自身（FORWARD/DOCKER-USER 管不到的路径，v1.0 盲区）----
# -I 1 插链首（见文件头）；每次插入都落到位置 1，故按逆序下发，最终链首次序：
# ESTABLISHED → 各端口放行（按参数顺序）→ 子网 DROP。ESTABLISHED 覆盖沙箱出站连接的回包
ipt -I INPUT 1 -s "$SUBNET" -m comment --comment "$MARK" -j DROP
for ((i = ${#PORTS[@]} - 1; i >= 0; i--)); do
  ipt -I INPUT 1 -s "$SUBNET" -p tcp --dport "${PORTS[i]}" -m comment --comment "$MARK" -j ACCEPT
done
ipt -I INPUT 1 -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$MARK" -j ACCEPT
INPUT_RULES=$((2 + ${#PORTS[@]}))
INPUT_TARGETS="ACCEPT$(printf ' ACCEPT%.0s' "${PORTS[@]}") DROP"

# ---- FORWARD 链（DOCKER-USER）：沙箱 → 外部网络 ----
# 从序号 1 起按下发顺序依次 -I，最终次序 = 下发次序，且整体排在 dockerd 预置的 RETURN 之前
pos=1
du() { # 用法：du <匹配条件…> -j <目标>；注释放在目标之前，与 INPUT 规则及自检写法一致
  local target=("${@: -2}") match=("${@:1:$#-2}")
  ipt -I DOCKER-USER "$pos" -s "$SUBNET" "${match[@]}" -m comment --comment "$MARK" "${target[@]}"
  pos=$((pos + 1))
}
du -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
du -d "$SUBNET" -j DROP            # 东西向 = 跨用户
du -d 169.254.0.0/16 -j DROP       # 链路本地（AWS / GCP / Azure metadata 在 169.254.169.254）
du -d 100.64.0.0/10 -j DROP        # CGNAT / tailscale（阿里云 metadata 100.100.100.200 在其中）
du -d 10.0.0.0/8 -j DROP           # RFC1918
du -d 172.16.0.0/12 -j DROP
# 注：agent-sandbox 自身在 172.16/12 内，东西向 DROP 已前置覆盖，此行不误伤
du -d 192.168.0.0/16 -j DROP
DU_RULES=$((pos - 1))
# 未匹配流量走 DOCKER-USER 默认 ACCEPT（链尾 RETURN）= 允许出网（模型 API/DNS）；
# 域名白名单见 docs/Research/01 §6.5-5
# 注：本脚本仅覆盖 IPv4——agent-sandbox 网络默认无 IPv6 出口，云 metadata 的
# IPv6 形态（如 fd00:ec2::254）不可达。若未来给沙箱网络启用 IPv6，需同步补
# ip6tables 白名单，届时 metadata/RFC1918 等价物一并复查。

# ---- 自检输出（安全清单 #2/#3/#4 的宿主侧断言）----
# 规则「存在」不等于「生效」：iptables 自上而下首条匹配即止，所以还要核对次序——
# 带标记的规则必须正好占据链首的 $2 个位置（从而排在 RETURN / ufw 等既有规则之前）。
check() { # $1=链名 其余=规则匹配参数
  local chain="$1"; shift
  ipt -C "$chain" "$@" 2>/dev/null && ok "$chain: $*" || fail "$chain 规则缺失：$*"
}
check_head() { # $1=链名 $2=本脚本下发的规则条数 $3=链首各规则的期望目标（空格分隔，按次序）
  local chain="$1" want="$2" targets="$3" rules marked first_return got
  rules="$(ipt -S "$chain" | grep '^-A ' || true)"
  marked="$(grep -n -F -- "$MARK" <<<"$rules" | grep -F -- " -s $SUBNET_KEY " | cut -d: -f1 | paste -sd, - || true)"
  [ "$marked" = "$(seq -s, 1 "$want")" ] \
    || fail "$chain 中带标记的规则没有占据链首（位置：${marked:-无}，期望 1..$want），隔离不会生效"
  # 次序本身也是语义（INPUT 的 DROP 若排在 ACCEPT 前，api 端口就被误杀）：核对链首各规则的目标
  got="$(head -n "$want" <<<"$rules" | sed -E 's/.* -j ([A-Z]+)$/\1/' | paste -sd' ' -)"
  [ "$got" = "$targets" ] || fail "$chain 链首规则次序不对（目标：$got，期望：$targets）"
  first_return="$(grep -n -E -- ' -j RETURN$' <<<"$rules" | head -1 | cut -d: -f1 || true)"
  if [ -n "$first_return" ] && [ "$first_return" -le "$want" ]; then
    fail "$chain 的 RETURN（第 $first_return 条）排在带标记规则之前"
  fi
  ok "$chain: 前 $want 条均为本脚本规则，且位于首条 RETURN 之前"
}
for _p in "${PORTS[@]}"; do
  check INPUT -s "$SUBNET" -p tcp --dport "$_p" -m comment --comment "$MARK" -j ACCEPT
done
check INPUT -s "$SUBNET" -m comment --comment "$MARK" -j DROP
check DOCKER-USER -s "$SUBNET" -d "$SUBNET" -m comment --comment "$MARK" -j DROP
check DOCKER-USER -s "$SUBNET" -d 10.0.0.0/8 -m comment --comment "$MARK" -j DROP
check DOCKER-USER -s "$SUBNET" -d 169.254.0.0/16 -m comment --comment "$MARK" -j DROP
check DOCKER-USER -s "$SUBNET" -d 100.64.0.0/10 -m comment --comment "$MARK" -j DROP
check_head INPUT "$INPUT_RULES" "$INPUT_TARGETS"
check_head DOCKER-USER "$DU_RULES" "ACCEPT DROP DROP DROP DROP DROP DROP"

PORTS_TXT="$(printf ':%s ' "${PORTS[@]}")"
echo "防火墙就绪：sandbox $SUBNET → 仅放宿主 ${PORTS_TXT}+ 互联网出口"
echo "规则自检：$(ipt -S INPUT | grep -c -- "$MARK" || true) 条 INPUT / $(ipt -S DOCKER-USER | grep -c -- "$MARK" || true) 条 DOCKER-USER"
