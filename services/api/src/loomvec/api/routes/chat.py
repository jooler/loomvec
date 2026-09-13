"""P3-API-01 问答接口：会话管理 + SSE 流式问答。

- `POST /spaces/{space_id}/chat/sessions`                创建会话
- `GET  /spaces/{space_id}/chat/sessions`                会话列表（本人）
- `GET  /spaces/{space_id}/chat/sessions/{sid}/messages` 历史消息
- `DELETE /spaces/{space_id}/chat/sessions/{sid}`        删除会话（软删）
- `POST /spaces/{space_id}/chat/sessions/{sid}/messages` 提问（SSE 流式响应）
- `GET  /spaces/{space_id}/chat/status`                  问答可用性（降级提示）

SSE 协议见 api/services/qa.py 模块注释；会话按用户隔离（仅创建者可见）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import (
    get_ai,
    get_retriever,
    get_session,
    require_scope,
    resolve_space_access,
)
from loomvec.api.identity import user_uuid
from loomvec.api.services.qa import QaService
from loomvec.api.services.search import enrich_hits
from loomvec.core.authz import SpaceRole
from loomvec.core.db.models import ChatMessage, ChatSession
from loomvec.core.errors import ValidationError

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)


class ChatSessionOut(BaseModel):
    session_id: uuid.UUID
    space_id: uuid.UUID
    title: str
    created_at: Any = None
    last_message_at: Any = None


class ChatMessageOut(BaseModel):
    message_id: uuid.UUID
    role: str
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    created_at: Any = None


class ChatQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    use_graph: bool | None = None  # 图谱召回开关（默认取全局配置）


def _session_out(s: ChatSession) -> dict[str, Any]:
    return {
        "session_id": str(s.id),
        "space_id": str(s.space_id),
        "title": s.title,
        "created_at": s.created_at,
        "last_message_at": s.last_message_at,
    }


def _message_out(m: ChatMessage) -> dict[str, Any]:
    return {
        "message_id": str(m.id),
        "role": m.role.value,
        "content": m.content,
        "citations": list(m.citations or []),
        "graph_evidence": list(m.graph_evidence or []),
        "model": m.model,
        "created_at": m.created_at,
    }


async def _own_session(
    session: AsyncSession, identity: Identity, space_id: uuid.UUID, session_id: uuid.UUID
) -> ChatSession:
    """会话归属校验：空间成员 + 本人会话。"""
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    qa = QaService(session, None, None)  # 仅复用查询逻辑
    return await qa.get_session(session_id, space_id, uuid.UUID(identity.user_id))


@router.get("/spaces/{space_id}/chat/status")
async def chat_status(
    space_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    ai=Depends(get_ai),
    request: Request = None,
) -> dict:
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    from loomvec.api.services.search import graph_switch

    settings = request.app.state.settings
    enabled = settings.qa.enabled and ai.llm_available()
    return {
        "enabled": enabled,
        "reason": None if enabled else ("问答未启用" if not settings.qa.enabled else "LLM 未配置"),
        "graph_enabled": await graph_switch(session, settings),
    }


@router.get("/spaces/{space_id}/chat/sessions")
async def list_sessions(
    space_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    qa = QaService(session, None, None)
    rows = await qa.list_sessions(space_id, uuid.UUID(identity.user_id))
    return {"items": [_session_out(s) for s in rows], "total": len(rows)}


@router.post("/spaces/{space_id}/chat/sessions", status_code=201)
async def create_session(
    space_id: uuid.UUID,
    body: ChatSessionCreate | None = None,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    qa = QaService(session, None, None)
    row = await qa.create_session(
        space_id, uuid.UUID(identity.user_id), (body.title if body else None)
    )
    await session.commit()
    return _session_out(row)


@router.get("/spaces/{space_id}/chat/sessions/{session_id}/messages")
async def list_messages(
    space_id: uuid.UUID,
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _own_session(session, identity, space_id, session_id)
    qa = QaService(session, None, None)
    msgs = await qa.list_messages(row.id)
    return {"items": [_message_out(m) for m in msgs], "total": len(msgs)}


@router.delete("/spaces/{space_id}/chat/sessions/{session_id}", status_code=204)
async def delete_session(
    space_id: uuid.UUID,
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await _own_session(session, identity, space_id, session_id)
    qa = QaService(session, None, None)
    await qa.delete_session(row.id)
    await session.commit()


@router.post("/spaces/{space_id}/chat/sessions/{session_id}/messages")
async def ask(
    space_id: uuid.UUID,
    session_id: uuid.UUID,
    body: ChatQuestion,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    retriever=Depends(get_retriever),
    ai=Depends(get_ai),
    request: Request = None,
) -> StreamingResponse:
    """提问（SSE 流式）：meta → delta* → done；错误以 error 事件下发。"""
    settings = request.app.state.settings
    access = await resolve_space_access(session, identity, space_id, SpaceRole.VIEWER)
    if not settings.qa.enabled:
        raise ValidationError("问答未启用")
    if not ai.llm_available():
        raise ValidationError("LLM 未配置，问答暂不可用（检索不受影响）")

    qa = QaService(session, settings, retriever)
    session_row = await qa.get_session(session_id, space_id, uuid.UUID(identity.user_id))
    from loomvec.api.services.search import graph_switch

    use_graph = await graph_switch(session, settings)
    if body.use_graph is not None:
        use_graph = use_graph and body.use_graph

    async def enrich(hits):
        return await enrich_hits(session, hits, {space_id: access.role})

    prepared = await qa.prepare(
        space_id=space_id,
        tenant_id=access.space.tenant_id,
        session_row=session_row,
        question=body.question,
        visible_space_ids=[space_id],
        use_graph=use_graph,
        asset_enrich=enrich,
    )

    async def _gen():
        yield _sse(
            "meta",
            {
                "session_id": str(session_row.id),
                "citations": prepared["citations"],
                "graph_evidence": prepared["graph_evidence"],
            },
        )
        async for event in qa.stream_answer(prepared, ai, session_row, user_uuid(identity)):
            yield _sse(event["event"], event["data"])

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
