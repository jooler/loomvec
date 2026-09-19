#!/usr/bin/env bash
# agent-sandbox 沙箱网络防火墙（P5.5a，docs/Research/01 §6.5-2）
#
# 双链白名单（v1.1：FORWARD 与 INPUT 分别下发）：
# - FORWARD（DOCKER-USER）：沙箱 → 外部。仅放 ESTABLISHED 回包与互联网出口；
#   东西向（沙箱互访）、云 metadata、RFC1918 全拒；
# - INPUT（宿主自身）：沙箱经 host-gateway 触达宿主机服务面（F15 拓扑下 api
#   跑在宿主机，DOCKER-USER 管不到目的为宿主自身的流量）。仅放 api 端口，其余 DROP。
#
# 用法：deploy.sh 幂等下发（Linux + root/iptables 权限）；也可手动执行：
#   sudo deploy/compose/sandbox-firewall.sh [subnet] [api_port]
#
# 规则全部带 loomvec-sbx 注释标记：重跑先清旧再下发，幂等无堆积。
# INPUT 链规则必须 -I 1 插入链首：宿主既有的宽泛 ACCEPT（ufw/firewalld/手工
# 放行 docker 网段）若排在前面，链尾的 DROP 将永远匹配不到，主机隔离静默失效。
# DOCKER-USER 保持 -A 追加（docker 官方惯例：该链专为用户规则保留，默认空+RETURN）。
# 规则引用网段而非接口名；重启丢失后由 deploy.sh / 定时任务重刷（风险表 §11）。
set -euo pipefail

SUBNET="${1:-${AGENT_SANDBOX_SUBNET:-172.31.77.0/24}}"
API_PORT="${2:-${AGENT_SANDBOX_API_PORT:-38080}}"
MARK="loomvec-sbx"

fail() { echo "✗ $*" >&2; exit 1; }
ok()   { echo "✓ $*"; }

command -v iptables >/dev/null 2>&1 || fail "缺少 iptables（非 Linux 环境跳过本脚本）"
[ "$(id -u)" = "0" ] || fail "需要 root 权限（sudo 执行）"

# 前置自检：东西向 DROP 依赖 docker 对桥接流量过 iptables（其自身隔离规则同依赖）；
# 若宿主禁用 bridge-nf-call-iptables，同网桥容器互访将绕过 FORWARD 规则 → 拒绝下发
modprobe br_netfilter 2>/dev/null || true
BR_NF="$(sysctl -n net.bridge.bridge-nf-call-iptables 2>/dev/null || echo 0)"
[ "$BR_NF" = "1" ] || fail "net.bridge.bridge-nf-call-iptables != 1（东西向隔离将失效），拒绝下发；先执行：modprobe br_netfilter && sysctl -w net.bridge.bridge-nf-call-iptables=1"
ok "bridge-nf-call-iptables = 1"

# DOCKER-USER 链必须存在（docker daemon 创建；缺失说明 daemon 未运行过）
iptables -L DOCKER-USER -n >/dev/null 2>&1 || fail "DOCKER-USER 链不存在（docker daemon 是否运行过？）"

# 幂等清旧（按注释标记逐条删除），再追加
flush_marked() { # $1=链名
  local chain="$1" rule
  while :; do
    rule="$(iptables -S "$chain" 2>/dev/null | grep -F -- "$MARK" | head -1)"
    [ -z "$rule" ] && break
    # shellcheck disable=SC2086
    iptables -D "$chain" ${rule#-A $chain } || break
  done
}

flush_marked INPUT
flush_marked DOCKER-USER

# ---- INPUT 链：沙箱 → 宿主机自身（FORWARD/DOCKER-USER 管不到的路径，v1.0 盲区）----
# -I 1 插链首（见文件头）；每次插入都落到位置 1，故按逆序下发，最终链首次序：
# ESTABLISHED → api 放行 → 子网 DROP。ESTABLISHED 覆盖沙箱出站连接的回包
iptables -I INPUT 1 -s "$SUBNET" -m comment --comment "$MARK" -j DROP
iptables -I INPUT 1 -s "$SUBNET" -p tcp --dport "$API_PORT" -m comment --comment "$MARK" -j ACCEPT
iptables -I INPUT 1 -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$MARK" -j ACCEPT

# ---- FORWARD 链（DOCKER-USER）：沙箱 → 外部网络 ----
iptables -A DOCKER-USER -s "$SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -m comment --comment "$MARK" -j ACCEPT
iptables -A DOCKER-USER -s "$SUBNET" -d "$SUBNET" -m comment --comment "$MARK" -j DROP    # 东西向 = 跨用户
iptables -A DOCKER-USER -s "$SUBNET" -d 169.254.169.254/32 -m comment --comment "$MARK" -j DROP  # 云 metadata
iptables -A DOCKER-USER -s "$SUBNET" -d 10.0.0.0/8     -m comment --comment "$MARK" -j DROP  # RFC1918
iptables -A DOCKER-USER -s "$SUBNET" -d 172.16.0.0/12  -m comment --comment "$MARK" -j DROP
# 注：agent-sandbox 自身在 172.16/12 内，东西向 DROP 已前置覆盖，此行不误伤
iptables -A DOCKER-USER -s "$SUBNET" -d 192.168.0.0/16 -m comment --comment "$MARK" -j DROP
# 未匹配流量走 DOCKER-USER 默认 ACCEPT = 允许出网（模型 API/DNS）；域名白名单见 docs/Research/01 §6.5-5
# 注：本脚本仅覆盖 IPv4——agent-sandbox 网络默认无 IPv6 出口，云 metadata 的
# IPv6 形态（如 fd00:ec2::254）不可达。若未来给沙箱网络启用 IPv6，需同步补
# ip6tables 白名单，届时 metadata/RFC1918 等价物一并复查。

# ---- 自检输出（安全清单 #2/#3/#4 的宿主侧断言）----
check() { # $1=链名 其余=规则匹配参数
  local chain="$1"; shift
  iptables -C "$chain" "$@" 2>/dev/null && ok "$chain: $*" || fail "$chain 规则缺失：$*"
}
check INPUT -s "$SUBNET" -p tcp --dport "$API_PORT" -m comment --comment "$MARK" -j ACCEPT
check INPUT -s "$SUBNET" -m comment --comment "$MARK" -j DROP
check DOCKER-USER -s "$SUBNET" -d "$SUBNET" -m comment --comment "$MARK" -j DROP
check DOCKER-USER -s "$SUBNET" -d 10.0.0.0/8 -m comment --comment "$MARK" -j DROP

echo "防火墙就绪：sandbox $SUBNET → 仅放宿主 :$API_PORT + 互联网出口"
echo "规则自检：$(iptables -S INPUT | grep -c -- "$MARK") 条 INPUT / $(iptables -S DOCKER-USER | grep -c -- "$MARK") 条 DOCKER-USER"
