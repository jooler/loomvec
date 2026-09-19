"""L2 docker provider：每 env 一容器 = 一个 dsh runtime（P5.5a，docs/Research/01 §6.3）。

形态：宿主机网关（F15）经宿主 docker CLI 以 `docker run -i` stdio 桥拉起沙箱
容器，容器入口即 dsh（argv 经 SDK `_launch_args` 整体替换，F1）；容器生命周期
= runtime 生命周期，manager 的惰性 spawn / 复用 / 空闲回收零改动（F11）。

三条关键设计（docs/Research/01 §6）：
- 路径恒等：容器内路径 = 宿主路径，唯一 bind mount 为 env 子目录自身——
  `envs.py` 的 patch 渲染（F13）与 `transcript.py` 的 project_key 分桶（F14）
  零改动。任何引入「容器内路径 ≠ 宿主路径」的改动必须同步重审这两处。
- MVP 统一 uid：容器以网关进程 uid:gid 运行（--user），文件隔离单一边界 =
  子路径挂载；per-env uid 属 P5.5b（F16）。
- fail-closed：daemon 不可达 / 镜像缺失 / 创建失败一律 SandboxUnavailableError，
  绝不回退 L1 裸进程。
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

from loomvec.agent.config import AgentRuntimeConfig
from loomvec.agent.runtime.provider import SandboxUnavailableError
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.runtime.docker")

# deepseek-harness-sdk 顶层导出（惰性导入：启动期不拉运行时二进制）
_HARNESS_CLS = None


def _harness_cls():
    global _HARNESS_CLS
    if _HARNESS_CLS is None:
        from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

        _HARNESS_CLS = (DeepSeekHarness, DeepSeekHarnessConfig)
    return _HARNESS_CLS


def sandbox_container_name(env_id: str) -> str:
    return f"loomvec-agent-env-{env_id}"


async def _docker(
    *args: str, docker_host: str = "", timeout: float = 30.0
) -> tuple[int, str]:
    """运行 docker CLI（网关进程身份，无 socket 挂载）；返回 (rc, 合并输出)。

    docker_host 非空时经 DOCKER_HOST 指向 rootless/remote context（§4-E 部署变体）。
    """
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        raise SandboxUnavailableError("docker CLI 不存在（PATH）")
    env = dict(os.environ)
    if docker_host:
        env["DOCKER_HOST"] = docker_host
    proc = await asyncio.create_subprocess_exec(
        docker_bin,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise SandboxUnavailableError(f"docker {' '.join(args[:2])} 超时") from e
    return proc.returncode or 0, out.decode(errors="replace").strip()


class DockerSandboxProvider:
    """docker run -i stdio 桥：容器即 runtime，terminate 即销毁。"""

    async def spawn(self, cfg: AgentRuntimeConfig, env_id: str, token_env: dict[str, str]) -> Any:
        DeepSeekHarness, DeepSeekHarnessConfig = _harness_cls()
        docker_host = cfg.agent.runtime.sandbox.docker_host
        env_dir = cfg.env_dir(env_id)  # 宿主路径 == 容器内路径（§6.1 路径恒等）
        await self._preflight(cfg, docker_host)

        name = sandbox_container_name(env_id)
        # 名字竞态兜底：池驱逐/复活窗口内同名容器可能尚未退出，docker run
        # --name 会直接失败；rm -f 幂等且阻塞到删除完成（容器不存在也成功）
        await self._run_docker("rm", "-f", name, docker_host=docker_host)

        sb = cfg.agent.runtime.sandbox
        agent = cfg.agent
        # MVP 统一 uid = 网关进程身份（§6.1）：env 树属主即网关用户，
        # 网关读写与沙箱读写天然一致，无需 chown
        uid, gid = os.getuid(), os.getgid()
        argv = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--user",
            f"{uid}:{gid}",
            "--cpus",
            str(sb.cpus),
            "--memory",
            sb.memory,
            "--pids-limit",
            str(sb.pids_limit),
            "--ulimit",
            "nofile=1024:2048",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--read-only",
            # /tmp 与 $HOME tmpfs 的 noexec 是刻意的（不可信临时文件不得成为执行体）；
            # env 子目录挂载不加 noexec（用户构建产物必须可执行，§6.3-7）。
            # 尾缀 :z 为 SELinux 共享重标（非 SELinux 宿主是无操作）：RHEL 系
            # enforcing 下无标签挂载会被拒绝对容器进程可见；重标只改标签不改
            # 路径，路径恒等（F13/F14）不受影响
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--tmpfs",
            f"/home/agent:rw,noexec,nosuid,size={sb.home_tmpfs}",
            "-v",
            f"{env_dir}:{env_dir}:z",
            "--network",
            sb.network,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-e",
            f"DSH_HOME={env_dir / 'dsh-home'}",
            "-e",
            "HOME=/home/agent",
            # 沙箱模式与遥测开关同 local provider 语义（14 文档 §5.5）
            "-e",
            f"DSH_PERMISSION_MODE={agent.sandbox.mode}",
            "-e",
            "DSH_TELEMETRY_DISABLED=1",
            "-e",
            f"LOOMVEC_AGENT_TOKEN={token_env.get('LOOMVEC_AGENT_TOKEN', '')}",
        ]
        if cfg.llm_base_url:
            argv += ["-e", f"DEEPSEEK_BASE_URL={cfg.llm_base_url}"]
        if cfg.llm_api_key:
            argv += ["-e", f"DEEPSEEK_API_KEY={cfg.llm_api_key}"]
        argv += [
            sb.image,
            "--profile",
            agent.runtime.profile,  # patch 由 dsh 从 DSH_HOME 自动加载（F13）
        ]

        harness = DeepSeekHarness(
            DeepSeekHarnessConfig(
                cwd=str(env_dir / "workspace"),  # initialize payload；容器内同路径（F9/F14）
                runtime_cwd=str(env_dir),  # docker CLI 的 Popen cwd（F10，宿主真实存在）
                # _launch_args 就位时 SDK 不消费 dsh_home；传真实路径仅为语义一致
                dsh_home=str(env_dir / "dsh-home"),
                profile=agent.runtime.profile,
                provider=agent.model.provider,
                model=agent.model.name,
                reasoning_effort=(
                    None if agent.model.reasoning_effort == "off" else agent.model.reasoning_effort
                ),
                max_tokens=agent.model.max_tokens,
                # 仅 docker CLI 进程环境（无害）；容器内环境靠上方 -e 注入
                env=dict(token_env),
                initialize_timeout_seconds=float(agent.runtime_initialize_timeout_s),
                request_timeout_seconds=None,  # 轮次不设总限（断连/取消由编排层处理）
                shutdown_timeout_seconds=5.0,
            ),
            _launch_args=tuple(argv),  # F1：整体替换默认 argv
        )
        try:
            await asyncio.to_thread(harness.start)
        except SandboxUnavailableError:
            raise
        except Exception as e:
            # daemon 不可达 / 镜像缺失 / create 失败都表现为 CLI 退出 →
            # initialize 传输关闭；统一 fail-closed，不回退 L1 裸进程
            raise SandboxUnavailableError(
                f"沙箱容器启动失败：{type(e).__name__}（镜像 {sb.image} 是否已构建？"
                f"docker network '{sb.network}' 是否存在？详见 deploy.sh）"
            ) from e
        harness.env_id = env_id  # terminate 兜底清理需要容器名（SDK 句柄是普通对象）
        logger.info(
            "sandbox_runtime_spawned",
            env_id=env_id,
            container=name,
            image=sb.image,
            model=agent.model.name,
        )
        return harness

    async def terminate(self, handle: Any) -> None:
        await asyncio.to_thread(handle.close)  # shutdown RPC → stdin EOF（F2 梯）
        # F2 兜底：close 杀掉的只是 docker CLI；按名字强制清容器（幂等）
        env_id = getattr(handle, "env_id", None)
        if env_id:
            await self._run_docker("rm", "-f", sandbox_container_name(env_id))

    # ------------------------------------------------------------------
    # 孤儿容器 reconcile（§6.3-3）
    # ------------------------------------------------------------------

    async def reconcile(
        self, live_env_ids: set[str], *, docker_host: str = ""
    ) -> int:
        """对账：销毁注册表之外的沙箱容器（网关重启/驱逐竞态残留）。

        会话历史在宿主持久目录且路径恒等（F14），resume 天然恢复，容器可安全清。
        返回清理数。
        """
        rc, out = await _docker(
            "ps",
            "--all",
            "--filter",
            f"name={sandbox_container_name('')}",
            "--format",
            "{{.Names}}",
            docker_host=docker_host,
        )
        if rc != 0:
            return 0
        removed = 0
        prefix = sandbox_container_name("")
        for name in out.splitlines():
            name = name.strip()
            if not name.startswith(prefix):
                continue
            if name[len(prefix) :] in live_env_ids:
                continue
            await self._run_docker("rm", "-f", name, docker_host=docker_host)
            removed += 1
            logger.warning("sandbox_orphan_reclaimed", container=name)
        return removed

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _preflight(self, cfg: AgentRuntimeConfig, docker_host: str) -> None:
        """fail-closed 预检：daemon 可达 + 镜像本地存在（不触发误导性 pull）。"""
        rc, out = await _docker("info", "--format", "{{.ServerVersion}}", docker_host=docker_host)
        if rc != 0:
            raise SandboxUnavailableError(f"docker daemon 不可达：{out[:200]}")
        sb = cfg.agent.runtime.sandbox
        rc, out = await _docker("image", "inspect", sb.image, docker_host=docker_host)
        if rc != 0:
            raise SandboxUnavailableError(
                f"沙箱镜像 {sb.image} 不存在：请先运行 deploy.sh 构建沙箱镜像"
            )

    @staticmethod
    async def _run_docker(*args: str, docker_host: str = "") -> None:
        """结果不关心的 docker 调用（rm -f 类幂等操作）；输出丢弃。"""
        try:
            await _docker(*args, docker_host=docker_host)
        except SandboxUnavailableError as e:
            # rm 幂等失败不阻断主流程（daemon 级故障在 preflight / run 处暴露）
            logger.warning("docker_aux_failed", args=args[:2], error=str(e)[:120])
