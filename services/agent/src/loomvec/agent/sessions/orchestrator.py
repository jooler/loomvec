"""会话编排：prompt → dsh runtime → SSE 事件泵（14 文档 §5.3 协议）。

事件映射（dsh session.event → SSE；本 checkout 协议只有完成态消息事件，
无逐 token 增量——assistant/message 按「步」到达，一步一个 delta）：
    assistant/message  → delta（text 块）/ reasoning（reasoning 块）
    tool/call          → tool_start
    tool/result        → tool_end（检索工具随后读 Redis 旁路 → citations）
    session.status     → status(running/idle)
    turn/end           → 终态校验（finish_reason）
取消（无 session/cancel 协议）= 终止 runtime 进程；断连不杀进程（后台
收尾，事实源 JSONL 完整，刷新可见）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from loomvec.agent.citations import (
    SEARCH_TOOL,
    CitationAggregator,
    parse_search_result,
    strip_out_of_range_citations,
)
from loomvec.agent.config import AgentRuntimeConfig
from loomvec.agent.runtime.manager import ManagedRuntime, RuntimeManager
from loomvec.agent.runtime.provider import SandboxUnavailableError
from loomvec.agent.sessions import transcript
from loomvec.agent.sessions.usage import TurnStats
from loomvec.agent.sse import sse_event
from loomvec.core.agent_tokens import issue_agent_token
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.orchestrator")

_MAX_TOOL_SUMMARY = 200
# 后台收尾任务强引用（防 GC 中断）
_background_tasks: set[asyncio.Task] = set()


@dataclass
class PromptContext:
    """一次提问的全部输入（routes 组装）。"""

    env_id: str
    tenant_id: str | None
    tenant_name: str
    user_id: str
    user_display_name: str
    session_id: str  # 逻辑会话（agent_session.id，hex 无连字符）
    session_title: str
    scope_space_ids: list[str]  # 会话检索范围（空 = 未设置 → MCP fail-closed 不检索）
    question: str
    attachments: list[dict[str, Any]]  # resolve_attachment 产物
    project_path: str = ""  # 会话绑定项目目录（workspace 相对路径，空 = 根；P5.5a）


class PromptOrchestrator:
    def __init__(self, cfg: AgentRuntimeConfig, manager: RuntimeManager, redis: Any) -> None:
        self._cfg = cfg
        self._manager = manager
        self._redis = redis

    async def _issue_token(self, ctx: PromptContext) -> str:
        """runtime 级 agent JWT（scope=会话检索范围，随提问重签；MCP 侧再与可见空间取交集）。"""
        return issue_agent_token(
            self._cfg.settings,
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            env_id=ctx.env_id,
            session_id=ctx.session_id,
            scope_space_ids=ctx.scope_space_ids,  # 空 = 未设置范围 → MCP fail-closed 不检索
        )

    async def stream(self, ctx: PromptContext) -> AsyncIterator[str]:
        """主泵：yield SSE 帧（SSE 契约见 §5.3）。

        prompt_lock 持有到**底层 run 结束**（客户端断连也不提前释放：
        轮次在后台跑完、JSONL 事实源完整，且规避同 env 并发 prompt）。
        """
        runtime = None
        try:
            runtime = await self._manager.get_or_spawn(
                ctx.env_id,
                tenant_name=ctx.tenant_name,
                user_display_name=ctx.user_display_name,
                token=await self._issue_token(ctx),
            )
        except SandboxUnavailableError as e:
            # fail-closed（docs/Research/01 §6.3-6）：沙箱不可用不回退 L1，
            # 以 SSE error 收尾，前端可见明确错误码
            logger.warning("sandbox_unavailable", env_id=ctx.env_id, error=str(e)[:200])
            yield sse_event(
                "error",
                {"message": f"沙箱不可用：{e}", "code": "sandbox_unavailable"},
            )
            yield sse_event("done", {"message_id": "", "answer": "", "citations": [],
                                     "graph_evidence": [], "usage": {}, "finish_reason": "error"})
            return
        message_id = uuid.uuid4().hex
        model = self._cfg.agent.model.name
        yield sse_event(
            "meta",
            {
                "session_id": ctx.session_id,
                "message_id": message_id,
                "model": model,
                "env_id": ctx.env_id,
            },
        )

        await runtime.prompt_lock.acquire()
        run_ref: list[asyncio.Task] = []
        try:
            runtime.touch(running=True)
            async for frame in self._run_locked(runtime, ctx, message_id, run_ref):
                yield frame
        finally:
            runtime.touch(running=False)
            task = run_ref[0] if run_ref else None
            if task is not None and not task.done():
                # 客户端断连：轮次后台收尾，结束后再放行同 env 下一次提问
                async def _release_when_done() -> None:
                    with contextlib.suppress(BaseException):
                        await task
                    runtime.prompt_lock.release()

                _background_tasks.add(asyncio.create_task(_release_when_done()))
                logger.info("prompt_client_gone_background", session_id=ctx.session_id)
            else:
                runtime.prompt_lock.release()

    # ------------------------------------------------------------------
    # 锁内执行：物理会话定位 + 后台线程 run + 队列泵
    # ------------------------------------------------------------------

    async def _run_locked(
        self,
        runtime: ManagedRuntime,
        ctx: PromptContext,
        message_id: str,
        run_ref: list[asyncio.Task],
    ) -> AsyncIterator[str]:
        sessions_root = self._manager.sessions_root_of(ctx.env_id)
        workspace = self._manager.workspace_of(ctx.env_id)
        logical = ctx.session_id

        physical = self._manager.current_physical(runtime, logical)
        replay_note = ""
        if physical is None:
            gen = transcript.next_physical_gen(sessions_root, workspace, logical)
            physical = transcript.physical_gen(logical, gen)
            self._manager.register_physical(runtime, logical, physical)
            if gen > 0:
                history = transcript.aggregate_transcript(sessions_root, workspace, logical)
                replay_note = transcript.replay_context(history)
                if replay_note:
                    yield sse_event("status", {"status": "running"})

        blocks = self._build_blocks(ctx, replay_note)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_notification(notification) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, notification)

        run_task = asyncio.create_task(
            asyncio.to_thread(
                runtime.handle.run, blocks, session_id=physical, on_notification=on_notification
            )
        )
        run_ref.append(run_task)
        async for frame in self._pump(runtime, ctx, queue, run_task, message_id):
            yield frame

    def _build_blocks(self, ctx: PromptContext, replay_note: str) -> list[dict[str, Any]]:
        """prompt contentBlocks：重放前缀 + 工作目录说明 + 附件说明/图片 + 问题。"""
        blocks: list[dict[str, Any]] = []
        if replay_note:
            blocks.append({"type": "text", "text": replay_note})
        if ctx.project_path:
            # 会话绑定项目目录（P5.5a §6.1）：cwd 是 runtime 级（F9），按项目拆
            # runtime 不经济；以指令前缀约束相对路径落点，与重放前缀同机制
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"本次会话绑定的工作目录为 `{ctx.project_path}/`，"
                        "交付物（文件、代码、报告等）一律写入该目录及其子目录；"
                        "引用其中的文件时使用该目录下的相对路径。"
                    ),
                }
            )
        for att in ctx.attachments:
            if att.get("is_image"):
                with open(att["abs_path"], "rb") as f:
                    data = base64.b64encode(f.read()).decode()
                blocks.append({"type": "image", "data": data, "mimeType": att["mime"]})
            else:
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f"用户附加了文件：`{att['path']}`"
                            f"（{att['mime']}，{att['size']} 字节），请先读取该文件。"
                        ),
                    }
                )
        blocks.append({"type": "text", "text": ctx.question})
        return blocks

    # ------------------------------------------------------------------
    # 通知泵 → SSE 翻译
    # ------------------------------------------------------------------

    async def _pump(
        self,
        runtime: ManagedRuntime,
        ctx: PromptContext,
        queue: asyncio.Queue,
        run_task: asyncio.Task,
        message_id: str,
    ) -> AsyncIterator[str]:
        stats = TurnStats()
        aggregator = CitationAggregator()
        answer_parts: list[str] = []
        tools_by_call: dict[str, str] = {}
        saw_first_assistant = False
        finish_reason: str | None = None
        error_seen: str | None = None
        started = time.monotonic()

        while True:
            if run_task.done() and queue.empty():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                continue
            method = getattr(item, "method", "")
            payload = getattr(item, "payload", {}) or {}
            if method == "session.status":
                status = payload.get("status")
                runtime.touch(running=status == "running")
                if status == "idle":
                    yield sse_event("status", {"status": "idle"})
                continue
            if method != "session.event":
                continue  # subagent.* P2 再透传
            event = payload.get("event") or {}
            etype = event.get("type")
            data = event.get("data") or {}
            if payload.get("sessionId") != self._manager.current_physical(runtime, ctx.session_id):
                continue  # 子代理会话事件（P2 折叠卡片）

            if etype == "assistant/message":
                if not saw_first_assistant:
                    saw_first_assistant = True
                    stats.ttft_ms = int((time.monotonic() - started) * 1000)
                message = data.get("message") or {}
                blocks = message.get("content") or []
                text = "".join(
                    str(b.get("text") or "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                reasoning = "".join(
                    str(b.get("text") or "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "reasoning"
                )
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool-call":
                        tools_by_call[str(b.get("id") or "")] = str(b.get("name") or "")
                # 流式重放：dsh 只在 assistant/message 提交时通知（无逐 token
                # session.event），但其内嵌 stream 记录了每个 chunk 的原文与
                # 真实生成时间差（dt）→ 网关按节奏展开为逐块 delta/reasoning，
                # 用户端获得与原生 AI 对话一致的打字机效果；stream 缺失时
                # 回退为一次性整段帧。
                plan = self._replay_plan(data.get("stream"))
                if plan:
                    async for frame in self._emit_replay(plan):
                        yield frame
                else:
                    if reasoning:
                        yield sse_event("reasoning", {"text": reasoning})
                    if text:
                        yield sse_event("delta", {"text": text})
                if text:
                    answer_parts.append(text)
                    stats.llm_text_chars += len(text)
                stats.usage.add(data.get("usage"))
                yield sse_event("usage", stats.usage.to_event(stats.ttft_ms))
            elif etype == "tool/call":
                call_id = str(data.get("callId") or "")
                tools_by_call[call_id] = str(data.get("name") or "")
                yield sse_event(
                    "tool_start",
                    {
                        "call_id": call_id,
                        "tool": tools_by_call[call_id],
                        "args": str(data.get("arguments") or "")[:400],
                    },
                )
            elif etype == "tool/result":
                call_id, ok, summary = self._tool_result_parts(data)
                tool = tools_by_call.get(call_id, "")
                if tool == SEARCH_TOOL:
                    batch_id, ref_items, evidence = parse_search_result(summary)
                    await aggregator.load_batch(self._redis, ctx.session_id, batch_id)
                    if ref_items:
                        aggregator.add_batch(ref_items, evidence)
                    if aggregator.citations:
                        yield sse_event("citations", aggregator.to_event())
                yield sse_event(
                    "tool_end",
                    {
                        "call_id": call_id,
                        "tool": tool,
                        "ok": ok,
                        "result_summary": summary[:_MAX_TOOL_SUMMARY],
                    },
                )
            elif etype == "turn/end":
                reason = data.get("reason") or {}
                finish_reason = str(reason.get("kind") or "")
                stats.turns += 1
            elif etype in ("approval/asked", "approval/decided"):
                logger.info(
                    "approval_event",
                    session_id=ctx.session_id,
                    kind=etype,
                    tool=str(data.get("tool") or ""),
                )

        # run() 终态（异常 = runtime 被杀/崩溃）
        try:
            result = await run_task
            finish_reason = result.finish_reason or finish_reason
            if finish_reason == "error":
                error_seen = "model_turn_error"
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            if runtime.cancelled:
                yield sse_event("status", {"status": "cancelled"})
            else:
                error_seen = f"runtime_restarted:{type(e).__name__}"

        answer = "".join(answer_parts)
        answer = strip_out_of_range_citations(answer, aggregator.citations)
        if error_seen:
            yield sse_event(
                "error", {"message": "运行时中断，请重试", "code": error_seen.split(":")[0]}
            )
        usage_payload = stats.usage.to_event(stats.ttft_ms)
        usage_payload["turns"] = stats.turns
        yield sse_event(
            "done",
            {
                "message_id": message_id,
                "answer": answer,
                "citations": aggregator.citations,
                "graph_evidence": aggregator.graph_evidence,
                "usage": usage_payload,
                "finish_reason": finish_reason,
            },
        )

    # ------------------------------------------------------------------
    # 流式重放（dsh 增量补偿，§5.3 delta 语义的落实）
    # ------------------------------------------------------------------

    _REPLAY_MIN_DELAY_S = 0.004  # 单 chunk 最小间隔（保打字机观感）
    _REPLAY_MAX_DELAY_S = 0.025  # 单 chunk 最大间隔（真实 dt 可能很大，封顶）
    _REPLAY_BUDGET_S = 4.0  # 整条消息重放总时长预算（超预算等比压缩）

    @classmethod
    def _replay_plan(cls, records: Any) -> list[tuple[str, str, float]]:
        """AssistantStreamRecord[] → (event, text, delay_s) 重放计划。

        text-chunks/reasoning-chunks 的 texts[i] 与 dt[i] 一一对应（dt 为毫秒
        间隔）；其余记录（block-start/end、usage、finish、tool-call-chunks）
        已由外层事件承载，跳过。
        """
        plan: list[tuple[str, str, float]] = []
        if not isinstance(records, list):
            return plan
        for rec in records:
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype == "text-chunks":
                event = "delta"
            elif rtype == "reasoning-chunks":
                event = "reasoning"
            else:
                continue
            texts = rec.get("texts") or []
            dts = rec.get("dt") or []
            for i, chunk_text in enumerate(texts):
                text = str(chunk_text or "")
                if not text:
                    continue
                raw_dt = float(dts[i]) if i < len(dts) else 0.0
                delay = min(max(raw_dt / 1000.0, cls._REPLAY_MIN_DELAY_S), cls._REPLAY_MAX_DELAY_S)
                plan.append((event, text, delay))
        total = sum(d for _, _, d in plan)
        if total > cls._REPLAY_BUDGET_S and total > 0:
            scale = cls._REPLAY_BUDGET_S / total
            plan = [(e, t, d * scale) for e, t, d in plan]
        return plan

    @staticmethod
    async def _emit_replay(plan: list[tuple[str, str, float]]):
        """按计划逐帧 yield，帧间 await sleep（客户端断连即中止）。"""
        for event, text, delay in plan:
            yield sse_event(event, {"text": text})
            if delay > 0:
                await asyncio.sleep(delay)

    @staticmethod
    def _tool_result_parts(data: dict[str, Any]) -> tuple[str, bool, str]:
        """tool/result → (callId, ok, 结果文本)（工具名由 tools_by_call 补齐）。"""
        message = data.get("message") or {}
        blocks = message.get("content") or []
        call_id, summary = "", ""
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool-result":
                call_id = str(b.get("toolCallId") or "")
                inner = b.get("content") or []
                summary = (
                    "".join(
                        str(x.get("text") or "")
                        for x in inner
                        if isinstance(x, dict) and x.get("type") == "text"
                    )
                    if isinstance(inner, list)
                    else str(inner)
                )
                break
        return call_id, not bool(data.get("error")), summary
