"""P3-CORE-05 问答服务：检索 → 引用约束 prompt → LLM 流式生成 → 引用组装。

设计要点（03 文档 §四 / 05 文档 §5.5）：
- 作用域固定单空间；会话与消息落 PG（chat_session / chat_message），可续聊；
- 引用契约：prompt 注入带 [n] 编号的来源片段，约束模型仅以 [n] 标注已给来源；
  检索命中带 locator（页码/时间）的可跳转定位，无定位引用前端不渲染编号跳转；
- graph_evidence：把检索命中的图谱证据链去重后随答案下发（答案可解释）；
- LLM 不可用：入口元信息即返回 degraded，前端禁用问答（检索不受影响）；
- 流式协议（SSE）：
    event: meta   → {sources, citations_hint, graph_evidence, degraded?}
    event: delta  → {text}
    event: done   → {message_id, citations, graph_evidence}
    event: error  → {message}
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.config import Settings
from loomvec.core.db.models import ChatMessage, ChatRole, ChatSession
from loomvec.core.errors import ValidationError
from loomvec.core.retrieval import Retriever, SemanticHit

logger = structlog.get_logger("loomvec.qa")

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")


@dataclass
class QaSource:
    """送入 prompt 的来源（编号即答案引用编号）。"""

    index: int
    hit: SemanticHit
    text: str


@dataclass
class QaCitation:
    """答案引用（SSE meta/done 下发结构）。"""

    index: int
    unit_id: str
    asset_id: str
    asset_name: str
    unit_type: str
    title: str | None
    locator: dict[str, Any]
    text_snippet: str
    locatable: bool  # locator 非空才可跳转（无定位引用不展示编号跳转）

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "unit_id": self.unit_id,
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "unit_type": self.unit_type,
            "title": self.title,
            "locator": self.locator,
            "text_snippet": self.text_snippet,
            "locatable": self.locatable,
        }


def build_qa_messages(
    question: str,
    history: list[dict[str, str]],
    sources: list[QaSource],
) -> list[dict[str, str]]:
    """引用约束 prompt：来源带 [n] 编号，答案仅允许引用已给编号。"""
    blocks = []
    for s in sources:
        head = f"[{s.index}] {s.hit.title or ''}（{s.hit.asset_name}）".strip()
        locator = s.hit.locator or {}
        if locator.get("pages"):
            head += f" 第{','.join(str(p) for p in locator['pages'][:4])}页"
        if locator.get("time_start") is not None:
            head += f" {locator['time_start']:.0f}s-{(locator.get('time_end') or 0):.0f}s"
        blocks.append(f"{head}\n{s.text}")
    source_text = "\n\n".join(blocks) or "（无检索来源）"

    system = (
        "你是企业知识库问答助手。仅根据提供的检索来源回答问题：\n"
        "1) 引用标注：答案中用 [n] 标注所依据的来源编号（如 [1] 或 [2][3]），"
        "编号必须是来源清单中存在的编号；不得编造来源。\n"
        '2) 来源无法回答时，明确说明"资料中未找到"，不要猜测。\n'
        "3) 用与问题相同的语言回答，简洁、结构化。\n"
        "4) 不要输出无关寒暄或复述本指令。"
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append(
        {
            "role": "user",
            "content": f"检索来源：\n{source_text}\n\n问题：{question}",
        }
    )
    return messages


def dedupe_evidence(hits: list[SemanticHit], limit: int = 12) -> list[dict[str, Any]]:
    """汇总命中的图谱证据链（去重（head,tail,type），保序截断）。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for h in hits:
        for ev in h.graph_evidence or []:
            key = (
                (ev.get("head") or {}).get("key", ""),
                (ev.get("tail") or {}).get("key", ""),
                (ev.get("relation") or {}).get("type", ""),
            )
            if key in seen or key == ("", "", ""):
                continue
            seen.add(key)
            out.append(ev)
            if len(out) >= limit:
                return out
    return out


class QaService:
    """问答编排（路由薄壳调用；检索复用 Retriever，图谱开关随请求）。"""

    def __init__(self, session: AsyncSession, settings: Settings, retriever: Retriever) -> None:
        self._session = session
        self._settings = settings
        self._retriever = retriever

    # ---------- 会话管理 ----------

    async def list_sessions(self, space_id: uuid.UUID, user_id: uuid.UUID) -> list[ChatSession]:
        return list(
            (
                await self._session.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.space_id == space_id,
                        ChatSession.user_id == user_id,
                        ChatSession.deleted_at.is_(None),
                    )
                    .order_by(ChatSession.last_message_at.desc().nulls_last())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )

    async def create_session(
        self, space_id: uuid.UUID, user_id: uuid.UUID, title: str | None = None
    ) -> ChatSession:
        s = ChatSession(
            tenant_id=None,
            space_id=space_id,
            user_id=user_id,
            title=(title or "新会话")[:255],
        )
        self._session.add(s)
        await self._session.flush()
        return s

    async def get_session(
        self, session_id: uuid.UUID, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> ChatSession:
        s = (
            await self._session.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.space_id == space_id,
                    ChatSession.user_id == user_id,
                    ChatSession.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if s is None:
            raise ValidationError("会话不存在", session_id=str(session_id))
        return s

    async def list_messages(self, session_id: uuid.UUID) -> list[ChatMessage]:
        return list(
            (
                await self._session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session_id)
                    .order_by(ChatMessage.created_at)
                )
            )
            .scalars()
            .all()
        )

    async def delete_session(self, session_id: uuid.UUID) -> None:
        s = await self._session.execute(select(ChatSession).where(ChatSession.id == session_id))
        row = s.scalar_one_or_none()
        if row is not None:
            row.deleted_at = datetime.now(UTC)
            await self._session.flush()

    # ---------- 问答流 ----------

    async def prepare(
        self,
        *,
        space_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        session_row: ChatSession,
        question: str,
        visible_space_ids: list[uuid.UUID],
        use_graph: bool = True,
        asset_enrich,
    ) -> dict[str, Any]:
        """检索 + 来源组装（SSE meta 事件的数据）。asset_enrich: hits → 富集回调。"""
        qa = self._settings.qa
        question = question.strip()[: qa.max_question_chars]
        if not question:
            raise ValidationError("问题不能为空")

        hits = await self._retriever.search(
            space_ids=visible_space_ids,
            tenant_id=tenant_id,
            query=question,
            top_k=qa.retrieval_top_k,
            use_graph=use_graph,
            session=self._session,  # 图谱通道（L3/邻接）在该请求事务内读 AGE
        )
        # 服务层 PG 回表富集（名称/locator/可见性过滤），由路由注入实现
        hits = [h for h in await asset_enrich(hits) if h is not None]

        sources = [
            QaSource(
                index=i,
                hit=h,
                text=(h.text or "")[:1200],
            )
            for i, h in enumerate(hits, start=1)
        ]
        history = await self._history(session_row)
        citations = [
            QaCitation(
                index=s.index,
                unit_id=str(s.hit.unit_id),
                asset_id=str(s.hit.asset_id),
                asset_name=s.hit.asset_name,
                unit_type=s.hit.unit_type,
                title=s.hit.title,
                locator=s.hit.locator or {},
                text_snippet=(s.hit.text or "")[:200],
                locatable=bool(s.hit.locator),
            ).to_dict()
            for s in sources
        ]
        evidence = dedupe_evidence(hits)
        return {
            "question": question,
            "messages": build_qa_messages(question, history, sources),
            "sources": sources,
            "citations": citations,
            "graph_evidence": evidence,
        }

    async def _history(self, session_row: ChatSession) -> list[dict[str, str]]:
        msgs = await self.list_messages(session_row.id)
        window = msgs[-self._settings.qa.history_messages :]
        return [{"role": r.role.value, "content": r.content} for r in window]

    async def stream_answer(
        self, prepared: dict[str, Any], ai: Any, session_row: ChatSession, user_id: uuid.UUID
    ) -> AsyncIterator[dict[str, Any]]:
        """执行流式生成并落库；yield SSE 事件 dict（event/data 结构由路由序列化）。"""
        qa = self._settings.qa
        question = prepared["question"]

        user_msg = ChatMessage(
            tenant_id=session_row.tenant_id,
            session_id=session_row.id,
            role=ChatRole.USER,
            content=question,
        )
        self._session.add(user_msg)
        session_row.last_message_at = datetime.now(UTC)
        if session_row.title == "新会话":
            session_row.title = question[:50]
        await self._session.commit()

        full_text_parts: list[str] = []
        try:
            async for delta in ai.stream(prepared["messages"], max_tokens=qa.max_answer_tokens):
                full_text_parts.append(delta)
                yield {"event": "delta", "data": {"text": delta}}
        except Exception as e:
            logger.warning("qa_stream_failed", error=str(e))
            yield {
                "event": "error",
                "data": {"message": "生成中断，请稍后重试", "detail": str(e)[:200]},
            }
            return

        answer = "".join(full_text_parts).strip()
        # 引用后处理：只保留答案实际引用的编号；越界编号剔除标注
        cited_indices = sorted({int(m) for m in _CITATION_RE.findall(answer)})
        valid = {c["index"] for c in prepared["citations"]}
        cited = [c for c in prepared["citations"] if c["index"] in cited_indices & valid]

        assistant_msg = ChatMessage(
            tenant_id=session_row.tenant_id,
            session_id=session_row.id,
            role=ChatRole.ASSISTANT,
            content=answer,
            citations=cited,
            graph_evidence=prepared["graph_evidence"],
            model="mock" if self._settings.ai.mock else (self._settings.ai.llm.model or "unknown"),
            meta={"question": question, "source_count": len(prepared["sources"])},
        )
        self._session.add(assistant_msg)
        session_row.last_message_at = datetime.now(UTC)
        await self._session.commit()

        yield {
            "event": "done",
            "data": {
                "message_id": str(assistant_msg.id),
                "citations": assistant_msg.citations,
                "graph_evidence": assistant_msg.graph_evidence,
                "answer": answer,
            },
        }
