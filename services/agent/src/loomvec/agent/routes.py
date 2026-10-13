"""内部 REST `/internal/agent/*`（仅 services/api 可达，X-Internal-Token 闸门）。

职责：会话元数据 CRUD（PG agent_session）、SSE 提问、附件、取消、工作区浏览。
对话 / 文件 / 历史经 acp-gateway（gateway/ 子包）；本模块保留业务适配：
身份、会话元数据、@ 文献校验、检索范围（scope）与 MCP token。
身份经转发头（X-Loomvec-User-Id 等）注入。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.agent.attachments import AttachmentError, resolve_attachment, store_attachment
from loomvec.agent.config import AgentRuntimeConfig, load_gateway_connection
from loomvec.agent.gateway.errors import (
    GatewayConfigError,
    GatewayError,
    GatewayPathError,
    GatewayTransportError,
    to_domain_error,
)
from loomvec.agent.gateway.files import (
    FILE_PREVIEW_BYTES,
    TREE_DEPTH,
    flatten_tree,
    is_hidden,
    prefix_node,
)
from loomvec.agent.gateway.history import fold_history
from loomvec.agent.gateway.orchestrator import AskContext, GatewayOrchestrator, Outcome
from loomvec.agent.gateway.service import (
    GatewayService,
    OwnerContext,
    new_session_id,
    session_stats,
)
from loomvec.agent.sessions.transcript import aggregate_transcript
from loomvec.core.authz import (
    SpaceRole,
    decide_asset_visibility,
    linked_public_space_ids,
    visible_space_ids,
)
from loomvec.core.config import Settings
from loomvec.core.db.models import (
    AgentEnvironment,
    AgentProject,
    AgentSession,
    Asset,
    Space,
    Tenant,
    User,
)
from loomvec.core.db.repos import (
    AgentEnvironmentRepo,
    AgentProjectRepo,
    AgentSessionRepo,
    SpaceMemberRepo,
)
from loomvec.core.errors import NotFoundError, UpstreamUnavailableError, ValidationError
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.routes")

router = APIRouter(prefix="/internal/agent", tags=["agent-internal"])


@dataclass(frozen=True)
class InternalIdentity:
    user_id: str
    username: str
    tenant_id: str | None


def _identity(
    request: Request,
    x_internal_token: str = Header(default=""),
    x_loomvec_user_id: str = Header(default=""),
    x_loomvec_username: str = Header(default=""),
    x_loomvec_tenant_id: str = Header(default=""),
) -> InternalIdentity:
    settings: Settings = request.app.state.settings
    # 常数时间比较，避免逐字节短路泄露令牌前缀
    if not hmac.compare_digest(x_internal_token, settings.agent_service.internal_token):
        raise ValidationError("内部调用凭证无效")
    if not x_loomvec_user_id:
        raise ValidationError("缺少转发身份头")
    return InternalIdentity(
        user_id=x_loomvec_user_id,
        username=x_loomvec_username or "user",
        tenant_id=x_loomvec_tenant_id or None,
    )


def _db(request: Request) -> AsyncSession:
    return request.app.state.session_factory()


async def _session_ctx(request: Request):
    """请求级 DB 会话上下文（手动事务）。"""
    session = _db(request)
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# 会话元数据（补齐 dsh 缺失的列表/重命名/删除语义）
# ---------------------------------------------------------------------------


def _session_out(row: AgentSession) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "env_id": str(row.env_id),
        "title": row.title,
        "project_path": row.project_path or "",
        "scope_space_ids": [str(s) for s in row.scope_space_ids or []],
        "created_at": row.created_at,
        "last_message_at": row.last_message_at,
        "message_count": row.message_count,
        "preview": row.preview,
        "archived_at": row.archived_at,
        "agent": row.agent,
    }


async def _own_env(
    session: AsyncSession, identity: InternalIdentity
) -> tuple[AgentEnvironment, uuid.UUID]:
    """当前用户的默认工作环境（惰性创建）+ 用户 uuid。"""
    user_id = uuid.UUID(identity.user_id)
    display = await session.get(User, user_id)
    tenant_id = None
    tenant_name = "LoomVec"
    if display is not None and display.tenant_id is not None:
        tenant_id = display.tenant_id
        tenant_row = await session.get(Tenant, tenant_id)
        tenant_name = tenant_row.name if tenant_row else tenant_name
    env = await AgentEnvironmentRepo(session).get_or_create_default(
        user_id,
        tenant_id,
        title=f"{display.display_name or identity.username}的工作环境"
        if display
        else f"{identity.username}的工作环境",
    )
    return env, user_id


async def _owner(
    session: AsyncSession, identity: InternalIdentity, env: AgentEnvironment
) -> OwnerContext:
    """token 签发 / AGENTS.md 渲染用的身份信息（`_own_env` 刚取过，多为 identity map 命中）。"""
    user = await session.get(User, uuid.UUID(identity.user_id))
    tenant = await session.get(Tenant, env.tenant_id) if env.tenant_id else None
    return OwnerContext(
        user_id=identity.user_id,
        tenant_id=str(env.tenant_id) if env.tenant_id else None,
        tenant_name=tenant.name if tenant else "LoomVec",
        user_display_name=(user.display_name if user else None) or identity.username,
    )


@contextlib.contextmanager
def _gateway_errors() -> Iterator[None]:
    """网关异常 → 领域异常（HTTP 路由层统一出口；SSE 内部另有出帧逻辑）。"""
    try:
        yield
    except (GatewayError, GatewayTransportError) as e:
        raise to_domain_error(e) from e
    except GatewayPathError as e:
        raise ValidationError("路径越界") from e


def _gateway(request: Request) -> GatewayService:
    return request.app.state.gateway


async def _workspace(
    request: Request, session: AsyncSession, identity: InternalIdentity, env: AgentEnvironment
):
    """(网关客户端, 生效配置, workspace id)：首次使用时创建 workspace 并写 AGENTS.md。"""
    svc = _gateway(request)
    client, conn = await svc.connect(session)
    owner = await _owner(session, identity, env)
    with _gateway_errors():
        ws_id = await svc.ensure_workspace(client, session, env, owner)
    return client, conn, ws_id


class SessionCreate(BaseModel):
    title: str | None = None
    scope_space_ids: list[uuid.UUID] | None = None
    project_path: str | None = None  # 绑定项目目录（workspace 相对路径，可空）


class SessionUpdate(BaseModel):
    title: str | None = None
    scope_space_ids: list[uuid.UUID] | None = None


def normalize_project_path(raw: str | None) -> str:
    """项目目录入参校验（P5.5a §6.1）：workspace 相对路径、防穿越。

    返回规范化后的 POSIX 相对路径（空串 = workspace 根）。目录由调用方经网关 `dirs` 创建。
    """
    if raw is None:
        return ""
    rel = raw.strip().strip("/").replace("\\", "/")
    if rel in ("", "."):
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ValidationError("项目目录须为 workspace 下的相对路径，且不含 .. 与隐藏段")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in rel):
        raise ValidationError("项目目录不能包含控制字符")
    return "/".join(parts)


MAX_MENTIONS = 10  # 单条提问 @ 文献数上限


class QuestionIn(BaseModel):
    question: str = Field(min_length=1)
    attachment_ids: list[str] | None = None
    # 本条提问 @ 的文献（asset id）：检索只在这些文献内进行
    mention_asset_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_MENTIONS)


# ---------------------------------------------------------------------------
# 侧栏项目（P5.6）：workspace 根下一级目录的「已打开」登记
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str  # 目录名（workspace 根下一级；已存在则直接打开，否则创建）


def _project_out(row: AgentProject) -> dict[str, Any]:
    return {"id": str(row.id), "path": row.path, "created_at": row.created_at}


def normalize_project_name(raw: str) -> str:
    """项目名校验：workspace 根下一级目录名（禁路径分隔/穿越/隐藏段）。"""
    name = raw.strip().strip("/").replace("\\", "/")
    if "/" in name or name in ("", ".", "..") or name.startswith("."):
        raise ValidationError("项目名须为根目录下的单级目录名，且不以 . 开头")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        # 含 \0 时 exists() 静默返回 False、mkdir 抛 ValueError，必须在此拦截
        raise ValidationError("项目名不能包含控制字符")
    if len(name) > 255:
        raise ValidationError("项目名过长（≤255 字符）")
    return name


@router.get("/projects")
async def list_projects(
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, _ = await _own_env(session, identity)
    rows = await AgentProjectRepo(session).list_visible(env.id)
    return {"items": [_project_out(r) for r in rows]}


@router.post("/projects", status_code=201)
async def open_project(
    body: ProjectCreate,
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    """打开/新建项目：目录已存在则直接打开，否则在工作区根创建后打开。"""
    env, user_id = await _own_env(session, identity)
    name = normalize_project_name(body.name)
    client, _, ws_id = await _workspace(request, session, identity, env)
    with _gateway_errors():
        try:
            await client.mkdir(ws_id, name)  # mkdir -p：已存在的目录直接打开
        except GatewayError as e:
            if e.status == 409:  # 路径中有一段是文件
                raise ValidationError(f"工作区已存在同名文件：{name}") from e
            raise

    repo = AgentProjectRepo(session)
    row = await repo.find_by_path(env.id, name)
    if row is None:
        try:
            # 并发打开同一目录：savepoint 内落库，撞唯一约束后回读复用同一行
            async with session.begin_nested():
                row = await repo.create(
                    env_id=env.id, created_by_user_id=user_id, tenant_id=env.tenant_id, path=name
                )
        except IntegrityError:
            row = await repo.find_by_path(env.id, name)
            if row is None:
                raise
    elif row.removed_at is not None:
        row = await repo.update(row, removed_at=None)  # 重新打开此前移除的项目
    return _project_out(row)


@router.delete("/projects/{project_id}", status_code=204)
async def remove_project(
    project_id: uuid.UUID,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> None:
    """移除项目（软删）：文件与会话保留，仅从侧栏列表消失，可重新添加。"""
    env, _ = await _own_env(session, identity)
    repo = AgentProjectRepo(session)
    row = await repo.get(project_id)
    if row is None or row.env_id != env.id:
        raise NotFoundError(resource="agent_project", id=str(project_id))
    if row.removed_at is None:
        await repo.update(row, removed_at=datetime.now(UTC))


async def _assert_member_spaces(
    session: AsyncSession, user_id: uuid.UUID, space_ids: list[uuid.UUID]
) -> None:
    """scope 合法性：成员空间 ∪ 已链接公共空间（语义对齐旧 chat 链路）。"""
    allowed = set(await visible_space_ids(session, user_id=user_id))
    allowed.update(await linked_public_space_ids(session, user_id=user_id))
    unknown = [s for s in space_ids if s not in allowed]
    if unknown:
        raise ValidationError("存在无权访问的空间", space_ids=[str(s) for s in unknown[:5]])


async def _resolve_mentions(
    session: AsyncSession,
    user_id: uuid.UUID,
    scope_space_ids: list[str] | list[uuid.UUID],
    asset_ids: list[uuid.UUID],
) -> list[dict[str, str]]:
    """@ 文献校验：存在、未删除、在会话检索范围内，且对当前用户可见。

    返回 [{asset_id, name}]（保持传入顺序、去重）；任一不满足整体拒绝，
    避免静默丢弃导致检索范围与用户预期不一致。
    """
    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        return []
    allowed = set(await visible_space_ids(session, user_id=user_id))
    allowed.update(await linked_public_space_ids(session, user_id=user_id))
    # agent_session.scope_space_ids 是 JSON 列，读出来是字符串
    in_scope = [s for s in (uuid.UUID(str(x)) for x in scope_space_ids) if s in allowed]
    roles = {m.space_id: m.role for m in await SpaceMemberRepo(session).list_for_user(user_id)}
    rows = (
        await session.execute(
            select(Asset, Space.review_required)
            .join(Space, Space.id == Asset.space_id)
            .where(Asset.id.in_(ids), Asset.deleted_at.is_(None), Asset.space_id.in_(in_scope))
        )
    ).all()
    visible = {
        a.id: a
        for a, review_required in rows
        if decide_asset_visibility(
            review_required=review_required,
            review_status=a.review_status,
            role=roles.get(a.space_id, SpaceRole.VIEWER),
        )
    }
    missing = [str(i) for i in ids if i not in visible]
    if missing:
        raise ValidationError("所 @ 的文献不存在、不在检索范围内或无权访问", asset_ids=missing[:5])
    return [{"asset_id": str(i), "name": visible[i].name} for i in ids]


@router.get("/health")
async def health(request: Request) -> dict:
    """网关可用性：已配置 API Key 且 `GET /healthz` 通。`runtimes` 网关侧不可得，恒为 0。"""
    cfg: AgentRuntimeConfig = request.app.state.agent_config
    async with request.app.state.session_factory() as db:
        conn = await load_gateway_connection(db, cfg.agent)
    if not conn.api_key:
        raise GatewayConfigError("未配置 ACP 网关 API Key，请在管理后台「设置 → agent」填写")
    if not await _gateway(request).client_for(conn).healthz():
        raise UpstreamUnavailableError("acp-gateway", "无法连接 ACP 网关")
    return {"ok": True, "runtimes": 0}


@router.get("/sessions")
async def list_sessions(
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, _ = await _own_env(session, identity)
    rows = await AgentSessionRepo(session).list_for_env(env.id)
    return {"items": [_session_out(r) for r in rows], "total": len(rows)}


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request,
    body: SessionCreate | None = None,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, user_id = await _own_env(session, identity)
    scope = body.scope_space_ids if body else None
    if scope:
        await _assert_member_spaces(session, user_id, scope)
    cfg: AgentRuntimeConfig = request.app.state.agent_config
    project_path = normalize_project_path(body.project_path if body else None)
    if project_path:
        client, _, ws_id = await _workspace(request, session, identity, env)
        with _gateway_errors():
            await client.mkdir(ws_id, project_path)  # 项目目录不存在则创建
    conn = await load_gateway_connection(session, cfg.agent)
    row = await AgentSessionRepo(session).create(
        id=new_session_id(),
        env_id=env.id,
        created_by_user_id=user_id,
        tenant_id=env.tenant_id,
        title=(body.title if body and body.title else None) or "新会话",
        project_path=project_path,
        scope_space_ids=[str(s) for s in scope] if scope else [],
        agent=conn.default_agent,  # 新会话用当前生效的 default_agent；已有会话不受后续切换影响
    )
    return _session_out(row)


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: uuid.UUID,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
    request: Request = None,
) -> dict:
    env, _ = await _own_env(session, identity)
    row = await AgentSessionRepo(session).get_in_env(session_id, env.id, include_archived=True)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    out = _session_out(row)
    out["stats"] = await _session_stats(request, session, row)
    return out


async def _session_stats(request: Request, db: AsyncSession, row: AgentSession) -> dict:
    """会话级统计（§5.8）：网关各回合 usage 聚合。尽力而为：网关不可用返回空对象，不拖垮详情。"""
    try:
        client, _ = await _gateway(request).connect(db)
        with _gateway_errors():
            return await session_stats(client, row.gateway_session_id)
    except (GatewayConfigError, UpstreamUnavailableError, NotFoundError) as e:
        logger.warning("session_stats_unavailable", session_id=row.id.hex, error=str(e))
        return {}


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, user_id = await _own_env(session, identity)
    repo = AgentSessionRepo(session)
    row = await repo.get_in_env(session_id, env.id, include_archived=True)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    if body.scope_space_ids is not None:
        await _assert_member_spaces(session, user_id, body.scope_space_ids)
    await repo.update(
        row,
        **({"title": body.title} if body.title is not None else {}),
        **(
            # scope 随 MCP token 下发：改范围后须在下次提问前重签并 PATCH 到网关会话
            {"scope_space_ids": [str(s) for s in body.scope_space_ids], "mcp_token_exp": None}
            if body.scope_space_ids is not None
            else {}
        ),
    )
    return _session_out(row)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID,
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> None:
    """软删：archived_at 置位（物理清理由保留清理任务执行，§5.6）。"""
    env, _ = await _own_env(session, identity)
    repo = AgentSessionRepo(session)
    row = await repo.get_in_env(session_id, env.id, include_archived=True)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    await repo.update(row, archived_at=datetime.now(UTC))
    if row.gateway_session_id:
        # 网关侧同步归档（释放 agent 会话；历史仍可读）。尽力而为：网关不可用不影响本地删除
        try:
            client, _ = await _gateway(request).connect(session)
            await _gateway(request).archive_gateway_session(client, row)
        except GatewayConfigError:
            logger.warning("gateway_archive_skipped_no_key", session_id=row.id.hex)


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: uuid.UUID,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
    request: Request = None,
) -> dict:
    """历史读取：网关消息视图折成「一问一答」。

    尚未迁移 / 尚未在网关建会话的存量会话（`gateway_session_id` 为空）回落读旧 dsh JSONL，
    迁移脚本导入后读网关。
    """
    env, _ = await _own_env(session, identity)
    row = await AgentSessionRepo(session).get_in_env(session_id, env.id, include_archived=True)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    if not row.gateway_session_id:
        cfg: AgentRuntimeConfig = request.app.state.agent_config
        items = await asyncio.to_thread(
            aggregate_transcript,
            cfg.sessions_root(str(env.id)),
            cfg.workspace(str(env.id)),
            row.id.hex,
        )
        return {"items": items, "total": len(items)}
    client, _ = await _gateway(request).connect(session)
    with _gateway_errors():
        messages = await client.all_messages(row.gateway_session_id)
    items = fold_history(messages)
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 提问（SSE）/ 取消 / 附件
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/messages")
async def ask(
    session_id: uuid.UUID,
    body: QuestionIn,
    request: Request,
    identity: InternalIdentity = Depends(_identity),
) -> StreamingResponse:
    """提问（SSE）。DB 会话在端点内显式开关：流期间不悬挂请求级事务。

    事务内完成全部「落库 + 网关准备」（workspace / session 懒创建、MCP token 续签）：
    这些失败直接是 HTTP 错误（尚未开始流）；流本身的失败以 error + done 帧收尾。
    """
    cfg: AgentRuntimeConfig = request.app.state.agent_config
    svc = _gateway(request)
    question = body.question.strip()[: cfg.agent.session.max_question_chars]
    if not question:
        raise ValidationError("问题不能为空")

    async with request.app.state.session_factory() as session, session.begin():
        env, user_id = await _own_env(session, identity)
        repo = AgentSessionRepo(session)
        row = await repo.get_in_env(session_id, env.id)
        if row is None:
            raise NotFoundError(resource="agent_session", id=str(session_id))
        if row.message_count >= cfg.agent.session.session_max_messages:
            raise ValidationError("会话消息数已达上限，请新建会话")

        mentions = await _resolve_mentions(
            session, user_id, list(row.scope_space_ids or []), body.mention_asset_ids or []
        )
        owner = await _owner(session, identity, env)
        client, conn = await svc.connect(session)
        with _gateway_errors():
            ws_id = await svc.ensure_workspace(client, session, env, owner)
            attachments = []
            att_ids = (body.attachment_ids or [])[: cfg.agent.session.attachments.max_per_message]
            for att_id in att_ids:
                meta = await resolve_attachment(client, ws_id, str(row.id), att_id)
                if meta is None:
                    raise ValidationError(f"附件不存在：{att_id}")
                attachments.append(meta)

        # 提问侧元数据先行落库（断连也不丢会话列表新鲜度）；同时持有会话行锁，
        # 串行化同一会话的并发提问（网关 session 懒创建不会重复）
        first_question = row.message_count == 0
        await repo.update(
            row,
            message_count=row.message_count + 1,
            last_message_at=datetime.now(UTC),
            **({"title": question[:32]} if first_question and row.title == "新会话" else {}),
        )
        with _gateway_errors():
            prepared = await svc.prepare_session(client, session, conn, env, row, owner)

        ctx = AskContext(
            env_id=str(env.id),
            session_id=row.id.hex,
            question=question,
            prepared=prepared,
            attachments=attachments,
            mentions=mentions,
        )
    orchestrator: GatewayOrchestrator = request.app.state.orchestrator
    outcome = Outcome()

    async def _gen():
        async for frame in orchestrator.stream(client, ctx, outcome):
            yield frame
        # 回答侧元数据收尾（best-effort；事实源在网关事件日志，失败不影响流）
        if outcome.answer:
            try:
                async with request.app.state.session_factory() as db, db.begin():
                    fresh = await AgentSessionRepo(db).get_in_env(
                        session_id, env.id, include_archived=True
                    )
                    if fresh is not None:
                        await AgentSessionRepo(db).update(
                            fresh,
                            message_count=fresh.message_count + 1,
                            preview=outcome.answer[:200],
                            last_message_at=datetime.now(UTC),
                        )
            except Exception:
                logger.warning("session_metadata_finalize_failed", session_id=str(session_id))

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/cancel")
async def cancel(
    session_id: uuid.UUID,
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    """停止生成：取消该会话进行中的回合（只取消回合，不杀 agent 进程）。"""
    env, _ = await _own_env(session, identity)
    row = await AgentSessionRepo(session).get_in_env(session_id, env.id)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    if not row.gateway_session_id:
        return {"cancelled": False}  # 还没提过问，没有回合可取消
    client, _ = await _gateway(request).connect(session)
    with _gateway_errors():
        cancelled = await client.cancel_turn(row.gateway_session_id)
    return {"cancelled": cancelled}


@router.post("/sessions/{session_id}/attachments")
async def upload_attachment(
    session_id: uuid.UUID,
    file: UploadFile,
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    cfg: AgentRuntimeConfig = request.app.state.agent_config
    env, _ = await _own_env(session, identity)
    row = await AgentSessionRepo(session).get_in_env(session_id, env.id)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    # 预检大小（multipart 解析出的实际字节数），避免无谓的上传
    att = cfg.agent.session.attachments
    if file.size is not None and file.size > att.max_file_mb * 1024 * 1024:
        raise ValidationError(f"附件超过 {att.max_file_mb}MB 上限")
    client, _, ws_id = await _workspace(request, session, identity, env)
    try:
        with _gateway_errors():
            stored = await store_attachment(
                cfg,
                client,
                ws_id,
                str(row.id),
                file.filename or "attachment",
                file.file,
                file.content_type,
            )
    except AttachmentError as e:
        raise ValidationError(str(e)) from e
    return {
        "attachment_id": stored.attachment_id,
        "path": stored.path,
        "size": stored.size,
        "mime": stored.mime,
    }


# ---------------------------------------------------------------------------
# 工作区浏览（P1 WorkspacePage 数据面；tree/file 均限 workspace 内，路径校验在网关）
# ---------------------------------------------------------------------------


@router.get("/workspace/tree")
async def workspace_tree(
    request: Request,
    prefix: str = "",
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, _ = await _own_env(session, identity)
    # prefix：仅返回该子树（项目文件树视图用）
    prefix = prefix.strip().strip("/")
    if any(part == ".." for part in prefix.split("/")) or any(
        ord(c) < 0x20 or ord(c) == 0x7F for c in prefix
    ):
        raise ValidationError("路径越界")
    if prefix and is_hidden(prefix):
        return {"items": []}  # 隐藏目录（.loomvec 等）不出树
    client, _, ws_id = await _workspace(request, session, identity, env)
    with _gateway_errors():
        try:
            tree = await client.tree(ws_id, prefix, TREE_DEPTH)
        except GatewayError as e:
            if e.status == 404:
                return {"items": []}  # 前缀不存在：空树（与旧行为一致）
            if e.status != 400 or not prefix:
                raise
            # 前缀指向文件：候选集仅含该文件本身
            try:
                st = await client.stat_file(ws_id, prefix)
            except GatewayError:
                return {"items": []}
            size = int(st.get("size") or 0)
            return {"items": [prefix_node(prefix, st.get("type", "file"), size)]}
    return {"items": flatten_tree(prefix, tree.get("entries") or [])}


@router.get("/workspace/file")
async def workspace_file(
    path: str,
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, _ = await _own_env(session, identity)
    client, _, ws_id = await _workspace(request, session, identity, env)
    with _gateway_errors():
        try:
            raw, size, truncated = await client.read_file(
                ws_id,
                path,
                max_bytes=FILE_PREVIEW_BYTES,  # 只取前 256KB 文本预览
            )
        except GatewayError as e:
            if e.status == 404 or (e.status == 400 and e.code != "path_forbidden"):
                raise NotFoundError(resource="workspace_file", id=path) from e  # 目录 / 非常规文件
            raise
    return {
        "path": path,
        "name": PurePosixPath(path).name,
        "size": size,
        "content": raw.decode("utf-8", errors="replace") if size > 0 else None,
        "truncated": truncated,
    }
