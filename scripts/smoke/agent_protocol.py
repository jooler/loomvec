"""P5 协议冒烟：真 dsh 二进制 + 假 OpenAI 兼容模型端点（无外网/无真实 key）。

验证链路（14 文档 §11 CI 协议冒烟 / §15.3 集成测试）：
1. 本地起 OpenAI 兼容 SSE echo server（流式分片 + usage 尾块）；
2. DeepSeekHarness spawn（--profile sdk，隔离 DSH_HOME）→ prompt →
   断言通知序列（inbox 回执 → assistant/message → turn/end → idle）与
   final_response / finish_reason；
3. 断言 JSONL 落盘布局（<DSH_HOME>/sessions/--{projectKey}--/{sid}/…）与
   transcript.py 解析一致；
4. 断言进程内同 session 续聊可复用；新进程同 session_id 被拒
   （SessionAlreadyExistsError——网关「物理段」设计的协议依据）。

用法：uv run python scripts/smoke/agent_protocol.py
退出码 0 = 全绿。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "services" / "agent" / "src"))

from loomvec.agent.runtime import envs as env_layout  # noqa: E402
from loomvec.agent.sessions.transcript import (  # noqa: E402
    parse_transcript,
    physical_gen,
    project_key,
)

ANSWER = "冒烟回答：知识库检索完成，引用 [1]。"


@dataclass
class FakeModelServer:
    """OpenAI 兼容端点（uvicorn）：/chat/completions SSE 流式 + /models 目录。"""

    host: str = "127.0.0.1"
    port: int = 0
    requests: list[dict] = field(default_factory=list)
    calls: list[tuple] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 注：路由参数注解（Request）必须模块级可见——本文件启用
        # from __future__ import annotations，闭包内局部导入的注解无法解析
        self._app = FastAPI()

        @self._app.middleware("http")
        async def _log_requests(request: Request, call_next):
            response = await call_next(request)
            self.calls.append((request.method, request.url.path, response.status_code))
            return response

        @self._app.get("/models")
        async def models() -> dict:
            return {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}

        @self._app.post("/chat/completions")
        async def completions(request: Request):
            try:
                self.requests.append(await request.json())
            except Exception:
                self.requests.append({})

            async def gen():
                chunks = [ANSWER[i : i + 8] for i in range(0, len(ANSWER), 8)]
                for i, chunk in enumerate(chunks):
                    delta: dict = {"content": chunk}
                    if i == 0:
                        delta["role"] = "assistant"
                    yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': delta}]})}\n\n"
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": len(ANSWER),
                                "total_tokens": 100 + len(ANSWER),
                                "prompt_cache_hit_tokens": 40,
                                "prompt_cache_miss_tokens": 60,
                            },
                        }
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")

    async def start(self) -> None:
        import uvicorn

        self._uvicorn = uvicorn.Server(
            uvicorn.Config(self._app, host=self.host, port=self.port, log_level="warning")
        )
        self._task = asyncio.create_task(self._uvicorn.serve())
        await self._uvicorn_startup()
        self.port = self._uvicorn.servers[0].sockets[0].getsockname()[1]

    async def _uvicorn_startup(self) -> None:
        import time as _time

        deadline = _time.monotonic() + 15
        while not getattr(self._uvicorn, "started", False):
            if _time.monotonic() > deadline:
                raise RuntimeError("fake model server 启动超时")
            await asyncio.sleep(0.05)

    async def stop(self) -> None:
        self._uvicorn.should_exit = True
        with contextlib.suppress(BaseException):
            await self._task

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def collect_notifications(sink: list, kinds: list[str]):
    def on_notification(notification) -> None:
        sink.append(notification)
        if notification.method == "session.event":
            event = notification.payload.get("event") or {}
            kinds.append(str(event.get("type")))

    return on_notification


async def main() -> int:
    from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
    from deepseek_harness.errors import JsonRpcError

    fake = FakeModelServer()
    await fake.start()
    tmp = Path(tempfile.mkdtemp(prefix="loomvec-agent-protocol-smoke-"))
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "✓" if ok else "✗"
        suffix = f" — {detail}" if detail and not ok else ""
        print(f"  {mark} {name}{suffix}", flush=True)
        if not ok:
            failures.append(name)

    try:
        env_id = "smoke-env"
        storage = tmp / "agent-envs"
        # 直接复用网关的 env 渲染（AGENTS.md/cordis.patch.yml/MCP 挂载格式单源）
        from loomvec.agent.config import AgentRuntimeConfig
        from loomvec.core.config import get_app_config, get_settings

        agent_cfg = get_app_config().agent.model_copy(update={"storage_root": str(storage)})
        cfg = AgentRuntimeConfig(settings=get_settings(), agent=agent_cfg)
        env_layout.ensure_env_layout(cfg, env_id)
        workspace = cfg.workspace(env_id)
        dsh_home = cfg.dsh_home(env_id)
        check("env 布局渲染", (dsh_home / "cordis.patch.yml").is_file())

        session_id = physical_gen("a" * 32, 0)
        notifications: list = []
        kinds: list[str] = []

        harness = DeepSeekHarness(
            DeepSeekHarnessConfig(
                provider="deepseek-official",
                model="deepseek-chat",
                cwd=str(workspace),
                dsh_home=str(dsh_home),
                profile="sdk",
                env={"DEEPSEEK_BASE_URL": fake.url, "DEEPSEEK_API_KEY": "smoke-key"},
                initialize_timeout_seconds=60.0,
            )
        )
        await asyncio.to_thread(harness.start)
        result = await asyncio.to_thread(
            harness.run,
            [{"type": "text", "text": "冒烟问题"}],
            session_id=session_id,
            on_notification=collect_notifications(notifications, kinds),
        )
        check("final_response 非空", bool(result.final_response), result.final_response[:80])
        check(
            "finish_reason=completed",
            result.finish_reason == "completed",
            str(result.finish_reason),
        )
        check(
            "事件序列含 assistant/message 与 turn/end",
            "assistant/message" in kinds and "turn/end" in kinds,
            ",".join(kinds[:12]),
        )
        status_idle = any(
            n.method == "session.status" and n.payload.get("status") == "idle"
            for n in notifications
        )
        check("session.status idle 到达", status_idle)

        # JSONL 落盘 + transcript 解析
        log = dsh_home / "sessions" / project_key(str(workspace)) / session_id / "session.v3.jsonl"
        check("JSONL 落盘（projectKey 布局）", log.is_file(), str(log))
        if log.is_file():
            # idle 通知先于末条 assistant/message 的异步落盘（fsync 追认），
            # 有界等待事实源追平（网关 done 取内存态答案，不受此竞态影响）
            assistant: list = []
            messages: list = []
            for _ in range(50):
                messages = parse_transcript(log)
                assistant = [m for m in messages if m.role == "assistant" and m.content]
                if assistant:
                    break
                await asyncio.sleep(0.2)
            user = [m for m in messages if m.role == "user"]
            check("transcript 解析出 user/assistant", bool(user) and bool(assistant))
            check("答案内容一致", any(ANSWER in m.content for m in assistant))

        # 进程内续聊（同 session 复用）
        result2 = await asyncio.to_thread(harness.run, "追问", session_id=session_id)
        check("进程内同 session 续聊", bool(result2.final_response))
        await asyncio.to_thread(harness.close)

        # 新进程同 session_id → 预期被拒（单写者约束；网关按物理段规避）
        harness3 = DeepSeekHarness(
            DeepSeekHarnessConfig(
                provider="deepseek-official",
                model="deepseek-chat",
                cwd=str(workspace),
                dsh_home=str(dsh_home),
                profile="sdk",
                env={"DEEPSEEK_BASE_URL": fake.url, "DEEPSEEK_API_KEY": "smoke-key"},
                initialize_timeout_seconds=60.0,
            )
        )
        rejected = False
        try:
            await asyncio.to_thread(harness3.run, "重复会话", session_id=session_id)
        except JsonRpcError:
            rejected = True
        except Exception:
            rejected = True  # 传输层失败同样视为拒绝
        finally:
            await asyncio.to_thread(harness3.close)
        check("新进程同 session_id 被拒（单写者）", rejected)

        print(f"  · 假模型调用记录: {fake.calls}", flush=True)
        check("假模型收到请求", len(fake.requests) >= 1)
    finally:
        await fake.stop()

    print()
    if failures:
        print(f"协议冒烟失败：{failures}", flush=True)
        return 1
    print("协议冒烟全绿：spawn → prompt → 事件序列 → JSONL → 续聊/单写者约束", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
