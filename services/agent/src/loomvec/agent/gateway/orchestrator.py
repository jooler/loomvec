"""经 acp-gateway 的提问流：prompt 组装 → 发起回合 → 事件翻译 → SSE 帧。

SSE 契约不变（meta / delta / reasoning / tool_start / tool_end / citations / usage /
status / error / done）。要点：

- 受理阶段失败（网关 4xx：prompt 预检、配额、会话忙……）在任何帧发出之前发生，
  以 `error` + `done(finish_reason=error)` 两帧收尾（与旧「沙箱不可用」同形状）；
- 客户端（浏览器）断开：只关本连接，**不**取消网关回合（回合在网关后台跑完，
  刷新可见历史）；取消只由显式 `cancel` 触发；
- 流被切断 / 网关发 `event: error`（慢连接、事件落库失败等）：已收到 `turn.started` 的
  回合带着最后的 seq 用 `GET /events?after_seq=` 重连续传，最多 `MAX_RESUMES` 次，
  仍不行才按错误收尾。未收到任何事件就断了（无法确认回合是否已受理）直接报错，不重发 POST。
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from loomvec.agent.attachments import AttachmentMeta
from loomvec.agent.gateway.client import GatewayClient
from loomvec.agent.gateway.errors import GatewayError, GatewayTransportError, user_message
from loomvec.agent.gateway.service import PreparedSession
from loomvec.agent.gateway.translate import CitationsLookup, Frame, TurnTranslator
from loomvec.agent.mentions import build_mention_block
from loomvec.agent.sse import sse_event
from loomvec.core.agent_tokens import publish_mentions
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.gateway.orchestrator")

MAX_RESUMES = 2
_RESUME_DELAY_S = 0.5
_IMAGE_MAX_BYTES = 20 * 1024 * 1024


@dataclass
class AskContext:
    """一次提问的全部输入（routes 组装）。"""

    env_id: str
    session_id: str  # 逻辑会话（agent_session.id.hex，也是 MCP token 的 agent_session_id）
    question: str
    prepared: PreparedSession
    attachments: list[AttachmentMeta] = field(default_factory=list)
    mentions: list[dict[str, str]] = field(default_factory=list)  # [{asset_id, name}]


@dataclass
class Outcome:
    """流结束后供 routes 做元数据收尾（preview 等）。"""

    answer: str = ""
    finish_reason: str | None = None
    ok: bool = False


def attachment_uri(path: str) -> str:
    """workspace 相对路径 → 网关识别的 `workspace:///` URI（网关改写为容器内绝对路径）。"""
    return "workspace:///" + quote(path, safe="/")


async def build_prompt_blocks(
    client: GatewayClient, workspace_id: str, ctx: AskContext
) -> list[dict[str, Any]]:
    """@ 文献说明块 → 附件（图片 image 块；其他 resource_link + 文字说明）→ 问题。

    项目目录由会话 `cwd` 承担，不再往 prompt 拼目录说明。
    """
    blocks: list[dict[str, Any]] = []
    if ctx.mentions:
        blocks.append({"type": "text", "text": build_mention_block(ctx.mentions)})
    for att in ctx.attachments:
        if att.is_image:
            data, _, truncated = await client.read_file(
                workspace_id, att.path, max_bytes=_IMAGE_MAX_BYTES
            )
            if truncated:
                raise GatewayError(413, "payload_too_large", "图片过大")
            blocks.append(
                {"type": "image", "data": base64.b64encode(data).decode(), "mimeType": att.mime}
            )
        else:
            blocks.append(
                {
                    "type": "resource_link",
                    "uri": attachment_uri(att.path),
                    "name": att.name,
                    "mimeType": att.mime,
                }
            )
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"用户附加了文件：`{att.path}`（{att.mime}，{att.size} 字节），"
                        "请先读取该文件。"
                    ),
                }
            )
    blocks.append({"type": "text", "text": ctx.question})
    return blocks


def _error_frames(message: str, code: str) -> list[str]:
    return [
        sse_event("error", {"message": message, "code": code}),
        sse_event(
            "done",
            {
                "message_id": "",
                "answer": "",
                "citations": [],
                "graph_evidence": [],
                "usage": {},
                "finish_reason": "error",
            },
        ),
    ]


class GatewayOrchestrator:
    def __init__(
        self, redis: Any, *, max_resumes: int = MAX_RESUMES, resume_delay_s: float = _RESUME_DELAY_S
    ) -> None:
        self._redis = redis
        self._max_resumes = max_resumes
        self._resume_delay_s = resume_delay_s

    async def stream(
        self, client: GatewayClient, ctx: AskContext, outcome: Outcome | None = None
    ) -> AsyncIterator[str]:
        """主泵：yield SSE 帧。"""
        outcome = outcome if outcome is not None else Outcome()
        message_id = uuid.uuid4().hex
        translator = TurnTranslator(
            session_id=ctx.session_id,
            message_id=message_id,
            model=ctx.prepared.model,
            env_id=ctx.env_id,
        )
        try:
            # 先写 @ 文献（无 @ 则清上一轮残留）：写失败必须中止，否则检索范围与用户预期不一致
            await publish_mentions(
                self._redis, ctx.session_id, [m["asset_id"] for m in ctx.mentions]
            )
        except Exception:
            logger.exception("publish_mentions_failed", session_id=ctx.session_id)
            for f in _error_frames("检索范围设置失败，请稍后重试", "mentions_publish_failed"):
                yield f
            return
        try:
            blocks = await build_prompt_blocks(client, ctx.prepared.workspace_id, ctx)
            async for frame in self._run(client, ctx, translator, blocks):
                yield frame
        except GatewayError as e:
            # 受理阶段失败：此时尚未发出任何帧
            logger.warning("gateway_turn_rejected", status=e.status, code=e.code)
            for f in _error_frames(user_message(e), e.code):
                yield f
        except GatewayTransportError as e:
            logger.warning("gateway_unreachable", reason=e.reason)
            for f in _error_frames(f"无法连接 ACP 网关：{e.reason}", "gateway_unreachable"):
                yield f
        finally:
            outcome.answer = translator.answer
            outcome.finish_reason = translator.finish_reason
            outcome.ok = translator.finished and translator.finish_reason not in (None, "error")

    async def _run(
        self,
        client: GatewayClient,
        ctx: AskContext,
        translator: TurnTranslator,
        blocks: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        sid = ctx.prepared.gateway_session_id
        turn_id: str | None = None
        resumes = 0
        last_error: tuple[str, str] | None = None  # 网关 `event: error` 帧的内容
        first = True
        while True:
            interrupted: str | None = None
            try:
                opener = (
                    client.open_turn(sid, blocks)
                    if first
                    else client.open_events(sid, after_seq=translator.last_seq)
                )
                async with opener as events:
                    async for msg in events:
                        if msg.event == "error":
                            err = msg.data.get("error")
                            err = err if isinstance(err, dict) else {}
                            last_error = (
                                str(err.get("code") or "internal"),
                                str(err.get("message") or ""),
                            )
                            interrupted = f"gateway_stream_error:{last_error[0]}"
                            break
                        event_turn = msg.data.get("turn_id")
                        if turn_id is None and isinstance(event_turn, str):
                            turn_id = event_turn
                        if isinstance(event_turn, str) and event_turn != turn_id:
                            continue  # 续传回放里别的回合的事件
                        for out in translator.feed(msg.event, msg.data):
                            frame = await self._emit(translator, out)
                            if frame is not None:
                                yield frame
                        if translator.finished:
                            return
                    if interrupted is None:
                        interrupted = "stream_ended"
            except GatewayTransportError as e:
                if first and turn_id is None:
                    raise  # 连 POST 都没有确认受理，不重发
                interrupted = f"transport:{e.reason}"
            except GatewayError:
                if first:
                    raise  # 受理阶段 4xx
                interrupted = "resume_rejected"

            # 到这里 = 回合未到终态就断了
            first = False
            if turn_id is None or resumes >= self._max_resumes:
                logger.warning(
                    "gateway_stream_gave_up", session_id=ctx.session_id, reason=interrupted
                )
                code, message = last_error or ("stream_interrupted", "")
                for out in translator.fail(code, message or "与网关的连接中断，请重试"):
                    frame = await self._emit(translator, out)
                    if frame is not None:
                        yield frame
                return
            resumes += 1
            logger.info(
                "gateway_stream_resume",
                session_id=ctx.session_id,
                attempt=resumes,
                after_seq=translator.last_seq,
                reason=interrupted,
            )
            await asyncio.sleep(self._resume_delay_s * resumes)

    async def _emit(self, translator: TurnTranslator, out: Frame | CitationsLookup) -> str | None:
        if isinstance(out, CitationsLookup):
            cited = await translator.resolve_citations(out, self._redis)
            return cited.to_sse() if cited is not None else None
        return out.to_sse()
