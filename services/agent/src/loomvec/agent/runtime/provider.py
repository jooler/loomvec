"""RuntimeProvider 协议（14 文档 §5.2）：网关其余逻辑不感知隔离级别。

接口只暴露 spawn(env, token_env) / io / terminate(env)；L1 local 为
同机子进程（Python SDK），L2 k8s / L3 vm 为后续阶段实现。
"""

from __future__ import annotations

from typing import Any, Protocol

from loomvec.agent.config import AgentRuntimeConfig


class RuntimeProvider(Protocol):
    """spawn 返回 SDK 客户端句柄（DeepSeekHarness 或等价物）。"""

    async def spawn(
        self, cfg: AgentRuntimeConfig, env_id: str, token_env: dict[str, str]
    ) -> Any: ...

    async def terminate(self, handle: Any) -> None: ...
