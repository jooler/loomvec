"""映射状态机：workspace / session 懒创建、409 回填、MCP token 续签（假 DB + MockTransport）。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from loomvec.agent.config import AgentRuntimeConfig, GatewayConnection
from loomvec.agent.gateway import instructions as instr
from loomvec.agent.gateway.errors import GatewayError
from loomvec.agent.gateway.service import (
    GatewayService,
    OwnerContext,
    session_stats,
    token_needs_renewal,
    turn_cap_seconds,
)
from loomvec.core.agent_tokens import decode_agent_token
from loomvec.core.config import AgentSettings, Settings
from loomvec.core.db.models import AgentEnvironment, AgentSession

from ._gw import error_response, json_response, make_client

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
OWNER = OwnerContext(
    user_id=str(uuid.uuid4()),
    tenant_id=str(uuid.uuid4()),
    tenant_name="某公司",
    user_display_name="张三",
)


class FakeDb:
    """最小 AsyncSession 替身：仓储 update 只需要 flush；refresh 可注入「别的事务已提交」。"""

    def __init__(self) -> None:
        self.flushes = 0
        self.refreshes = 0
        self.on_refresh = None

    async def flush(self):
        self.flushes += 1

    async def refresh(self, obj, **_kw):
        self.refreshes += 1
        if self.on_refresh:
            self.on_refresh(obj)


def _cfg(**agent_kw) -> AgentRuntimeConfig:
    return AgentRuntimeConfig(settings=Settings(), agent=AgentSettings(**agent_kw))


def _conn(agent="claude", models=None) -> GatewayConnection:
    return GatewayConnection(
        url="http://gw.test",
        api_key="k",
        request_timeout_s=30,
        default_agent=agent,
        models=models if models is not None else {"claude": None, "dsh": "deepseek-v4-flash"},
    )


def _env(workspace_id: str | None = None) -> AgentEnvironment:
    return AgentEnvironment(id=uuid.uuid4(), gateway_workspace_id=workspace_id)


def _row(env: AgentEnvironment, **kw) -> AgentSession:
    base = dict(
        id=uuid.uuid4(),
        env_id=env.id,
        title="新会话",
        project_path="",
        scope_space_ids=[],
        agent="claude",
        gateway_session_id=None,
        mcp_token_exp=None,
    )
    return AgentSession(**{**base, **kw})


def _svc(cfg=None, now=NOW) -> GatewayService:
    return GatewayService(cfg or _cfg(), None, now=lambda: now)


class Gateway:
    """脚本化的网关：记录调用并返回合理响应。"""

    def __init__(self, *, instructions: str | None = None, workspace_conflict: bool = False):
        self.instructions = instructions
        self.workspace_conflict = workspace_conflict
        self.sessions_created = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v1/workspaces" and method == "POST":
            if self.workspace_conflict:
                return error_response(409, "conflict", "占用", {"workspace_id": "ws_existing"})
            return json_response(201, {"id": "ws_new"})
        if path.endswith("/instructions") and method == "GET":
            return json_response(200, {"content": self.instructions})
        if path.endswith("/instructions") and method == "PUT":
            self.instructions = json.loads(request.content)["content"]
            return json_response(200, {"bytes": 1})
        if path.endswith("/sessions") and method == "POST":
            self.sessions_created += 1
            return json_response(201, {"id": f"ses_{self.sessions_created}"})
        if "/sessions/" in path and method == "PATCH":
            return json_response(200, {"id": path.rsplit("/", 1)[-1]})
        raise AssertionError(f"unexpected {method} {path}")


def _calls(rec) -> list[str]:
    return [f"{r.method} {r.url.path}" for r in rec.requests]


# ---- workspace ----


async def test_ensure_workspace_creates_writes_instructions_and_persists():
    gw = Gateway()
    client, rec = make_client(gw)
    env, db = _env(), FakeDb()
    ws = await _svc().ensure_workspace(client, db, env, OWNER)
    assert ws == "ws_new" and env.gateway_workspace_id == "ws_new" and db.flushes == 1
    assert _calls(rec) == ["POST /v1/workspaces", "PUT /v1/workspaces/ws_new/instructions"]
    assert json.loads(rec.requests[0].content) == {"external_ref": str(env.id)}
    assert "某公司" in gw.instructions and "张三" in gw.instructions
    assert gw.instructions.startswith(instr.marker_line())


async def test_ensure_workspace_is_noop_once_bound():
    client, rec = make_client(Gateway())
    env = _env("ws_bound")
    assert await _svc().ensure_workspace(client, FakeDb(), env, OWNER) == "ws_bound"
    assert rec.requests == []


async def test_ensure_workspace_409_backfills_existing_id_and_syncs_instructions():
    gw = Gateway(workspace_conflict=True, instructions=None)
    client, rec = make_client(gw)
    env = _env()
    assert await _svc().ensure_workspace(client, FakeDb(), env, OWNER) == "ws_existing"
    assert env.gateway_workspace_id == "ws_existing"
    # 不是本次新建：先读现有 AGENTS.md（空 → 写入）
    assert _calls(rec) == [
        "POST /v1/workspaces",
        "GET /v1/workspaces/ws_existing/instructions",
        "PUT /v1/workspaces/ws_existing/instructions",
    ]


async def test_ensure_workspace_failure_after_create_leaves_unbound_and_next_call_heals():
    """建好 workspace 但写 AGENTS.md 失败：不落库；下次 409 回填 + 补写。"""
    state = {"fail_put": True, "created": False}

    def handler(request):
        path, method = request.url.path, request.method
        if path == "/v1/workspaces":
            if state["created"]:
                return error_response(409, "conflict", "占用", {"workspace_id": "ws_1"})
            state["created"] = True
            return json_response(201, {"id": "ws_1"})
        if path.endswith("/instructions") and method == "PUT":
            if state["fail_put"]:
                return error_response(500, "internal")
            return json_response(200, {"bytes": 1})
        return json_response(200, {"content": None})

    client, _ = make_client(handler)
    env, db = _env(), FakeDb()
    with pytest.raises(GatewayError):
        await _svc().ensure_workspace(client, db, env, OWNER)
    assert env.gateway_workspace_id is None and db.flushes == 0
    state["fail_put"] = False
    assert await _svc().ensure_workspace(client, db, env, OWNER) == "ws_1"
    assert env.gateway_workspace_id == "ws_1"


async def test_concurrent_first_use_converges_on_one_workspace():
    """两个并发请求（两个事务）同时首次使用同一 env：一个 201，一个 409 回填，结果一致。"""
    lock = asyncio.Lock()
    created = {"id": None}

    async def post_once():
        async with lock:
            return created["id"]

    def handler(request):
        if request.url.path == "/v1/workspaces":
            if created["id"] is None:
                created["id"] = "ws_race"
                return json_response(201, {"id": "ws_race"})
            return error_response(409, "conflict", "占用", {"workspace_id": "ws_race"})
        if request.method == "GET":
            return json_response(200, {"content": None})
        return json_response(200, {"bytes": 1})

    client, _ = make_client(handler)
    env_id = uuid.uuid4()
    e1, e2 = AgentEnvironment(id=env_id), AgentEnvironment(id=env_id)
    svc = _svc()
    r1, r2 = await asyncio.gather(
        svc.ensure_workspace(client, FakeDb(), e1, OWNER),
        svc.ensure_workspace(client, FakeDb(), e2, OWNER),
    )
    assert r1 == r2 == "ws_race"
    assert e1.gateway_workspace_id == e2.gateway_workspace_id == "ws_race"


# ---- AGENTS.md 模板版本 ----


@pytest.mark.parametrize(
    ("existing", "expect_write"),
    [
        (None, True),
        ("", True),
        ("   \n", True),
        (instr.render_instructions(tenant_name="某公司", user_display_name="张三"), False),
        ("<!-- loomvec-template: 000000000000 -->\n旧模板", True),  # 模板升级
        ("我自己写的规则，没有标记", False),  # 用户 / agent 改写过：不动
    ],
)
async def test_sync_instructions_decision(existing, expect_write):
    gw = Gateway(instructions=existing)
    client, rec = make_client(gw)
    wrote = await _svc().sync_instructions(client, "ws_1", OWNER)
    assert wrote is expect_write
    assert ("PUT /v1/workspaces/ws_1/instructions" in _calls(rec)) is expect_write


async def test_legacy_unmodified_workspace_agents_md_is_upgraded():
    from loomvec.agent.runtime.envs import AGENTS_MD_WORKSPACE

    legacy = AGENTS_MD_WORKSPACE.format(tenant_name="某公司", user_display_name="张三")
    gw = Gateway(instructions=legacy)
    client, _ = make_client(gw)
    assert await _svc().sync_instructions(client, "ws_1", OWNER) is True
    assert gw.instructions.startswith(instr.marker_line())
    # 迁移来的但被用户改过的不动
    gw = Gateway(instructions=legacy + "\n- 额外规则")
    client, _ = make_client(gw)
    assert await _svc().sync_instructions(client, "ws_1", OWNER) is False


# ---- session 懒创建 ----


async def test_prepare_session_creates_gateway_session_once():
    gw = Gateway()
    client, rec = make_client(gw)
    env, db = _env("ws_1"), FakeDb()
    scope = [str(uuid.uuid4())]
    row = _row(env, project_path="projects/report", scope_space_ids=scope, title="周报")
    svc = _svc()

    prepared = await svc.prepare_session(client, db, _conn("claude"), env, row, OWNER)
    assert (prepared.workspace_id, prepared.gateway_session_id) == ("ws_1", "ses_1")
    assert prepared.agent == "claude" and prepared.model == "claude"  # 未配置模型回落 agent 名
    assert row.gateway_session_id == "ses_1"
    assert row.mcp_token_exp == NOW + timedelta(seconds=svc._cfg.agent.mcp.token_ttl_s)

    create = next(
        r for r in rec.requests if r.method == "POST" and r.url.path.endswith("/sessions")
    )
    body = json.loads(create.content)
    assert body["agent"] == "claude" and body["cwd"] == "projects/report"
    assert body["title"] == "周报" and "model" not in body and "effort" not in body
    assert body["permission_policy"]["default"] == "reject"
    assert body["metadata"] == {"loomvec_session_id": row.id.hex, "loomvec_env_id": str(env.id)}
    (server,) = body["mcp_servers"]
    assert server["name"] == "loomvec" and server["type"] == "http"
    assert server["url"] == svc._cfg.agent.mcp.url_sandbox
    bearer = server["headers"][0]["value"]
    assert bearer.startswith("Bearer ")
    claims = decode_agent_token(svc._cfg.settings, bearer.removeprefix("Bearer "))
    assert claims.session_id == row.id.hex  # 会话级 token，MCP 引用旁路 / @ 文献键都靠它
    assert claims.env_id == str(env.id) and claims.user_id == OWNER.user_id
    assert list(claims.scope_space_ids) == scope and claims.tenant_id == OWNER.tenant_id

    # 第二次提问：已建会话，token 远未过期 → 不再有任何网关写调用
    n = len(rec.requests)
    again = await svc.prepare_session(client, db, _conn("claude"), env, row, OWNER)
    assert again.gateway_session_id == "ses_1" and len(rec.requests) == n
    assert gw.sessions_created == 1


async def test_prepare_session_body_for_dsh_uses_configured_model_and_effort():
    gw = Gateway()
    client, rec = make_client(gw)
    env = _env("ws_1")
    row = _row(env, agent="dsh")
    cfg = _cfg(model={"name": "deepseek-chat", "reasoning_effort": "low"})
    svc = _svc(cfg)
    await svc.prepare_session(
        client, FakeDb(), _conn("dsh", {"dsh": "deepseek-v4-pro"}), env, row, OWNER
    )
    body = json.loads(next(r for r in rec.requests if r.url.path.endswith("/sessions")).content)
    assert body["agent"] == "dsh" and body["model"] == "deepseek-v4-pro"
    assert body["effort"] == "low" and "cwd" not in body  # 项目目录为空 = 根


async def test_prepare_session_empty_scope_is_signed_into_token_for_fail_closed_mcp():
    client, rec = make_client(Gateway())
    env = _env("ws_1")
    row = _row(env, scope_space_ids=[])
    svc = _svc()
    await svc.prepare_session(client, FakeDb(), _conn(), env, row, OWNER)
    body = json.loads(next(r for r in rec.requests if r.url.path.endswith("/sessions")).content)
    token = body["mcp_servers"][0]["headers"][0]["value"].removeprefix("Bearer ")
    assert decode_agent_token(svc._cfg.settings, token).scope_space_ids == ()


async def test_concurrent_ask_sees_session_created_by_other_transaction():
    """行锁串行化后，后到者 refresh 即看到前者已建好的会话，不重复创建。"""
    gw = Gateway()
    client, rec = make_client(gw)
    env = _env("ws_1")
    row = _row(env)
    db = FakeDb()

    def other_txn_committed(obj):
        obj.gateway_session_id = "ses_other"
        obj.mcp_token_exp = NOW + timedelta(hours=1)

    db.on_refresh = other_txn_committed
    prepared = await _svc().prepare_session(client, db, _conn(), env, row, OWNER)
    assert prepared.gateway_session_id == "ses_other"
    assert gw.sessions_created == 0
    assert all(r.method != "POST" or not r.url.path.endswith("/sessions") for r in rec.requests)


# ---- MCP token 续签 ----


def test_turn_cap_and_renewal_threshold():
    assert turn_cap_seconds(3600) == 1800
    assert turn_cap_seconds(600) == 600  # 不小于 600
    assert turn_cap_seconds(100) == 600
    ttl = 3600
    assert token_needs_renewal(None, now=NOW, token_ttl_s=ttl)
    assert token_needs_renewal(NOW + timedelta(seconds=1799), now=NOW, token_ttl_s=ttl)
    assert not token_needs_renewal(NOW + timedelta(seconds=1800), now=NOW, token_ttl_s=ttl)
    assert token_needs_renewal(NOW - timedelta(seconds=1), now=NOW, token_ttl_s=ttl)  # 已过期
    # DB 返回 naive 时间按 UTC 处理
    assert not token_needs_renewal(
        (NOW + timedelta(hours=1)).replace(tzinfo=None), now=NOW, token_ttl_s=ttl
    )


@pytest.mark.parametrize(
    ("exp_delta", "renews"),
    [
        (None, True),  # scope 被改（置 NULL）或从未签发
        (timedelta(seconds=300), True),  # 剩余不足一个回合上限
        (timedelta(seconds=-5), True),  # 已过期
        (timedelta(seconds=3000), False),
    ],
)
async def test_mcp_token_renewal_before_each_ask(exp_delta, renews):
    gw = Gateway()
    client, rec = make_client(gw)
    env = _env("ws_1")
    exp = None if exp_delta is None else NOW + exp_delta
    row = _row(env, gateway_session_id="ses_9", mcp_token_exp=exp, scope_space_ids=["s-1"])
    db = FakeDb()
    svc = _svc()
    await svc.prepare_session(client, db, _conn(), env, row, OWNER)
    patches = [r for r in rec.requests if r.method == "PATCH"]
    if not renews:
        assert patches == [] and row.mcp_token_exp == exp
        return
    (patch,) = patches
    assert patch.url.path == "/v1/sessions/ses_9"
    body = json.loads(patch.content)
    assert set(body) == {"mcp_servers"}
    token = body["mcp_servers"][0]["headers"][0]["value"].removeprefix("Bearer ")
    assert decode_agent_token(svc._cfg.settings, token).scope_space_ids == ("s-1",)
    assert row.mcp_token_exp == NOW + timedelta(seconds=svc._cfg.agent.mcp.token_ttl_s)


async def test_failed_patch_keeps_old_expiry_so_next_ask_retries():
    client, _ = make_client(lambda r: error_response(500, "internal"))
    env = _env("ws_1")
    row = _row(env, gateway_session_id="ses_9", mcp_token_exp=None)
    with pytest.raises(GatewayError):
        await _svc().prepare_session(client, FakeDb(), _conn(), env, row, OWNER)
    assert row.mcp_token_exp is None


async def test_archive_gateway_session_is_best_effort():
    client, rec = make_client(lambda r: error_response(500, "internal"))
    env = _env("ws_1")
    await _svc().archive_gateway_session(client, _row(env, gateway_session_id="ses_1"))
    assert len(rec.requests) == 1
    await _svc().archive_gateway_session(client, _row(env))  # 无网关会话：不发请求
    assert len(rec.requests) == 1


# ---- 会话统计 ----


async def test_session_stats_folds_turn_usage_and_tolerates_dsh_null():
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 30,
        "cache_write_tokens": 10,
        "thought_tokens": 1,
        "total_tokens": 56,
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/messages"):
            return json_response(
                200,
                {
                    "items": [
                        {"role": "user", "turn_id": "t1"},
                        {"role": "assistant", "turn_id": "t1"},
                        {"role": "user", "turn_id": "t2"},
                        {"role": "user", "turn_id": "t3"},
                    ],
                    "next_cursor": None,
                },
            )
        tid = path.rsplit("/", 1)[-1]
        return json_response(200, {"id": tid, "usage": usage if tid != "t3" else None})

    client, _ = make_client(handler)
    stats = await session_stats(client, "ses_1")
    assert stats["turns"] == 3 and stats["steps"] == 2
    assert stats["uncached_input_tokens"] == 20 and stats["cache_read_tokens"] == 60
    assert stats["output_tokens"] == 10 and stats["reasoning_tokens"] == 2
    assert stats["cache_hit_rate"] == round(60 / (60 + 20 + 20), 4)

    empty = await session_stats(client, None)
    assert empty["turns"] == 0 and empty["output_tokens"] == 0
