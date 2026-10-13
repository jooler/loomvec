"""loomvec ↔ 网关的映射状态机：workspace / session 懒创建、MCP token 续签。

落库状态（P2 合同 §2.3）：
- `agent_environment.gateway_workspace_id`：首次使用时 `POST /v1/workspaces
  {external_ref: str(env.id)}`；409（并发首次使用 / 上次落库失败）读 `details.workspace_id` 回填；
- `agent_session.gateway_session_id`：首个提问时创建；
- `agent_session.mcp_token_exp`：网关会话上当前 MCP token 的过期时间，空 = 需要（重新）签发
  并 `PATCH mcp_servers`。

并发：同一会话的并发提问在 `ask` 的元数据 UPDATE 上串行（行锁持有到事务提交），
后者进入本模块时 `refresh` 即可看到前者已创建的 `gateway_session_id`，不会重复建会话。
网关调用发生在 DB 事务内（持行锁）：换来「网关建好但落库失败」的窗口最小；
窗口内失败只会留下一个无人引用的网关空会话，下次提问重新创建，不影响正确性。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.agent.approvals import build_permission_policy
from loomvec.agent.config import AgentRuntimeConfig, GatewayConnection, load_gateway_connection
from loomvec.agent.gateway import instructions as instructions_mod
from loomvec.agent.gateway.client import GatewayClient
from loomvec.agent.gateway.errors import GatewayConfigError, GatewayError, GatewayTransportError
from loomvec.agent.gateway.usage import fold_usages
from loomvec.core.agent_tokens import issue_agent_token
from loomvec.core.db.models import AgentEnvironment, AgentSession
from loomvec.core.db.repos import AgentEnvironmentRepo, AgentSessionRepo
from loomvec.core.logging import get_logger

logger = get_logger("loomvec.agent.gateway.service")

MCP_SERVER_NAME = "loomvec"
MIN_TURN_CAP_S = 600
_STATS_CONCURRENCY = 8


def turn_cap_seconds(token_ttl_s: int) -> int:
    """网关回合上限的估计：token TTL 的一半且不小于 600s。剩余有效期低于它就续签。"""
    return max(token_ttl_s // 2, MIN_TURN_CAP_S)


def token_needs_renewal(exp: datetime | None, *, now: datetime, token_ttl_s: int) -> bool:
    if exp is None:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return (exp - now).total_seconds() < turn_cap_seconds(token_ttl_s)


@dataclass(frozen=True)
class OwnerContext:
    """签发 token / 渲染 AGENTS.md 所需的身份信息（routes 组装）。"""

    user_id: str
    tenant_id: str | None
    tenant_name: str
    user_display_name: str


@dataclass(frozen=True)
class PreparedSession:
    workspace_id: str
    gateway_session_id: str
    agent: str
    model: str  # 展示用：配置的模型名，未配置回落 agent 名
    imported: bool = (
        False  # 迁移导入且尚无成功回合（agent_session.replay_pending）：提问需补重放前缀
    )


class GatewayService:
    """进程级单例（app.state.gateway）；持有连接池与配置，逐请求绑定 API Key。"""

    def __init__(
        self,
        cfg: AgentRuntimeConfig,
        http: Any,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cfg = cfg
        self._http = http
        self._now = now

    # ---- 连接 ----

    async def connect(self, db: AsyncSession) -> tuple[GatewayClient, GatewayConnection]:
        conn = await load_gateway_connection(db, self._cfg.agent)
        if not conn.api_key:
            raise GatewayConfigError("未配置 ACP 网关 API Key，请在管理后台「设置 → agent」填写")
        return self.client_for(conn), conn

    def client_for(self, conn: GatewayConnection) -> GatewayClient:
        return GatewayClient(
            self._http, api_key=conn.api_key or "", request_timeout_s=conn.request_timeout_s
        )

    # ---- workspace ----

    async def ensure_workspace(
        self,
        client: GatewayClient,
        db: AsyncSession,
        env: AgentEnvironment,
        owner: OwnerContext,
    ) -> str:
        """env 的网关 workspace id；首次使用时创建并写入 AGENTS.md（幂等，409 回填）。"""
        if env.gateway_workspace_id:
            return env.gateway_workspace_id
        ws_id, created = await client.get_or_create_workspace(str(env.id))
        await self.sync_instructions(client, ws_id, owner, known_empty=created)
        await AgentEnvironmentRepo(db).update(env, gateway_workspace_id=ws_id)
        logger.info("gateway_workspace_bound", env_id=str(env.id), created=created)
        return ws_id

    async def sync_instructions(
        self,
        client: GatewayClient,
        workspace_id: str,
        owner: OwnerContext,
        *,
        known_empty: bool = False,
    ) -> bool:
        """按需把当前模板写入 `files/AGENTS.md`（缺失 / 模板升级时）。返回是否写了。"""
        existing = None if known_empty else await client.get_instructions(workspace_id)
        if not instructions_mod.needs_write(
            existing, tenant_name=owner.tenant_name, user_display_name=owner.user_display_name
        ):
            return False
        await client.put_instructions(
            workspace_id,
            instructions_mod.render_instructions(
                tenant_name=owner.tenant_name, user_display_name=owner.user_display_name
            ),
        )
        return True

    # ---- session ----

    def _mcp_server(self, row: AgentSession, env: AgentEnvironment, owner: OwnerContext):
        """会话级 MCP 配置 + token 过期时间（偏保守：取签发前的时刻 + TTL）。"""
        ttl = self._cfg.agent.mcp.token_ttl_s
        issued_at = self._now()
        token = issue_agent_token(
            self._cfg.settings,
            user_id=owner.user_id,
            tenant_id=owner.tenant_id,
            env_id=str(env.id),
            session_id=row.id.hex,
            scope_space_ids=[str(s) for s in (row.scope_space_ids or [])],  # 空 = MCP fail-closed
            ttl_seconds=ttl,
        )
        server = {
            "type": "http",
            "name": MCP_SERVER_NAME,
            "url": self._cfg.agent.mcp.url_sandbox or self._cfg.agent.mcp.url,
            "headers": [{"name": "Authorization", "value": f"Bearer {token}"}],
        }
        return server, issued_at + timedelta(seconds=ttl)

    async def prepare_session(
        self,
        client: GatewayClient,
        db: AsyncSession,
        conn: GatewayConnection,
        env: AgentEnvironment,
        row: AgentSession,
        owner: OwnerContext,
    ) -> PreparedSession:
        """提问前的全部网关准备：workspace → session（懒创建）→ MCP token（按需续签）。"""
        workspace_id = await self.ensure_workspace(client, db, env, owner)
        # 并发提问：先看别的事务是否已建好（行锁已由调用方的元数据 UPDATE 持有）
        await db.refresh(row)
        repo = AgentSessionRepo(db)
        if not row.gateway_session_id:
            server, exp = self._mcp_server(row, env, owner)
            created = await client.create_session(
                workspace_id, self._session_body(conn, row, server)
            )
            await self.sync_instructions(client, workspace_id, owner)  # 模板升级只在新会话时检查
            await repo.update(row, gateway_session_id=str(created["id"]), mcp_token_exp=exp)
            logger.info(
                "gateway_session_created", session_id=row.id.hex, agent=row.agent, workspace=1
            )
        elif token_needs_renewal(
            row.mcp_token_exp, now=self._now(), token_ttl_s=self._cfg.agent.mcp.token_ttl_s
        ):
            server, exp = self._mcp_server(row, env, owner)
            await client.patch_session(row.gateway_session_id, {"mcp_servers": [server]})
            await repo.update(row, mcp_token_exp=exp)
            logger.info("gateway_mcp_token_renewed", session_id=row.id.hex)
        return PreparedSession(
            workspace_id=workspace_id,
            gateway_session_id=row.gateway_session_id,
            agent=row.agent,
            model=conn.model_for(row.agent) or row.agent,
            imported=bool(row.replay_pending),
        )

    def _session_body(
        self, conn: GatewayConnection, row: AgentSession, mcp_server: dict[str, Any]
    ) -> dict[str, Any]:
        """`POST /v1/workspaces/{id}/sessions` 请求体。

        - `cwd` = 会话绑定项目目录（workspace 相对路径，空 = 根），不再往 prompt 拼目录前缀；
        - 模型：claude 取 `models.claude`（None = agent 默认）；dsh 取 `models.dsh`，
          缺省回落 `agent.model.name`；dsh 另带 `reasoning_effort`；
        - 审批策略对 dsh 不生效（容器内 danger-full-access），仍照传，无副作用。
        """
        body: dict[str, Any] = {
            "agent": row.agent,
            "title": row.title,
            "permission_policy": build_permission_policy(),
            "mcp_servers": [mcp_server],
            "metadata": {"loomvec_session_id": row.id.hex, "loomvec_env_id": str(row.env_id)},
        }
        if row.project_path:
            body["cwd"] = row.project_path
        model = conn.model_for(row.agent)
        if model:
            body["model"] = model
        if row.agent == "dsh" and self._cfg.agent.model.reasoning_effort:
            body["effort"] = self._cfg.agent.model.reasoning_effort
        return body

    async def archive_gateway_session(self, client: GatewayClient, row: AgentSession) -> None:
        """loomvec 侧删除会话时归档网关会话（尽力而为：失败只记日志，历史仍可读）。"""
        if not row.gateway_session_id:
            return
        try:
            await client.archive_session(row.gateway_session_id)
        except (GatewayError, GatewayTransportError) as e:
            logger.warning("gateway_session_archive_failed", session_id=row.id.hex, error=str(e))


async def session_stats(client: GatewayClient, gateway_session_id: str | None) -> dict[str, Any]:
    """会话级用量统计：各回合 usage 折叠（dsh 无用量 → 全零，不报错）+ 回合数。"""
    if not gateway_session_id:
        return {**fold_usages([]), "turns": 0}
    turn_ids: list[str] = []
    for m in await client.all_messages(gateway_session_id):
        tid = m.get("turn_id")
        if m.get("role") == "user" and isinstance(tid, str) and tid not in turn_ids:
            turn_ids.append(tid)
    sem = asyncio.Semaphore(_STATS_CONCURRENCY)

    async def _usage(tid: str) -> dict[str, Any] | None:
        async with sem:
            turn = await client.get_turn(gateway_session_id, tid)
        return turn.get("usage") if isinstance(turn, dict) else None

    usages = await asyncio.gather(*[_usage(t) for t in turn_ids])
    return {**fold_usages(list(usages)), "turns": len(turn_ids)}


def new_session_id() -> uuid.UUID:
    """逻辑会话 id（uuid4；hex 同时是 MCP token 的 agent_session_id 与引用旁路键）。"""
    return uuid.uuid4()
