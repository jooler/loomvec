"""RuntimeProvider 协议（14 文档 §5.2）：网关其余逻辑不感知隔离级别。

接口只暴露 spawn(env, token_env) / io / terminate(env)；L1 local 为
同机子进程（Python SDK），L2 docker 为每 env 一容器（P5.5a），k8s / vm
为后续阶段实现。
"""

from __future__ import annotations

from typing import Any, Protocol

from loomvec.agent.config import AgentRuntimeConfig


class SandboxUnavailableError(RuntimeError):
    """沙箱不可用（docker daemon 不可达 / 镜像缺失 / 容器创建失败）。

    fail-closed：provider 实现不得回退 L1 裸进程（docs/Research/01 §6.3-6）；
    orchestrator 捕获后以 SSE error(code=sandbox_unavailable) 收尾。
    """


class RuntimeProvider(Protocol):
    """spawn 返回 SDK 客户端句柄（DeepSeekHarness 或等价物）。"""

    async def spawn(
        self, cfg: AgentRuntimeConfig, env_id: str, token_env: dict[str, str]
    ) -> Any: ...

    async def terminate(self, handle: Any) -> None: ...
