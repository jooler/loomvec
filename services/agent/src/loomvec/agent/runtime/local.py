"""L1 local provider：Python SDK 子进程（dsh --profile sdk，stdio JSON-RPC）。

SDK 为同步实现（子进程 + 读线程），所有阻塞调用经 asyncio.to_thread
落到默认线程池；on_notification 回调来自读线程，编排层负责
loop.call_soon_threadsafe 跨线程投递。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loomvec.agent.config import AgentRuntimeConfig
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.runtime")

# deepseek-harness-sdk 顶层导出（惰性导入：启动期不拉运行时二进制）
_HARNESS_CLS = None


def _harness_cls():
    global _HARNESS_CLS
    if _HARNESS_CLS is None:
        from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

        _HARNESS_CLS = (DeepSeekHarness, DeepSeekHarnessConfig)
    return _HARNESS_CLS


class LocalRuntimeProvider:
    """同机子进程 + workspace-write 沙箱 + fail-closed 审批（P0）。"""

    async def spawn(self, cfg: AgentRuntimeConfig, env_id: str, token_env: dict[str, str]) -> Any:
        DeepSeekHarness, DeepSeekHarnessConfig = _harness_cls()
        env = dict(token_env)
        # 沙箱模式固定注入（14 文档 §5.5；danger-full-access 仅 dev 显式开启）
        env["DSH_PERMISSION_MODE"] = cfg.agent.sandbox.mode
        env.setdefault("DSH_TELEMETRY_DISABLED", "1")
        # 显式凭据覆盖（SDK 语义：构造参数 → 子进程环境变量）
        if cfg.llm_api_key:
            env["DEEPSEEK_API_KEY"] = cfg.llm_api_key
        if cfg.llm_base_url:
            env["DEEPSEEK_BASE_URL"] = cfg.llm_base_url

        agent = cfg.agent
        harness = DeepSeekHarness(
            DeepSeekHarnessConfig(
                provider=agent.model.provider,
                model=agent.model.name,
                reasoning_effort=(
                    None if agent.model.reasoning_effort == "off" else agent.model.reasoning_effort
                ),
                max_tokens=agent.model.max_tokens,
                cwd=str(cfg.workspace(env_id)),
                dsh_home=str(cfg.dsh_home(env_id)),
                profile=agent.runtime.profile,
                env=env,
                initialize_timeout_seconds=float(cfg.agent.runtime_initialize_timeout_s),
                request_timeout_seconds=None,  # 轮次不设总限（断连/取消由编排层处理）
                shutdown_timeout_seconds=3.0,
            )
        )
        await asyncio.to_thread(harness.start)
        logger.info("runtime_spawned", env_id=env_id, model=agent.model.name)
        return harness

    async def terminate(self, handle: Any) -> None:
        await asyncio.to_thread(handle.close)
