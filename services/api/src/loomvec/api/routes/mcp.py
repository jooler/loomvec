"""P5 MCP endpoint：`/api/v1/mcp`（streamable-http，仅 agent token）。

知识库接入 dsh 的唯一通道（14 文档 §6）：工具全部只读，检索链路与
`/api/v1/search` 同源（Retriever.search + enrich_hits），鉴权用网关签发的
per-session 短时 JWT（services/agent_tokens.py）。

- 挂载方式：api 应用 lifespan 内 build_mcp_app(app.state)，Starlette mount；
  授权在 ASGI 包裹层完成（Bearer agent token → ContextVar），MCP 会话管理器
  经返回的 lifespan 上下文随 api 主应用启停；
- `search_knowledge` 每批结果同时写 Redis 引用旁路
  `agent:citations:{session_id}:{batch_id}`（TTL 1h），网关在 tool_end 时
  按 batch_id 聚合（协议升级免疫，14 文档 §5.4）；
- 越权收敛：requested space_ids ∩ token scope ∩ 用户可检索空间
  （成员 ∪ 已链接公共空间），最后一道闸在检索层。
"""

from __future__ import annotations

import json
import uuid
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from loomvec.api.services.agent_tokens import (
    CITATIONS_PREFIX,
    AgentTokenClaims,
    decode_agent_token,
    is_token_revoked,
)
from loomvec.core.authz import SpaceRole, linked_public_space_ids, visible_space_ids
from loomvec.core.config import Settings
from loomvec.core.retrieval import Retriever

# 每请求 agent 身份（ASGI 包裹层写入，工具闭包读取）
_current_claims: ContextVar[AgentTokenClaims | None] = ContextVar(
    "loomvec_mcp_claims", default=None
)

CITATIONS_TTL_S = 3600  # 引用旁路 TTL（14 文档 §5.4）


@dataclass
class McpAppState:
    """api 应用 state 的最小切片（避免循环依赖 app → routes → app）。"""

    settings: Settings
    retriever: Retriever
    session_factory: Any
    redis: aioredis.Redis


def _claims() -> AgentTokenClaims:
    claims = _current_claims.get()
    if claims is None:
        raise RuntimeError("MCP 工具在无 agent 身份上下文中被调用")
    return claims


# ---------------------------------------------------------------------------
# 检索范围收敛（语义对齐 routes/chat.py 的 _resolve_chat_scope）
# ---------------------------------------------------------------------------


async def _effective_scope(
    session, user_id: uuid.UUID, requested: list[uuid.UUID] | None
) -> tuple[list[uuid.UUID], uuid.UUID | None, dict[uuid.UUID, SpaceRole]]:
    """token scope（会话范围）∩ 用户可检索空间 ∩ 请求参数 → 有效空间集合。"""
    from sqlalchemy import select

    from loomvec.core.db.models import Space
    from loomvec.core.db.repos import SpaceMemberRepo

    visible = await visible_space_ids(session, user_id=user_id)
    visible.extend(await linked_public_space_ids(session, user_id=user_id))
    visible = list(dict.fromkeys(visible))
    # fail-closed：token 必须携带具体空间列表（网关按会话 scope 签发）；
    # 空 scope = 该会话未设置检索范围 → 任何空间都不可检索
    scope = {uuid.UUID(s) for s in _claims().scope_list}
    req = set(requested) if requested else None
    effective = [s for s in visible if s in scope and (req is None or s in req)]
    roles = {m.space_id: m.role for m in await SpaceMemberRepo(session).list_for_user(user_id)}
    tenant_ids = (
        list(
            (
                await session.execute(select(Space.tenant_id).where(Space.id.in_(effective)))
            ).scalars()
        )
        if effective
        else []
    )
    distinct = {t for t in tenant_ids if t is not None}
    tenant_id = next(iter(distinct)) if len(distinct) == 1 else None
    return effective, tenant_id, roles


async def _search_once(
    ctx: McpAppState, query: str, requested: list[uuid.UUID] | None, top_k: int, use_graph: bool
) -> dict[str, Any]:
    """检索 + 富集 + 引用旁路写入（search_knowledge 工具体）。"""
    from loomvec.api.services.search import enrich_hits

    claims = _claims()
    async with ctx.session_factory() as session, session.begin():
        space_ids, tenant_id, roles = await _effective_scope(
            session, uuid.UUID(claims.user_id), requested
        )
        if not space_ids:
            return {"batch_id": "", "ref_items": [], "graph_evidence": [], "note": "无可检索空间"}
        hits = await ctx.retriever.search(
            space_ids=space_ids,
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
            use_graph=use_graph,
            session=session,
        )
        items = await enrich_hits(session, hits, roles)

    ref_items: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    seen_ev: set[tuple[str, str, str]] = set()
    for n, item in enumerate(items, start=1):
        ref_items.append(
            {
                "n": n,
                "unit_id": str(item["unit_id"]),
                "asset_id": str(item["asset_id"]),
                "asset_name": item["asset_name"],
                "space_id": str(item.get("space_id") or ""),
                "unit_type": item["unit_type"],
                "title": item.get("title"),
                "text_snippet": (item.get("text") or "")[:200],
                "locator": item.get("locator") or {},
                "score": item.get("score") or 0.0,
            }
        )
        for ev in item.get("graph_evidence") or []:
            key = (str(ev.get("head")), str(ev.get("relation")), str(ev.get("tail")))
            if key not in seen_ev:
                seen_ev.add(key)
                evidence.append(ev)

    batch_id = uuid.uuid4().hex
    if ref_items:
        # 引用旁路：网关 tool_end 时按 batch_id 聚合（不依赖事件流携带完整结果）
        await ctx.redis.set(
            f"{CITATIONS_PREFIX}{claims.session_id}:{batch_id}",
            json.dumps({"ref_items": ref_items, "graph_evidence": evidence}),
            ex=CITATIONS_TTL_S,
        )
    return {"batch_id": batch_id, "ref_items": ref_items, "graph_evidence": evidence}


# ---------------------------------------------------------------------------
# 工具实现（全部只读）
# ---------------------------------------------------------------------------


async def tool_search_knowledge(
    ctx: McpAppState,
    query: str,
    space_ids: list[str] | None = None,
    top_k: int = 8,
    use_graph: bool = True,
) -> dict[str, Any]:
    """知识检索：返回带编号 ref_items（答案引用 [n] 的唯一合法来源）。"""
    top_k = max(1, min(top_k, 20))
    try:
        requested = [uuid.UUID(s) for s in space_ids] if space_ids else None
    except ValueError:
        return {"batch_id": "", "ref_items": [], "graph_evidence": [], "error": "invalid_space_id"}
    return await _search_once(ctx, query, requested, top_k, use_graph)


async def tool_list_my_spaces(ctx: McpAppState) -> list[dict[str, Any]]:
    """当前 token 可见空间（成员 ∪ 已链接公共 ∩ 会话 scope）。"""
    from sqlalchemy import select

    from loomvec.core.db.models import Space
    from loomvec.core.db.repos import SpaceMemberRepo

    claims = _claims()
    async with ctx.session_factory() as session, session.begin():
        space_ids, _, _ = await _effective_scope(session, uuid.UUID(claims.user_id), None)
        if not space_ids:
            return []
        spaces = (
            (
                await session.execute(
                    select(Space).where(Space.id.in_(space_ids), Space.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        roles = {
            m.space_id: m.role.value
            for m in await SpaceMemberRepo(session).list_for_user(uuid.UUID(claims.user_id))
        }
    return [
        {
            "space_id": str(s.id),
            "name": s.name,
            "type": s.type.value if hasattr(s.type, "value") else str(s.type),
            "role": roles.get(s.id, "viewer"),
        }
        for s in spaces
    ]


async def tool_read_unit(ctx: McpAppState, unit_id: str, asset_id: str) -> dict[str, Any]:
    """深读某条检索命中：单元全文 + 父块上下文（越权命中返回 not_found）。"""
    from loomvec.core.db.models import Asset, SemanticUnit

    claims = _claims()
    try:
        unit_uuid = uuid.UUID(unit_id)
        asset_uuid = uuid.UUID(asset_id)
    except ValueError:
        return {"error": "not_found"}
    async with ctx.session_factory() as session, session.begin():
        space_ids, _, _ = await _effective_scope(session, uuid.UUID(claims.user_id), None)
        unit = await session.get(SemanticUnit, unit_uuid)
        if unit is None:
            return {"error": "not_found"}
        asset = await session.get(Asset, unit.asset_id)
        if (
            asset is None
            or asset.deleted_at is not None
            or asset.id != asset_uuid
            or asset.space_id not in space_ids
        ):
            return {"error": "not_found"}
        parent = None
        if unit.parent_id:
            p = await session.get(SemanticUnit, unit.parent_id)
            if p is not None:
                parent = {"unit_id": str(p.id), "title": p.title, "text": p.content}
    return {
        "unit_id": unit_id,
        "asset_id": asset_id,
        "asset_name": asset.name,
        "unit_type": unit.unit_type.value,
        "title": unit.title,
        "text": unit.content,
        "locator": unit.locator or {},
        "parent": parent,
    }


# ---------------------------------------------------------------------------
# ASGI 组装：授权包裹层 + MCPServer（官方 mcp python-sdk，stateless）
# ---------------------------------------------------------------------------


class _AgentAuthASGI:
    """Bearer agent token 校验 → ContextVar → 转发 MCP app；失败 401。"""

    def __init__(self, app: ASGIApp, ctx: McpAppState) -> None:
        self._app = app
        self._ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope)
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            await _unauthorized(scope, receive, send, "缺少 agent token")
            return
        try:
            claims = decode_agent_token(self._ctx.settings, token)
        except Exception:
            await _unauthorized(scope, receive, send, "agent token 无效")
            return
        if await is_token_revoked(self._ctx.redis, claims):
            await _unauthorized(scope, receive, send, "agent token 已失效")
            return
        token_var = _current_claims.set(claims)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_claims.reset(token_var)


async def _unauthorized(scope: Scope, receive: Receive, send: Send, message: str) -> None:
    response = JSONResponse({"error": {"code": -32001, "message": message}}, status_code=401)
    await response(scope, receive, send)


def build_mcp_app(ctx: McpAppState) -> tuple[ASGIApp, AbstractAsyncContextManager[None]]:
    """构建 /api/v1/mcp 的 ASGI app 与会话管理器 lifespan。

    用法（api 应用 lifespan 内）：
        mcp_app, mcp_lifespan = build_mcp_app(McpAppState(...))
        app.mount("/api/v1/mcp", mcp_app)
        async with mcp_lifespan(): ...
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="loomvec",
        title="LoomVec 知识库",
        instructions=(
            "企业知识检索（只读）：回答知识类问题前先检索；引用标注 [n] 只能来自 ref_items 编号。"
        ),
    )

    @server.tool(
        name="search_knowledge", description="检索企业知识库，返回带编号 ref_items 的引用来源"
    )
    async def _search_knowledge(
        query: str,
        space_ids: list[str] | None = None,
        top_k: int = 8,
        use_graph: bool = True,
    ) -> dict[str, Any]:
        return await tool_search_knowledge(ctx, query, space_ids, top_k, use_graph)

    @server.tool(name="list_my_spaces", description="列出当前会话可检索的知识空间")
    async def _list_my_spaces() -> list[dict[str, Any]]:
        return await tool_list_my_spaces(ctx)

    @server.tool(name="read_unit", description="深读一条检索命中的知识单元全文（含父块上下文）")
    async def _read_unit(unit_id: str, asset_id: str) -> dict[str, Any]:
        return await tool_read_unit(ctx, unit_id, asset_id)

    app = server.streamable_http_app(streamable_http_path="/", stateless_http=True)

    @asynccontextmanager
    async def lifespan():
        async with server.session_manager.run():
            yield

    return _AgentAuthASGI(app, ctx), lifespan()
