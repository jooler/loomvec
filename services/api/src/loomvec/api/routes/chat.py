"""P3-API-01 问答接口（用户级，跨空间）：会话管理 + SSE 流式问答。

- `GET    /chat/status`                       问答可用性（降级提示）
- `GET    /chat/sessions`                     会话列表（本人，全部空间）
- `POST   /chat/sessions`                     创建会话（可携带召回空间范围）
- `PATCH  /chat/sessions/{sid}`               更新会话（标题 / 召回空间范围）
- `GET    /chat/sessions/{sid}/messages`      历史消息
- `DELETE /chat/sessions/{sid}`               删除会话（软删）
- `POST   /chat/sessions/{sid}/messages`      提问（SSE 流式响应）

会话按用户隔离（仅创建者可见）；召回范围 scope_space_ids 为空列表 = 我的全部空间，
提问时与用户当前可见空间取交集（被移出空间的内容即时剔除）。图谱联合召回常态开启，
仅保留运维全局关停（SystemConfig graph.enabled）作为降级开关。
SSE 协议见 api/services/qa.py 模块注释。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.api.context import Identity
from loomvec.api.deps import get_ai, get_retriever, get_session, require_scope
from loomvec.api.identity import user_uuid
from loomvec.api.services.qa import QaService
from loomvec.api.services.search import enrich_hits, graph_switch
from loomvec.core.authz import SpaceRole, visible_space_ids
from loomvec.core.db.models import ChatMessage, ChatSession, Space
from loomvec.core.db.repos import SpaceMemberRepo
from loomvec.core.errors import ValidationError

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class ChatSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    scope_space_ids: list[uuid.UUID] | None = None  # 空/缺省 = 我的全部空间


class ChatSessionUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    scope_space_ids: list[uuid.UUID] | None = None


class ChatSessionOut(BaseModel):
    session_id: uuid.UUID
    title: str
    scope_space_ids: list[uuid.UUID]
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


def _session_out(s: ChatSession) -> dict[str, Any]:
    return {
        "session_id": str(s.id),
        "title": s.title,
        "scope_space_ids": list(s.scope_space_ids or []),
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


def _scope_space_ids(raw: list[str] | None) -> list[uuid.UUID]:
    return [uuid.UUID(x) for x in raw or []]


async def _assert_member_spaces(
    session: AsyncSession, user_id: uuid.UUID, space_ids: list[uuid.UUID]
) -> None:
    """召回范围合法性：所选空间必须是用户当前可见（成员中）的空间。"""
    visible = set(await visible_space_ids(session, user_id=user_id))
    unknown = [s for s in space_ids if s not in visible]
    if unknown:
        raise ValidationError(
            "存在无权访问的空间", space_ids=[str(s) for s in unknown[:5]]
        )


async def _resolve_chat_scope(
    session: AsyncSession, user_id: uuid.UUID, session_row: ChatSession
) -> tuple[list[uuid.UUID], uuid.UUID | None, dict[uuid.UUID, SpaceRole]]:
    """会话召回范围 × 用户可见空间 → 有效空间集合、租户与角色映射（富集用）。"""
    visible = await visible_space_ids(session, user_id=user_id)
    scope = set(_scope_space_ids(session_row.scope_space_ids))
    effective = [s for s in visible if not scope or s in scope]
    roles = {m.space_id: m.role for m in await SpaceMemberRepo(session).list_for_user(user_id)}
    tenant_ids = (
        list(
            (
                await session.execute(select(Space.tenant_id).where(Space.id.in_(effective)))
            )
            .scalars()
            .all()
        )
        if effective
        else []
    )
    distinct = {t for t in tenant_ids if t is not None}
    # 范围跨租户时不加租户过滤（space_id 集合已是边界），单租户保持原有收敛
    tenant_id = next(iter(distinct)) if len(distinct) == 1 else None
    return effective, tenant_id, roles


@router.get("/status")
async def chat_status(
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    ai=Depends(get_ai),
    request: Request = None,
) -> dict:
    settings = request.app.state.settings
    enabled = settings.qa.enabled and ai.llm_available()
    return {
        "enabled": enabled,
        "reason": None if enabled else ("问答未启用" if not settings.qa.enabled else "LLM 未配置"),
        "graph_enabled": await graph_switch(session, settings),
    }


@router.get("/sessions")
async def list_sessions(
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    qa = QaService(session, None, None)
    rows = await qa.list_sessions(user_uuid(identity))
    return {"items": [_session_out(s) for s in rows], "total": len(rows)}


@router.post("/sessions", status_code=201)
async def create_session(
    body: ChatSessionCreate | None = None,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user_id = user_uuid(identity)
    scope = body.scope_space_ids if body else None
    if scope:
        await _assert_member_spaces(session, user_id, scope)
    qa = QaService(session, None, None)
    row = await qa.create_session(
        user_id, (body.title if body else None), scope_space_ids=scope
    )
    await session.commit()
    return _session_out(row)


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: uuid.UUID,
    body: ChatSessionUpdate,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    user_id = user_uuid(identity)
    qa = QaService(session, None, None)
    row = await qa.get_session(session_id, user_id)
    # scope_space_ids 为 None 表示不改动；空列表是合法值（= 全部我的空间）
    if body.scope_space_ids is not None:
        await _assert_member_spaces(session, user_id, body.scope_space_ids)
    row = await qa.update_session(row, title=body.title, scope_space_ids=body.scope_space_ids)
    await session.commit()
    return _session_out(row)


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    qa = QaService(session, None, None)
    row = await qa.get_session(session_id, user_uuid(identity))
    msgs = await qa.list_messages(row.id)
    return {"items": [_message_out(m) for m in msgs], "total": len(msgs)}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
) -> None:
    qa = QaService(session, None, None)
    row = await qa.get_session(session_id, user_uuid(identity))
    await qa.delete_session(row.id)
    await session.commit()


@router.post("/sessions/{session_id}/messages")
async def ask(
    session_id: uuid.UUID,
    body: ChatQuestion,
    identity: Identity = Depends(require_scope("read")),
    session: AsyncSession = Depends(get_session),
    retriever=Depends(get_retriever),
    ai=Depends(get_ai),
    request: Request = None,
) -> StreamingResponse:
    """提问（SSE 流式）：meta → delta* → done；错误以 error 事件下发。

    召回范围 = 会话 scope_space_ids（空 = 我的全部空间）∩ 当前可见空间；
    图谱联合召回常态开启（运维全局关停时自动降级为向量检索）。
    """
    settings = request.app.state.settings
    if not settings.qa.enabled:
        raise ValidationError("问答未启用")
    if not ai.llm_available():
        raise ValidationError("LLM 未配置，问答暂不可用（检索不受影响）")

    user_id = user_uuid(identity)
    qa = QaService(session, settings, retriever)
    session_row = await qa.get_session(session_id, user_id)
    effective_ids, tenant_id, roles = await _resolve_chat_scope(session, user_id, session_row)
    if not effective_ids:
        raise ValidationError("召回范围为空：请先选择至少一个你有权限的空间")

    use_graph = await graph_switch(session, settings)

    async def enrich(hits):
        return await enrich_hits(session, hits, roles)

    prepared = await qa.prepare(
        tenant_id=tenant_id,
        session_row=session_row,
        question=body.question,
        visible_space_ids=effective_ids,
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
        async for event in qa.stream_answer(prepared, ai, session_row, user_id):
            yield _sse(event["event"], event["data"])

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
