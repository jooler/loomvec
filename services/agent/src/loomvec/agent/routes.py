"""内部 REST `/internal/agent/*`（仅 services/api 可达，X-Internal-Token 闸门）。

职责：会话元数据 CRUD（PG agent_session）、SSE 提问（orchestrator）、
附件落盘、取消、工作区浏览。身份经转发头（X-Loomvec-User-Id 等）注入。
"""

from __future__ import annotations

import asyncio
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.agent.attachments import AttachmentError, resolve_attachment, store_attachment
from loomvec.agent.config import AgentRuntimeConfig
from loomvec.agent.runtime.manager import RuntimeManager, new_logical_session_id
from loomvec.agent.sessions.orchestrator import PromptContext, PromptOrchestrator
from loomvec.agent.sessions.transcript import aggregate_transcript
from loomvec.core.authz import linked_public_space_ids, visible_space_ids
from loomvec.core.config import Settings
from loomvec.core.db.models import AgentEnvironment, AgentSession, Tenant, User
from loomvec.core.db.repos import AgentEnvironmentRepo, AgentSessionRepo
from loomvec.core.errors import NotFoundError, ValidationError
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


class SessionCreate(BaseModel):
    title: str | None = None
    scope_space_ids: list[uuid.UUID] | None = None
    project_path: str | None = None  # 绑定项目目录（workspace 相对路径，可空）


class SessionUpdate(BaseModel):
    title: str | None = None
    scope_space_ids: list[uuid.UUID] | None = None


def normalize_project_path(cfg: AgentRuntimeConfig, env_id: str, raw: str | None) -> str:
    """项目目录入参校验（P5.5a §6.1）：workspace 相对路径、防穿越、目录可建。

    返回规范化后的 POSIX 相对路径（空串 = workspace 根）；目录不存在则创建。
    """
    if raw is None:
        return ""
    rel = raw.strip().strip("/").replace("\\", "/")
    if rel in ("", "."):
        return ""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ValidationError("项目目录须为 workspace 下的相对路径，且不含 .. 与隐藏段")
    target = cfg.workspace(env_id) / Path(*parts)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValidationError(f"项目目录创建失败：{e}") from e
    return "/".join(parts)


class QuestionIn(BaseModel):
    question: str = Field(min_length=1)
    attachment_ids: list[str] | None = None


async def _assert_member_spaces(
    session: AsyncSession, user_id: uuid.UUID, space_ids: list[uuid.UUID]
) -> None:
    """scope 合法性：成员空间 ∪ 已链接公共空间（语义对齐旧 chat 链路）。"""
    allowed = set(await visible_space_ids(session, user_id=user_id))
    allowed.update(await linked_public_space_ids(session, user_id=user_id))
    unknown = [s for s in space_ids if s not in allowed]
    if unknown:
        raise ValidationError("存在无权访问的空间", space_ids=[str(s) for s in unknown[:5]])


@router.get("/health")
async def health(request: Request) -> dict:
    manager: RuntimeManager = request.app.state.runtime_manager
    return {"ok": True, "runtimes": manager.active_count}


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
    project_path = normalize_project_path(
        cfg, str(env.id), body.project_path if body else None
    )
    row = await AgentSessionRepo(session).create(
        id=uuid.UUID(new_logical_session_id()),
        env_id=env.id,
        created_by_user_id=user_id,
        tenant_id=env.tenant_id,
        title=(body.title if body and body.title else None) or "新会话",
        project_path=project_path,
        scope_space_ids=[str(s) for s in scope] if scope else [],
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
    out["stats"] = await _session_stats(request, env, row)
    return out


async def _session_stats(request: Request, env: AgentEnvironment, row: AgentSession) -> dict:
    """会话级统计（§5.8：usage 折叠到全会话，从 JSONL 事实源聚合）。"""

    cfg: AgentRuntimeConfig = request.app.state.agent_config
    # JSONL 可能很大：解析放线程池，避免阻塞事件循环
    return await asyncio.to_thread(_fold_session_stats, cfg, env, row)


def _fold_session_stats(cfg: AgentRuntimeConfig, env: AgentEnvironment, row: AgentSession) -> dict:
    from loomvec.agent.sessions.usage import UsageFold

    fold = UsageFold()
    turns = 0
    for path in _physical_files(cfg, env, row):
        import json

        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                env_line = json.loads(line)
            except ValueError:
                continue
            data = env_line.get("data") or {}
            if env_line.get("type") == "assistant/message":
                fold.add(data.get("usage"))
            elif env_line.get("type") == "turn/end":
                turns += 1
    stats = fold.to_event()
    stats["turns"] = turns
    return stats


def _physical_files(cfg: AgentRuntimeConfig, env: AgentEnvironment, row: AgentSession):
    from loomvec.agent.sessions import transcript

    return transcript.physical_session_files(
        cfg.sessions_root(str(env.id)), cfg.workspace(str(env.id)), row.id.hex
    )


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
            {"scope_space_ids": [str(s) for s in body.scope_space_ids]}
            if body.scope_space_ids is not None
            else {}
        ),
    )
    return _session_out(row)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID,
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


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: uuid.UUID,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
    request: Request = None,
) -> dict:
    """历史读取：解析 JSONL 事实源（聚合全部物理段）。"""
    env, _ = await _own_env(session, identity)
    row = await AgentSessionRepo(session).get_in_env(session_id, env.id, include_archived=True)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    cfg: AgentRuntimeConfig = request.app.state.agent_config
    items = await asyncio.to_thread(
        aggregate_transcript, cfg.sessions_root(str(env.id)), cfg.workspace(str(env.id)), row.id.hex
    )
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
    """提问（SSE）。DB 会话在端点内显式开关：流期间不悬挂请求级事务。"""
    cfg: AgentRuntimeConfig = request.app.state.agent_config
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

        user_row = await session.get(User, user_id)
        tenant_row = await session.get(Tenant, env.tenant_id) if env.tenant_id else None
        attachments = []
        for att_id in (body.attachment_ids or [])[: cfg.agent.session.attachments.max_per_message]:
            meta = resolve_attachment(cfg, str(env.id), str(row.id), att_id)
            if meta is None:
                raise ValidationError(f"附件不存在：{att_id}")
            attachments.append(meta)

        # 提问侧元数据先行落库（断连也不丢会话列表新鲜度）
        first_question = row.message_count == 0
        await repo.update(
            row,
            message_count=row.message_count + 1,
            last_message_at=datetime.now(UTC),
            **({"title": question[:32]} if first_question and row.title == "新会话" else {}),
        )

        ctx = PromptContext(
            env_id=str(env.id),
            tenant_id=str(env.tenant_id) if env.tenant_id else None,
            tenant_name=tenant_row.name if tenant_row else "LoomVec",
            user_id=identity.user_id,
            user_display_name=(user_row.display_name if user_row else None) or identity.username,
            session_id=row.id.hex,
            session_title=row.title,
            # 会话检索范围随 token 下发（严格遵循用户设置；空 = 未设置 → 不检索）
            scope_space_ids=[str(s) for s in (row.scope_space_ids or [])],
            question=question,
            attachments=attachments,
            project_path=row.project_path or "",
        )
    orchestrator: PromptOrchestrator = request.app.state.orchestrator

    async def _gen():
        answer = ""
        async for frame in orchestrator.stream(ctx):
            if frame.startswith("event: done"):
                import json as _json

                try:
                    data_line = frame.split("\ndata: ", 1)[1].rsplit("\n", 1)[0]
                    answer = _json.loads(data_line).get("answer") or ""
                except (ValueError, IndexError):
                    answer = ""
            yield frame
        # 回答侧元数据收尾（best-effort；事实源在 JSONL，失败不影响流）
        if answer:
            try:
                async with request.app.state.session_factory() as db, db.begin():
                    fresh = await AgentSessionRepo(db).get_in_env(
                        session_id, env.id, include_archived=True
                    )
                    if fresh is not None:
                        await AgentSessionRepo(db).update(
                            fresh,
                            message_count=fresh.message_count + 1,
                            preview=answer[:200],
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
    """停止生成：终止该 env 的 runtime（协议无 session/cancel；进程级取消）。"""
    env, _ = await _own_env(session, identity)
    row = await AgentSessionRepo(session).get_in_env(session_id, env.id)
    if row is None:
        raise NotFoundError(resource="agent_session", id=str(session_id))
    manager: RuntimeManager = request.app.state.runtime_manager
    terminated = await manager.terminate(str(env.id), reason="user_cancel")
    return {"cancelled": terminated is not None}


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
    # 预检大小（multipart 解析出的实际字节数），避免无谓的大文件落盘
    att = cfg.agent.session.attachments
    if file.size is not None and file.size > att.max_file_mb * 1024 * 1024:
        raise ValidationError(f"附件超过 {att.max_file_mb}MB 上限")
    # 分块流式落盘（file.file 为 Starlette spooled file，大文件已在磁盘缓冲）
    try:
        stored = await asyncio.to_thread(
            store_attachment,
            cfg,
            str(env.id),
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
# 工作区浏览（P1 WorkspacePage 数据面；tree/file 均限 env 目录内）
# ---------------------------------------------------------------------------


def _safe_workspace_path(cfg: AgentRuntimeConfig, env_id: str, rel: str) -> Path:
    workspace = cfg.workspace(env_id).resolve()
    target = (workspace / rel).resolve()
    if target != workspace and workspace not in target.parents:
        raise ValidationError("路径越界")
    return target


@router.get("/workspace/tree")
async def workspace_tree(
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, _ = await _own_env(session, identity)
    cfg: AgentRuntimeConfig = request.app.state.agent_config
    root = cfg.workspace(str(env.id)).resolve()
    items: list[dict] = []
    count = 0
    for path in sorted(root.rglob("*")):
        if count >= 500:
            break
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue  # 隐藏目录/文件（.loomvec/.dsh）不出树（含根级隐藏目录本身）
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "path": rel,
                "name": path.name,
                "type": "dir" if path.is_dir() else "file",
                "size": 0 if path.is_dir() else stat.st_size,
            }
        )
        count += 1
    return {"items": items}


@router.get("/workspace/file")
async def workspace_file(
    path: str,
    request: Request,
    identity: InternalIdentity = Depends(_identity),
    session: AsyncSession = Depends(_session_ctx),
) -> dict:
    env, _ = await _own_env(session, identity)
    cfg: AgentRuntimeConfig = request.app.state.agent_config
    target = _safe_workspace_path(cfg, str(env.id), path)
    if not target.is_file():
        raise NotFoundError(resource="workspace_file", id=path)
    size = target.stat().st_size
    content = None
    truncated = False
    read_limit = 256 * 1024  # 只取前 256KB 文本预览，杜绝大文件整读进内存
    if size > 0:
        with target.open("rb") as f:
            raw = f.read(read_limit)
        content = raw.decode("utf-8", errors="replace")
        truncated = size > read_limit
    return {
        "path": path,
        "name": target.name,
        "size": size,
        "content": content,
        "truncated": truncated,
    }
