"""RuntimeManager：env → runtime 注册表、惰性 spawn、空闲回收、并发闸。

生命周期（14 文档 §5.1，按 third_party 协议事实适配）：
- 惰性 spawn：env 首请求到达时启动 dsh 子进程（local provider）；
- 复用：同 env 多逻辑会话复用同一进程；**同 env 串行**（P0 单活跃流；
  dsh 同会话跨进程并发写不支持 + 取消即杀进程，串行是最简正确性边界）；
- 空闲回收：session.status == idle 持续 idle_timeout_s 后优雅关闭；
  回收后再来消息走「新物理段 + 历史重放注入」（orchestrator）；
- 取消：terminate（shutdown → stdin-EOF → SIGTERM → SIGKILL 由 SDK close 梯）。

多副本：P0 单副本；K8s 阶段按 env_id 一致性哈希分片 + Redis 租约（§14）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loomvec.agent.config import AgentRuntimeConfig
from loomvec.agent.runtime import envs as env_layout
from loomvec.agent.runtime.local import LocalRuntimeProvider
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.manager")

SPAWN_MUTEX_TTL_S = 60  # Redis agent:envlock:{env_id}（跨进程 spawn 互斥）


@dataclass
class ManagedRuntime:
    """一个 env 的存活 runtime 及其簿记。"""

    env_id: str
    handle: Any  # DeepSeekHarness（local）
    spawned_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    idle_since: float | None = None
    # 本进程内已用的逻辑→物理会话映射（重启后重建时由 orchestrator 再发现）
    physical_sessions: dict[str, str] = field(default_factory=dict)
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancelled: bool = False  # 最近一次 terminate 的原因标记（SSE 终态判定）

    def touch(self, running: bool) -> None:
        self.last_active_at = time.monotonic()
        self.idle_since = None if running else (self.idle_since or time.monotonic())

    def idle_seconds(self) -> float:
        return time.monotonic() - self.idle_since if self.idle_since is not None else 0.0


class RuntimeManager:
    def __init__(self, cfg: AgentRuntimeConfig, redis: Any = None) -> None:
        self._cfg = cfg
        self._redis = redis
        self._provider = LocalRuntimeProvider()
        self._runtimes: dict[str, ManagedRuntime] = {}
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: asyncio.Task | None = None

    # ---------- 查询 ----------

    def get(self, env_id: str) -> ManagedRuntime | None:
        return self._runtimes.get(env_id)

    @property
    def active_count(self) -> int:
        return len(self._runtimes)

    # ---------- spawn / terminate ----------

    async def get_or_spawn(
        self, env_id: str, *, tenant_name: str, user_display_name: str, token: str
    ) -> ManagedRuntime:
        """取活 runtime 或启动新进程（env 目录渲染 + token 注入）。

        token 为 runtime 级 agent JWT（!!js 挂载时求值一次，轮换靠重启；
        scope = 持有者可见空间，MCP 侧实时交集兜底，交接时 JTI 拉黑+回收）。
        """
        existing = self._runtimes.get(env_id)
        if existing is not None:
            return existing
        lock = self._spawn_locks.setdefault(env_id, asyncio.Lock())
        async with lock:
            existing = self._runtimes.get(env_id)
            if existing is not None:
                return existing
            if len(self._runtimes) >= self._cfg.agent.runtime.max_active_runtimes_per_node:
                # 先驱逐最久空闲的（无可驱逐则拒绝）
                victim = min(
                    (r for r in self._runtimes.values() if not r.prompt_lock.locked()),
                    key=lambda r: r.last_active_at,
                    default=None,
                )
                if victim is None:
                    raise RuntimeError("runtime 池已满，请稍后重试")
                await self.terminate(victim.env_id, reason="pool_pressure")
            env_layout.ensure_env_layout(
                self._cfg, env_id, tenant_name=tenant_name, user_display_name=user_display_name
            )
            handle = await self._provider.spawn(self._cfg, env_id, {"LOOMVEC_AGENT_TOKEN": token})
            runtime = ManagedRuntime(env_id=env_id, handle=handle)
            self._runtimes[env_id] = runtime
            logger.info("runtime_registered", env_id=env_id, active=len(self._runtimes))
            return runtime

    async def terminate(self, env_id: str, reason: str = "manual") -> ManagedRuntime | None:
        runtime = self._runtimes.pop(env_id, None)
        if runtime is None:
            return None
        runtime.cancelled = reason == "user_cancel"
        try:
            await self._provider.terminate(runtime.handle)
        except Exception:
            logger.warning("runtime_terminate_failed", env_id=env_id, reason=reason)
        # 回收 spawn 锁（同 env 复活时重建），防止锁字典只增不减
        lock = self._spawn_locks.get(env_id)
        if lock is not None and not lock.locked():
            self._spawn_locks.pop(env_id, None)
        logger.info("runtime_terminated", env_id=env_id, reason=reason)
        return runtime

    # ---------- 物理会话簿记 ----------

    def current_physical(self, runtime: ManagedRuntime, logical_hex: str) -> str | None:
        return runtime.physical_sessions.get(logical_hex)

    def register_physical(self, runtime: ManagedRuntime, logical_hex: str, physical: str) -> None:
        runtime.physical_sessions[logical_hex] = physical

    # ---------- 空闲回收 ----------

    async def start_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop(), name="agent-runtime-reaper")

    async def stop_reaper(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        for env_id in list(self._runtimes):
            await self.terminate(env_id, reason="shutdown")

    async def _reap_loop(self) -> None:
        idle_timeout = self._cfg.agent.runtime.idle_timeout_s
        while True:
            await asyncio.sleep(min(30, max(5, idle_timeout / 10)))
            for env_id, runtime in list(self._runtimes.items()):
                if runtime.prompt_lock.locked():
                    runtime.idle_since = None
                    continue
                if runtime.idle_seconds() >= idle_timeout:
                    await self.terminate(env_id, reason="idle_timeout")

    # ---------- workspace ----------

    def workspace_of(self, env_id: str) -> Path:
        return self._cfg.workspace(env_id)

    def sessions_root_of(self, env_id: str) -> Path:
        return self._cfg.sessions_root(env_id)


def new_logical_session_id() -> str:
    """dsh 物理会话 id 的逻辑前缀（uuid4 hex；物理段 = {hex}-{gen:04d}）。"""
    return uuid.uuid4().hex
