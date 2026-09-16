"""P5 `/api/v1/agent/*`（14 文档 §8）：认证与契约边界在 api，
业务在 services/agent（内网 only），httpx 流式转发（SSE 透传）。
对话由 dsh 全量接管（无 legacy 分支）。

- 所有端点要求用户 JWT；
- 转发头：内部共享密钥 + X-Loomvec-User/Tenant（agent 服务侧信任内网调用）；
- SSE：httpx stream → StreamingResponse 逐块透传，客户端断连自动关上游。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from loomvec.api.context import Identity
from loomvec.api.deps import require_scope
from loomvec.core.config import Settings
from loomvec.core.errors import (
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
    UpstreamUnavailableError,
    ValidationError,
)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


# ---------------------------------------------------------------------------
# 契约模型（openapi → sdk-ts 的类型面；内部转发体与之一致）
# ---------------------------------------------------------------------------


class AgentStatusOut(BaseModel):
    enabled: bool
    runtime_ok: bool
    reason: str | None = None


class AgentSessionCreate(BaseModel):
    title: str | None = None
    scope_space_ids: list[uuid.UUID] | None = None


class AgentSessionUpdate(BaseModel):
    title: str | None = None
    scope_space_ids: list[uuid.UUID] | None = None


class AgentSessionOut(BaseModel):
    id: uuid.UUID
    env_id: uuid.UUID
    title: str
    scope_space_ids: list[uuid.UUID]
    created_at: datetime
    last_message_at: datetime | None = None
    message_count: int = 0
    preview: str = ""
    archived_at: datetime | None = None
    stats: dict[str, Any] | None = None


class AgentSessionListOut(BaseModel):
    items: list[AgentSessionOut]
    total: int


class AgentMessageOut(BaseModel):
    role: str
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    graph_evidence: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None


class AgentMessageListOut(BaseModel):
    items: list[AgentMessageOut]
    total: int


class AgentQuestion(BaseModel):
    question: str = Field(min_length=1)
    attachment_ids: list[str] | None = None


class AgentAttachmentOut(BaseModel):
    attachment_id: str
    path: str
    size: int
    mime: str


class WorkspaceNodeOut(BaseModel):
    path: str
    name: str
    type: str  # file | dir
    size: int = 0


class WorkspaceTreeOut(BaseModel):
    items: list[WorkspaceNodeOut]


class WorkspaceFileOut(BaseModel):
    path: str
    name: str
    size: int
    content: str | None = None  # 文本内容（二进制仅返回元信息 + base64 可后续扩展）
    truncated: bool = False


# ---------------------------------------------------------------------------
# 转发基建
# ---------------------------------------------------------------------------


def _client(request: Request) -> httpx.AsyncClient:
    return request.app.state.agent_client


def _headers(settings: Settings, identity: Identity) -> dict[str, str]:
    return {
        "X-Internal-Token": settings.agent_service.internal_token,
        "X-Loomvec-User-Id": identity.user_id,
        "X-Loomvec-Username": identity.username,
        **({"X-Loomvec-Tenant-Id": identity.tenant_id} if identity.tenant_id else {}),
    }


async def _forward_json(
    request: Request, identity: Identity, method: str, path: str, **kwargs: Any
) -> Any:
    settings: Settings = request.app.state.settings
    try:
        resp = await _client(request).request(
            method, path, headers=_headers(settings, identity), **kwargs
        )
    except httpx.HTTPError as e:
        raise UpstreamUnavailableError("agent", id=str(e)) from e
    if resp.status_code >= 400:
        _raise_for_upstream(resp)
    return resp.json() if resp.content else None


def _raise_for_upstream(resp: httpx.Response) -> None:
    """上游错误状态码 → 同语义的领域异常（保留 404/403/5xx 区分度）。"""
    detail = _error_detail(resp) or f"agent 服务错误（{resp.status_code}）"
    if resp.status_code == 404:
        raise NotFoundError(detail)
    if resp.status_code == 403:
        raise PermissionDeniedError(detail)
    if resp.status_code == 401:
        raise UnauthenticatedError(reason=detail)
    if resp.status_code >= 500:
        raise UpstreamUnavailableError("agent", detail)
    raise ValidationError(detail)


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("detail") or body.get("message") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/status", response_model=AgentStatusOut)
async def agent_status(
    identity: Identity = Depends(require_scope("read")),
    request: Request = None,
) -> AgentStatusOut:
    settings: Settings = request.app.state.settings
    runtime_ok = False
    reason = None
    if not settings.agent.enabled:
        reason = "agent 未启用"
    else:
        try:
            resp = await _client(request).get(
                "/internal/agent/health",
                headers={"X-Internal-Token": settings.agent_service.internal_token},
                timeout=3.0,
            )
            runtime_ok = resp.status_code == 200
            if not runtime_ok:
                reason = "agent 服务不可用"
        except httpx.HTTPError:
            reason = "agent 服务不可达"
    return AgentStatusOut(enabled=settings.agent.enabled, runtime_ok=runtime_ok, reason=reason)


@router.get("/sessions", response_model=AgentSessionListOut)
async def list_sessions(
    identity: Identity = Depends(require_scope("read")),
    request: Request = None,
) -> AgentSessionListOut:
    data = await _forward_json(request, identity, "GET", "/internal/agent/sessions")
    return AgentSessionListOut.model_validate(data)


@router.post("/sessions", response_model=AgentSessionOut, status_code=201)
async def create_session(
    body: AgentSessionCreate | None = None,
    identity: Identity = Depends(require_scope("write")),
    request: Request = None,
) -> AgentSessionOut:
    data = await _forward_json(
        request,
        identity,
        "POST",
        "/internal/agent/sessions",
        json=(body.model_dump(mode="json") if body else {}),
    )
    return AgentSessionOut.model_validate(data)


@router.get("/sessions/{session_id}", response_model=AgentSessionOut)
async def get_session_detail(
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    request: Request = None,
) -> AgentSessionOut:
    data = await _forward_json(request, identity, "GET", f"/internal/agent/sessions/{session_id}")
    return AgentSessionOut.model_validate(data)


@router.patch("/sessions/{session_id}", response_model=AgentSessionOut)
async def update_session(
    session_id: uuid.UUID,
    body: AgentSessionUpdate,
    identity: Identity = Depends(require_scope("write")),
    request: Request = None,
) -> AgentSessionOut:
    data = await _forward_json(
        request,
        identity,
        "PATCH",
        f"/internal/agent/sessions/{session_id}",
        json=body.model_dump(mode="json"),
    )
    return AgentSessionOut.model_validate(data)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("write")),
    request: Request = None,
) -> None:
    await _forward_json(request, identity, "DELETE", f"/internal/agent/sessions/{session_id}")


@router.get("/sessions/{session_id}/messages", response_model=AgentMessageListOut)
async def list_messages(
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("read")),
    request: Request = None,
) -> AgentMessageListOut:
    data = await _forward_json(
        request, identity, "GET", f"/internal/agent/sessions/{session_id}/messages"
    )
    return AgentMessageListOut.model_validate(data)


@router.post("/sessions/{session_id}/messages")
async def ask(
    session_id: uuid.UUID,
    body: AgentQuestion,
    identity: Identity = Depends(require_scope("write")),
    request: Request = None,
) -> StreamingResponse:
    """提问（SSE 流式透传，协议见 14 文档 §5.3）。

    客户端断连时 StreamingResponse 生成器被取消 → finally 关闭上游流
    （agent 服务侧感知断连并 session/cancel）。
    """
    settings: Settings = request.app.state.settings
    req = _client(request).build_request(
        "POST",
        f"/internal/agent/sessions/{session_id}/messages",
        json=body.model_dump(mode="json"),
        headers=_headers(settings, identity),
    )
    try:
        resp = await _client(request).send(req, stream=True)
    except httpx.HTTPError as e:
        raise UpstreamUnavailableError("agent", id=str(e)) from e
    if resp.status_code >= 400:
        await resp.aclose()
        _raise_for_upstream(resp)

    async def _gen():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        _gen(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "text/event-stream"),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/attachments", response_model=AgentAttachmentOut)
async def upload_attachment(
    session_id: uuid.UUID,
    file: UploadFile,
    identity: Identity = Depends(require_scope("write")),
    request: Request = None,
) -> AgentAttachmentOut:
    """附件上传（multipart 流式转发；agent 服务落盘 workspace/.loomvec/uploads/）。"""
    settings: Settings = request.app.state.settings
    att = settings.agent.session.attachments
    max_bytes = att.max_file_mb * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise ValidationError(f"附件超过 {att.max_file_mb}MB 上限")

    async def _chunks():
        while chunk := await file.read(1024 * 1024):  # 1MB 分块，双端均不整包进内存
            yield chunk

    try:
        resp = await _client(request).post(
            f"/internal/agent/sessions/{session_id}/attachments",
            headers=_headers(settings, identity),
            files={"file": (file.filename or "attachment", _chunks(), file.content_type)},
            timeout=120.0,
        )
    except httpx.HTTPError as e:
        raise UpstreamUnavailableError("agent", id=str(e)) from e
    if resp.status_code >= 400:
        _raise_for_upstream(resp)
    return AgentAttachmentOut.model_validate(resp.json())


@router.post("/sessions/{session_id}/cancel")
async def cancel(
    session_id: uuid.UUID,
    identity: Identity = Depends(require_scope("write")),
    request: Request = None,
) -> dict:
    return await _forward_json(
        request, identity, "POST", f"/internal/agent/sessions/{session_id}/cancel"
    )


@router.get("/workspace/tree", response_model=WorkspaceTreeOut)
async def workspace_tree(
    identity: Identity = Depends(require_scope("read")),
    request: Request = None,
) -> WorkspaceTreeOut:
    data = await _forward_json(request, identity, "GET", "/internal/agent/workspace/tree")
    return WorkspaceTreeOut.model_validate(data)


@router.get("/workspace/file", response_model=WorkspaceFileOut)
async def workspace_file(
    path: str,
    identity: Identity = Depends(require_scope("read")),
    request: Request = None,
) -> WorkspaceFileOut:
    data = await _forward_json(
        request, identity, "GET", "/internal/agent/workspace/file", params={"path": path}
    )
    return WorkspaceFileOut.model_validate(data)
