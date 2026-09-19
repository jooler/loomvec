# LoomVec agent 沙箱镜像（P5.5a，docs/Research/01 §6.2）
# 构建（deploy.sh 负责；无 COPY，上下文仅为本目录）：
#   docker build -f deploy/compose/sandbox/Dockerfile -t loomvec/agent-sandbox:stable deploy/compose/sandbox
#
# 设计要点：
# - 依赖基底 = deepseek-harness-sdk（PyPI，版本经构建参数单源于 services/agent/pyproject.toml）
#   + 自包含 runtime wheel（F3/F4：自带 Linux x64/arm64 原生可执行，无需系统 Node）；
# - 仅依赖镜像内置 pip，不引入 ghcr.io 的 uv 镜像层——部署环境的仓库镜像加速
#   通常只覆盖 docker.io，ghcr.io 拉取可能不可达（实测卡死构建）；
# - 入口即 dsh（argv 由网关注入 --profile sdk，F1），无任何常驻守护；
# - 运行期约束由网关 docker run 注入：--user（网关 uid，MVP 统一 uid）、
#   cap-drop ALL、no-new-privileges、只读 rootfs、tmpfs /tmp 与 $HOME（§6.3）；
#   $HOME 由运行期 --tmpfs 提供，镜像内不建 /home/agent；
# - 工具链 = dsh 技能执行的物理载体（shell/git/jq/ripgrep 等常用工具）；
#   重工具链（nodejs/build-essential 等）做独立 tag，不进默认镜像（§6.2）。
FROM python:3.12-slim

ARG SDK_VERSION=0.1.5rc1

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates jq unzip less procps ripgrep \
    && rm -rf /var/lib/apt/lists/*

# 仅装 SDK（含 runtime wheel）：非网关依赖，镜像保持最小。
# 构建自检不用 dsh --version：控制台脚本在 POSIX 上 exec 替换进程且要求
# 非空 DSH_HOME；改用 runtime wheel 的 bundled_runtime_path()（校验可执行
# 文件与伴随文件齐全，缺失抛 FileNotFoundError），不实际拉起进程。
RUN pip install --no-cache-dir "deepseek-harness-sdk==${SDK_VERSION}" \
    && python -c "import deepseek_harness; from deepseek_harness_runtime import bundled_runtime_path; print('runtime:', bundled_runtime_path())" \
    && test -x /usr/local/bin/dsh

# dsh 控制台脚本要求非空 DSH_HOME（运行期由网关 -e 注入 env 真实路径，路径恒等）
ENV DSH_HOME=/tmp/dsh-home

ENTRYPOINT ["/usr/local/bin/dsh"]
