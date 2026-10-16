"""P5 MCP endpoint：`/api/v1/mcp`（streamable-http，agent token 或用户 PAT）。

知识库的 MCP 通道（14 文档 §6）：工具全部只读，检索链路与 `/api/v1/search`
同源（Retriever.search + enrich_hits）。两类调用方：

- dsh 会话：网关签发的 per-session 短时 JWT（services/agent_tokens.py），
  检索范围 = 会话 scope；
- 外部 MCP 客户端（VS Code / Claude Code 等）：绑定用户的 API Key（PAT），
  `Authorization: Bearer lv_...` 或 `X-API-Key: lv_...`；校验/限流/吊销与 REST
  同一入口（identity.identity_from_api_key），检索范围 = 用户全部可检索空间。

- 挂载方式：api 应用 lifespan 内 build_mcp_app(app.state)，Starlette mount；
  授权在 ASGI 包裹层完成（凭证 → McpCaller → ContextVar），MCP 会话管理器
  经返回的 lifespan 上下文随 api 主应用启停；
- agent 会话的 `search_knowledge` 每批结果同时写 Redis 引用旁路
  `agent:citations:{session_id}:{batch_id}`（TTL 1h），网关在 tool_end 时
  按 batch_id 聚合（协议升级免疫，14 文档 §5.4）；PAT 调用方无会话，不写；
- agent 会话本轮 @ 的文献（Redis `agent:mentions:{agent_session_id}`，agent 服务每轮
  提问前写入/清除）强制收敛 `search_knowledge` 的 asset 范围，不依赖模型自觉传参；
- 越权收敛：requested space_ids ∩ 调用方 scope ∩ 用户可检索空间
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

from loomvec.api.identity import API_KEY_PREFIX, identity_from_api_key
from loomvec.api.services.agent_tokens import (
    CITATIONS_PREFIX,
    AgentTokenClaims,
    decode_agent_token,
    is_token_revoked,
)
from loomvec.core.agent_tokens import mentions_key
from loomvec.core.authz import (
    SpaceRole,
    decide_asset_visibility,
    linked_public_space_ids,
    visible_space_ids,
)
from loomvec.core.config import Settings
from loomvec.core.constants import SCOPE_READ
from loomvec.core.errors import LoomvecError, PermissionDeniedError, UnauthenticatedError
from loomvec.core.retrieval import Retriever


@dataclass(frozen=True)
class McpCaller:
    """MCP 调用方身份（ASGI 包裹层写入 ContextVar，工具闭包读取）。

    - scope_space_ids：调用方自带的空间上限；None = 不额外收敛（PAT 即用户全部
      可检索空间：成员空间 ∪ 已链接公共空间，与 chat 检索同口径）；空集 = 什么都不可检索；
    - session_id：agent 会话 ID（@ 文献收敛 + 引用旁路的键）；None = 无会话（PAT）。
    """

    user_id: uuid.UUID
    scope_space_ids: frozenset[uuid.UUID] | None
    session_id: str | None = None

    @classmethod
    def from_agent_claims(cls, claims: AgentTokenClaims) -> McpCaller:
        # fail-closed：token 必须携带具体空间列表（网关按会话 scope 签发）；
        # 空 scope = 该会话未设置检索范围 → 任何空间都不可检索
        return cls(
            user_id=uuid.UUID(claims.user_id),
            scope_space_ids=frozenset(uuid.UUID(s) for s in claims.scope_list),
            session_id=claims.session_id,
        )


_current_caller: ContextVar[McpCaller | None] = ContextVar("loomvec_mcp_caller", default=None)

CITATIONS_TTL_S = 3600  # 引用旁路 TTL（14 文档 §5.4）


@dataclass
class McpAppState:
    """api 应用 state 的最小切片（避免循环依赖 app → routes → app）。"""

    settings: Settings
    retriever: Retriever
    session_factory: Any
    redis: aioredis.Redis


def _caller() -> McpCaller:
    caller = _current_caller.get()
    if caller is None:
        raise RuntimeError("MCP 工具在无调用方身份上下文中被调用")
    return caller


# ---------------------------------------------------------------------------
# 检索范围收敛（语义对齐 routes/chat.py 的 _resolve_chat_scope）
# ---------------------------------------------------------------------------


async def _effective_scope(
    session, user_id: uuid.UUID, requested: list[uuid.UUID] | None
) -> tuple[list[uuid.UUID], uuid.UUID | None, dict[uuid.UUID, SpaceRole]]:
    """调用方 scope ∩ 用户可检索空间 ∩ 请求参数 → 有效空间集合。"""
    from sqlalchemy import select

    from loomvec.core.db.models import Space
    from loomvec.core.db.repos import SpaceMemberRepo

    visible = await visible_space_ids(session, user_id=user_id)
    visible.extend(await linked_public_space_ids(session, user_id=user_id))
    visible = list(dict.fromkeys(visible))
    scope = _caller().scope_space_ids
    req = set(requested) if requested else None
    effective = [s for s in visible if (scope is None or s in scope) and (req is None or s in req)]
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


async def _mentioned_asset_ids(ctx: McpAppState) -> list[uuid.UUID] | None:
    """本轮提问 @ 的文献（网关已校验可见性）；未 @ 或无会话返回 None = 不限文献。"""
    session_id = _caller().session_id
    if session_id is None:
        return None
    raw = await ctx.redis.get(mentions_key(session_id))
    if not raw:
        return None
    try:
        return [uuid.UUID(a) for a in json.loads(raw)] or None
    except (ValueError, TypeError):
        return None


async def _search_once(
    ctx: McpAppState, query: str, requested: list[uuid.UUID] | None, top_k: int, use_graph: bool
) -> dict[str, Any]:
    """检索 + 富集 + 引用旁路写入（search_knowledge 工具体）。"""
    from loomvec.api.services.search import enrich_hits

    caller = _caller()
    async with ctx.session_factory() as session, session.begin():
        space_ids, tenant_id, roles = await _effective_scope(session, caller.user_id, requested)
        if not space_ids:
            return {"batch_id": "", "ref_items": [], "graph_evidence": [], "note": "无可检索空间"}
        # 能出现在 effective_scope 里就已经代表对该空间有只读以上的访问权限
        # （真实成员含 viewer，或已链接的公共空间），空间内全部资产可见，
        # 知识库标签权限不再作为空间内检索的附加闸门；asset_ids 仅用于本轮 @ 文献收敛。
        hits = await ctx.retriever.search(
            space_ids=space_ids,
            tenant_id=tenant_id,
            asset_ids=await _mentioned_asset_ids(ctx),
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

    # 引用旁路只服务 agent 会话（网关 tool_end 时按 batch_id 聚合，不依赖事件流携带
    # 完整结果）；PAT 调用方无会话、无人消费，batch_id 置空
    batch_id = uuid.uuid4().hex if caller.session_id is not None else ""
    if ref_items and batch_id:
        await ctx.redis.set(
            f"{CITATIONS_PREFIX}{caller.session_id}:{batch_id}",
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
    """当前调用方可见空间（成员 ∪ 已链接公共 ∩ 调用方 scope）。"""
    from sqlalchemy import select

    from loomvec.core.db.models import Space
    from loomvec.core.db.repos import SpaceMemberRepo

    caller = _caller()
    async with ctx.session_factory() as session, session.begin():
        space_ids, _, _ = await _effective_scope(session, caller.user_id, None)
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
            for m in await SpaceMemberRepo(session).list_for_user(caller.user_id)
        }
    return [
        {
            "space_id": str(s.id),
            "name": s.name,
            "type": s.space_type.value,
            "role": roles.get(s.id, "viewer"),
        }
        for s in spaces
    ]


async def tool_read_unit(ctx: McpAppState, unit_id: str, asset_id: str) -> dict[str, Any]:
    """深读某条检索命中：单元全文 + 父块上下文（越权命中返回 not_found）。"""
    from loomvec.core.db.models import Asset, SemanticUnit, Space

    caller = _caller()
    try:
        unit_uuid = uuid.UUID(unit_id)
        asset_uuid = uuid.UUID(asset_id)
    except ValueError:
        return {"error": "not_found"}
    async with ctx.session_factory() as session, session.begin():
        space_ids, _, roles = await _effective_scope(session, caller.user_id, None)
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
        space = await session.get(Space, asset.space_id)
        role = roles.get(asset.space_id, SpaceRole.VIEWER)
        if space is None or not decide_asset_visibility(
            review_required=space.review_required,
            review_status=asset.review_status,
            role=role,
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


class _McpAuthASGI:
    """凭证校验 → McpCaller → ContextVar → 转发 MCP app；失败按错误语义 401/403/429。

    凭证识别：`X-API-Key` 头，或 `Authorization: Bearer` 值带 API Key 前缀 → 用户 PAT；
    其余 Bearer 值 → agent token。
    """

    def __init__(self, app: ASGIApp, ctx: McpAppState) -> None:
        self._app = app
        self._ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        try:
            caller = await self._authenticate(Request(scope))
        except LoomvecError as e:
            # identity 层把具体原因放 details.reason（如「API Key 无效或已过期」），优先透出
            message = e.details.get("reason") or e.message
            response = JSONResponse(
                {"error": {"code": -32001, "message": message}}, status_code=e.http_status
            )
            await response(scope, receive, send)
            return
        token_var = _current_caller.set(caller)
        try:
            await self._app(scope, receive, send)
        finally:
            _current_caller.reset(token_var)

    async def _authenticate(self, request: Request) -> McpCaller:
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        api_key = request.headers.get("x-api-key", "").strip()
        if not api_key and bearer.startswith(API_KEY_PREFIX):
            api_key = bearer
        if api_key:
            return await self._caller_from_api_key(api_key)
        if not bearer:
            raise UnauthenticatedError("缺少 agent token 或 API Key")
        try:
            claims = decode_agent_token(self._ctx.settings, bearer)
            caller = McpCaller.from_agent_claims(claims)  # 非法 UUID claims 同属无效 token
        except Exception as e:
            raise UnauthenticatedError("agent token 或 API Key 无效") from e
        if await is_token_revoked(self._ctx.redis, claims):
            raise UnauthenticatedError("agent token 已失效")
        return caller

    async def _caller_from_api_key(self, raw_key: str) -> McpCaller:
        """PAT 与 REST 同一校验入口（过期/吊销/限流/last_used）；MCP 只认用户语义。"""
        async with self._ctx.session_factory() as session:
            identity = await identity_from_api_key(raw_key, session, self._ctx.redis)
        if identity.user_id.startswith("apikey:"):
            raise PermissionDeniedError("MCP 只接受绑定用户的 API Key（个人中心签发）")
        if SCOPE_READ not in identity.scopes:
            raise PermissionDeniedError("API Key 缺少 read 权限")
        return McpCaller(user_id=uuid.UUID(identity.user_id), scope_space_ids=None)


def build_mcp_app(ctx: McpAppState) -> tuple[ASGIApp, AbstractAsyncContextManager[None]]:
    """构建 /api/v1/mcp 的 ASGI app 与会话管理器 lifespan。

    用法（api 应用 lifespan 内）：
        mcp_app, mcp_lifespan = build_mcp_app(McpAppState(...))
        app.mount("/api/v1/mcp", mcp_app)
        async with mcp_lifespan(): ...
    """
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

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

    @server.tool(name="list_my_spaces", description="列出当前身份可检索的知识空间")
    async def _list_my_spaces() -> list[dict[str, Any]]:
        return await tool_list_my_spaces(ctx)

    @server.tool(name="read_unit", description="深读一条检索命中的知识单元全文（含父块上下文）")
    async def _read_unit(unit_id: str, asset_id: str) -> dict[str, Any]:
        return await tool_read_unit(ctx, unit_id, asset_id)

    # SDK 默认（host=127.0.0.1）开 DNS rebinding 防护，只放行 localhost 的 Host 头，
    # 经局域网 IP / 域名 / 反向代理访问一律 421。该防护针对无鉴权本地服务；本端点每个
    # 请求都须带 Bearer 或 X-API-Key 凭证（_McpAuthASGI 先于 SDK 校验），凭证不走 cookie，
    # 恶意网页拿不到，故关闭
    app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan():
        async with server.session_manager.run():
            yield

    return _McpAuthASGI(app, ctx), lifespan()
