"""可选 e2e：services/agent 对着真实 acp-gateway 跑完整提问流（环境不具备自动 skip）。

起的东西（全部是本测试自己的临时资源，结束即销毁，不碰已有数据与容器）：
- 网关：`/home/jyy/acp-gateway`（env `ACPGW_REPO` 可改）用 `node + tsx` 起一个子进程，
  `provider=local`，`agents.claude.local_command` 指向网关仓库的假 ACP agent
  （`test/fake-agent/agent.mjs`，行为由 prompt 里的 JSON 脚本驱动），独立端口与临时存储目录；
- 网关数据库：开发用 PG（`npm run dev:up`，容器 acpgw-dev-postgres，:35440）里临时建的 database；
- loomvec 数据库：开发 PG 实例上临时建的 database + `create_all`；Redis 用开发实例的 db 14；
- agent 服务：进程内 `create_app()` + ASGITransport，等价于 services/api 转发到的内网服务。

前置条件缺任何一项（node >= 22.9、网关仓库与 node_modules、两个 PG、Redis）→ 整组 skip。
`ACPGW_E2E_GW_PG` 覆盖网关侧管理 DSN（默认 postgresql://acpgw:acpgw@127.0.0.1:35440/postgres）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest

pytestmark = pytest.mark.integration

GW_REPO = Path(os.environ.get("ACPGW_REPO", "/home/jyy/acp-gateway"))
GW_ADMIN_DSN = os.environ.get(
    "ACPGW_E2E_GW_PG", "postgresql://acpgw:acpgw@127.0.0.1:35440/postgres"
)
ADMIN_TOKEN = "e2e-admin-token"
INTERNAL_TOKEN = "e2e-internal-token"
REDIS_DB = 14
SEARCH = "mcp__loomvec__search_knowledge"


def _find_node() -> str | None:
    """Node >= 22.9：PATH 里的，或 nvm 安装的最高版本。"""
    candidates = [shutil.which("node"), *sorted(Path.home().glob(".nvm/versions/node/v*/bin/node"))]
    for c in candidates:
        if not c:
            continue
        try:
            out = subprocess.run([str(c), "-v"], capture_output=True, text=True, timeout=10).stdout
            major, minor = (int(x) for x in out.strip().lstrip("v").split(".")[:2])
        except Exception:
            continue
        if (major, minor) >= (22, 9):
            return str(c)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _swap_db(dsn: str, db: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{db}"))


async def _pg_exec(dsn: str, *stmts: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        for s in stmts:
            await conn.execute(s)
    finally:
        await conn.close()


def _prereq_or_skip() -> tuple[str, str]:
    node = _find_node()
    if node is None:
        pytest.skip("需要 Node >= 22.9（PATH 或 ~/.nvm）")
    tsx = GW_REPO / "node_modules" / ".bin" / "tsx"
    if not (GW_REPO / "src" / "index.ts").is_file() or not tsx.is_file():
        pytest.skip(f"网关仓库不可用：{GW_REPO}（需 src/ 与 node_modules）")
    if not (GW_REPO / "test" / "fake-agent" / "agent.mjs").is_file():
        pytest.skip("网关仓库缺少 test/fake-agent/agent.mjs")
    from loomvec.core.config import Settings

    settings = Settings()
    lv_dsn = settings.postgres.url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        asyncio.run(_pg_exec(GW_ADMIN_DSN, "SELECT 1"))
        asyncio.run(_pg_exec(_swap_db(lv_dsn, "postgres"), "SELECT 1"))
    except Exception as e:
        pytest.skip(f"开发 PostgreSQL 不可用：{type(e).__name__}")
    try:
        import redis

        redis.Redis.from_url(settings.redis.url, socket_connect_timeout=2).ping()
    except Exception as e:
        pytest.skip(f"开发 Redis 不可用：{type(e).__name__}")
    return node, str(tsx)


class Stack:
    """网关 + 两个临时数据库 + 租户 / Key。"""

    gw_url: str
    api_key: str
    tenant_id: str
    lv_dsn: str  # asyncpg 形式
    lv_url: str  # sqlalchemy 形式
    redis_url: str
    tmp: Path
    user_id: str
    lv_tenant_id: str


@pytest.fixture(scope="module")
def stack(tmp_path_factory) -> Iterator[Stack]:
    node, tsx = _prereq_or_skip()
    from loomvec.core.config import Settings

    settings = Settings()
    tmp = tmp_path_factory.mktemp("e2e")
    suffix = uuid.uuid4().hex[:8]
    gw_db, lv_db = f"acpgw_e2e_{suffix}", f"loomvec_e2e_{suffix}"
    lv_admin = _swap_db(settings.postgres.url.replace("+asyncpg", ""), "postgres")

    st = Stack()
    st.tmp = tmp
    st.lv_dsn = _swap_db(settings.postgres.url.replace("+asyncpg", ""), lv_db)
    st.lv_url = _swap_db(settings.postgres.url, lv_db)
    st.redis_url = _swap_db(settings.redis.url, str(REDIS_DB))

    asyncio.run(_pg_exec(GW_ADMIN_DSN, f"CREATE DATABASE {gw_db}"))
    asyncio.run(_pg_exec(lv_admin, f"CREATE DATABASE {lv_db}"))

    port = _free_port()
    st.gw_url = f"http://127.0.0.1:{port}"
    (tmp / "gw-store").mkdir()
    (tmp / "gw-config.json").write_text(
        json.dumps(
            {
                "server": {"host": "127.0.0.1", "port": port},
                "storage_root": str(tmp / "gw-store"),
                "runtime": {"provider": "local", "idle_timeout_s": 60},
                "agents": {
                    "claude": {
                        "local_command": [node, str(GW_REPO / "test" / "fake-agent" / "agent.mjs")]
                    }
                },
            }
        )
    )
    env = {
        **os.environ,
        "ACPGW_CONFIG": str(tmp / "gw-config.json"),
        "ACPGW_DATABASE_URL": _swap_db(GW_ADMIN_DSN, gw_db).replace("postgresql://", "postgres://"),
        "ACPGW_ADMIN_TOKEN": ADMIN_TOKEN,
        "ACPGW_ANTHROPIC_API_KEY": "sk-ant-e2e-fake",
        "ACPGW_LOG_LEVEL": "warn",
        "FAKE_AGENT_HOME": str(tmp / "fake-agent-home"),
        "PATH": f"{Path(node).parent}:{os.environ.get('PATH', '')}",
    }
    log = (tmp / "gateway.log").open("w")
    proc = subprocess.Popen(
        [tsx, "src/index.ts"],
        cwd=GW_REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # 独立进程组：只终止我们自己起的这一组
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                pytest.skip("网关启动失败：" + (tmp / "gateway.log").read_text()[-500:])
            try:
                if httpx.get(f"{st.gw_url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.skip("网关 60s 内未就绪")
            time.sleep(0.3)

        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        t = httpx.post(
            f"{st.gw_url}/admin/v1/tenants",
            headers=admin,
            json={
                "name": "loomvec-e2e",
                "default_agent": "claude",
                "settings": {"mcp": {"allowed_hosts": ["127.0.0.1"]}},
            },
        )
        t.raise_for_status()
        st.tenant_id = t.json()["id"]
        k = httpx.post(
            f"{st.gw_url}/admin/v1/tenants/{st.tenant_id}/api-keys",
            headers=admin,
            json={"scopes": ["sessions:write", "sessions:read", "files:read", "files:write"]},
        )
        k.raise_for_status()
        st.api_key = k.json()["api_key"]

        asyncio.run(_seed_loomvec(st))
        yield st
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        log.close()
        asyncio.run(_pg_exec(GW_ADMIN_DSN, f"DROP DATABASE IF EXISTS {gw_db} WITH (FORCE)"))
        asyncio.run(_pg_exec(lv_admin, f"DROP DATABASE IF EXISTS {lv_db} WITH (FORCE)"))
        with contextlib.suppress(Exception):
            import redis

            r = redis.Redis.from_url(st.redis_url)
            for key in r.scan_iter("agent:*"):
                r.delete(key)


async def _seed_loomvec(st: Stack) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    import loomvec.core.db.models  # noqa: F401  注册全部表
    from loomvec.core.db.base import Base
    from loomvec.core.db.models import SystemConfig, Tenant, User

    engine = create_async_engine(st.lv_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        async with async_sessionmaker(engine, expire_on_commit=False)() as s, s.begin():
            tenant = Tenant(name="E2E 公司")
            s.add(tenant)
            await s.flush()
            user = User(username="e2e-user", display_name="测试员", tenant_id=tenant.id)
            s.add(user)
            s.add(SystemConfig(key="agent.gateway.api_key", value=st.api_key, sensitive=True))
            s.add(SystemConfig(key="agent.default_agent", value="claude", sensitive=False))
            await s.flush()
            st.user_id, st.lv_tenant_id = str(user.id), str(tenant.id)
    finally:
        await engine.dispose()


@pytest.fixture()
async def agent(stack: Stack, monkeypatch):
    """进程内的 agent 服务（真实 lifespan）+ 指向它的 HTTP 客户端。"""
    cfg_path = stack.tmp / "loomvec.json"
    cfg_path.write_text(
        json.dumps(
            {
                "agent": {
                    "default_agent": "claude",
                    "gateway": {"url": stack.gw_url, "request_timeout_s": 20},
                    "mcp": {
                        "url": "http://127.0.0.1:38080/api/v1/mcp",
                        "url_sandbox": "http://127.0.0.1:38080/api/v1/mcp",
                        "token_ttl_s": 3600,
                    },
                }
            }
        )
    )
    monkeypatch.setenv("LOOMVEC_APP_CONFIG", str(cfg_path))
    monkeypatch.setenv("LOOMVEC_POSTGRES__URL", stack.lv_url)
    monkeypatch.setenv("LOOMVEC_REDIS__URL", stack.redis_url)
    monkeypatch.setenv("LOOMVEC_AGENT_SERVICE__INTERNAL_TOKEN", INTERNAL_TOKEN)
    from loomvec.core.config import get_app_config, get_settings

    get_settings.cache_clear()
    get_app_config.cache_clear()
    from loomvec.agent.app import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://agent",
            timeout=60,
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Loomvec-User-Id": stack.user_id,
                "X-Loomvec-Username": "e2e-user",
                "X-Loomvec-Tenant-Id": stack.lv_tenant_id,
            },
        ) as client:
            client.app_state = app.state  # type: ignore[attr-defined]
            yield client
    get_settings.cache_clear()
    get_app_config.cache_clear()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = next(line[7:] for line in block.split("\n") if line.startswith("event: "))
        data = next(line[6:] for line in block.split("\n") if line.startswith("data: "))
        out.append((event, json.loads(data)))
    return out


async def ask(client, session_id, question, **extra) -> list[tuple[str, dict]]:
    r = await client.post(
        f"/internal/agent/sessions/{session_id}/messages", json={"question": question, **extra}
    )
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


def gw(stack: Stack, method: str, path: str, **kw) -> httpx.Response:
    return httpx.request(
        method,
        f"{stack.gw_url}{path}",
        headers={"Authorization": f"Bearer {stack.api_key}"},
        timeout=20,
        **kw,
    )


async def new_session(client, **body) -> dict:
    r = await client.post("/internal/agent/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def db_row(stack: Stack, session_id: str) -> dict:
    import asyncpg

    conn = await asyncpg.connect(stack.lv_dsn)
    try:
        row = await conn.fetchrow(
            "SELECT s.agent, s.gateway_session_id, s.mcp_token_exp, e.gateway_workspace_id, "
            "s.message_count, s.preview, s.title FROM agent_session s "
            "JOIN agent_environment e ON e.id = s.env_id WHERE s.id = $1",
            uuid.UUID(session_id),
        )
        return dict(row)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

SEARCH_RESULT = {
    "batch_id": "e2e-b1",
    "ref_items": [
        {
            "n": 1,
            "unit_id": "u1",
            "asset_id": "a1",
            "asset_name": "手册",
            "unit_type": "text",
            "title": None,
            "text_snippet": "片段",
            "locator": {"pages": [3]},
        }
    ],
    "graph_evidence": [],
}


async def test_health_reports_gateway_up(agent):
    r = await agent.get("/internal/agent/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "runtimes": 0}


async def test_full_ask_flow_frames_history_and_lazy_creation(agent, stack):
    sess = await new_session(agent, title=None)
    assert sess["agent"] == "claude"
    row = await db_row(stack, sess["id"])
    assert row["gateway_session_id"] is None  # 首个提问时才懒创建

    script = [
        {"think": "先想一想"},
        {
            "tool": {
                "id": "t-read",
                "title": "读文件",
                "kind": "read",
                "name": "Read",
                "input": {"path": "a.md"},
                "output": "文件内容",
            }
        },
        {
            "tool": {
                "id": "t-search",
                "title": "检索",
                "kind": "other",
                "name": SEARCH,
                "input": {"query": "x"},
                "output": json.dumps(SEARCH_RESULT),
            }
        },
        {"say": "根据手册[1]，答案是42", "chunks": 3},
        {"usage": {"inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 50}},
    ]
    frames = await ask(agent, sess["id"], json.dumps(script, ensure_ascii=False))
    names = [f[0] for f in frames]
    assert names == [
        "meta", "reasoning", "tool_start", "tool_end", "tool_start", "citations", "tool_end",
        "delta", "delta", "delta", "usage", "done",
    ]  # fmt: skip
    meta, done = frames[0][1], frames[-1][1]
    assert meta["session_id"] == uuid.UUID(sess["id"]).hex and meta["env_id"] == sess["env_id"]
    assert frames[2][1] == {"call_id": "t-read", "tool": "Read", "args": '{"path": "a.md"}'}
    assert frames[3][1]["ok"] is True and frames[3][1]["result_summary"] == "文件内容"
    assert frames[5][1]["citations"][0]["unit_id"] == "u1"
    assert done["answer"] == "根据手册[1]，答案是42" and done["finish_reason"] == "end_turn"
    assert done["citations"][0]["asset_name"] == "手册"
    assert done["usage"]["uncached_input_tokens"] == 100 and done["usage"]["output_tokens"] == 20
    assert done["message_id"] == meta["message_id"]

    row = await db_row(stack, sess["id"])
    assert row["gateway_session_id"] and row["gateway_workspace_id"] and row["mcp_token_exp"]
    assert row["preview"].startswith("根据手册") and row["message_count"] == 2

    # 网关侧：workspace 的 external_ref = env id；会话带 loomvec MCP（header 值显示 ***）与审批策略
    ws = gw(stack, "GET", f"/v1/workspaces/{row['gateway_workspace_id']}").json()
    assert ws["external_ref"] == sess["env_id"]
    gs = gw(stack, "GET", f"/v1/sessions/{row['gateway_session_id']}").json()
    assert gs["agent"] == "claude" and gs["permission_policy"]["default"] == "reject"
    assert gs["mcp_servers"][0]["name"] == "loomvec"
    instr = gw(stack, "GET", f"/v1/workspaces/{row['gateway_workspace_id']}/instructions").json()
    assert "测试员" in instr["content"] and "E2E 公司" in instr["content"]

    # 历史：一问一答，工具调用与引用归到 assistant
    r = await agent.get(f"/internal/agent/sessions/{sess['id']}/messages")
    items = r.json()["items"]
    assert [i["role"] for i in items] == ["user", "assistant"]
    assert items[1]["content"] == "根据手册[1]，答案是42"
    assert [t["tool"] for t in items[1]["tool_calls"]] == ["Read", SEARCH]
    assert items[1]["citations"][0]["unit_id"] == "u1"

    # 第二次提问复用同一网关会话（非脚本文本 → 假 agent 回显）
    frames2 = await ask(agent, sess["id"], "你好")
    assert "".join(f[1]["text"] for f in frames2 if f[0] == "delta") == "echo:你好"
    assert frames2[-1][1]["finish_reason"] == "end_turn"
    assert (await db_row(stack, sess["id"]))["gateway_session_id"] == row["gateway_session_id"]
    items = (await agent.get(f"/internal/agent/sessions/{sess['id']}/messages")).json()["items"]
    assert [i["role"] for i in items] == ["user", "assistant", "user", "assistant"]

    detail = (await agent.get(f"/internal/agent/sessions/{sess['id']}")).json()
    assert detail["stats"]["turns"] == 2


async def test_cancel_stops_only_the_turn(agent, stack):
    sess = await new_session(agent)
    script = [{"say": "开始"}, {"sleep": 20000}, {"say": "不该出现"}]
    task = asyncio.create_task(ask(agent, sess["id"], json.dumps(script, ensure_ascii=False)))
    gsid = None
    for _ in range(100):  # 等回合真的跑起来
        await asyncio.sleep(0.2)
        gsid = (await db_row(stack, sess["id"]))["gateway_session_id"]
        if gsid:
            r = gw(stack, "GET", f"/v1/sessions/{gsid}/messages").json()["items"]
            if any(m["role"] == "assistant" for m in r):
                break
    r = await agent.post(f"/internal/agent/sessions/{sess['id']}/cancel")
    assert r.json() == {"cancelled": True}
    frames = await asyncio.wait_for(task, 30)
    assert [f[0] for f in frames][-2:] == ["status", "done"]
    assert (
        frames[-2][1] == {"status": "cancelled"} and frames[-1][1]["finish_reason"] == "cancelled"
    )
    # 进程没被杀：下一个提问立刻可用，且仍在同一网关会话里
    frames2 = await ask(agent, sess["id"], "继续")
    assert frames2[-1][1]["finish_reason"] == "end_turn"
    # 没有进行中回合时取消是空操作
    assert (await agent.post(f"/internal/agent/sessions/{sess['id']}/cancel")).json() == {
        "cancelled": False
    }


async def test_admission_4xx_ends_with_error_and_done(agent, stack):
    """网关受理阶段 4xx（这里：网关会话已被归档 → 409 conflict）→ error + done，流仍是 200。"""
    sess = await new_session(agent)
    await ask(agent, sess["id"], "先建会话")
    gsid = (await db_row(stack, sess["id"]))["gateway_session_id"]
    assert gw(stack, "DELETE", f"/v1/sessions/{gsid}").status_code == 200
    frames = await ask(agent, sess["id"], "再问")
    assert [f[0] for f in frames] == ["error", "done"]
    assert frames[0][1]["code"] == "conflict"
    assert frames[1][1]["finish_reason"] == "error" and frames[1][1]["answer"] == ""


async def test_gateway_busy_conflict_is_reported_in_stream(agent, stack):
    sess = await new_session(agent)
    script = json.dumps([{"say": "忙"}, {"sleep": 3000}], ensure_ascii=False)
    first = asyncio.create_task(ask(agent, sess["id"], script))
    for _ in range(100):
        await asyncio.sleep(0.2)
        if (await db_row(stack, sess["id"]))["gateway_session_id"]:
            await asyncio.sleep(0.8)
            break
    second = await ask(agent, sess["id"], "并发的第二个问题")
    assert [f[0] for f in second] == ["error", "done"]
    assert second[0][1]["code"] == "workspace_busy" and "正在回答" in second[0][1]["message"]
    frames = await asyncio.wait_for(first, 30)
    assert frames[-1][1]["finish_reason"] == "end_turn"


async def test_attachments_projects_and_workspace_browsing(agent, stack):
    # 项目：打开 = 网关 mkdir；会话 cwd = 项目目录
    r = await agent.post("/internal/agent/projects", json={"name": "proj"})
    assert r.status_code == 201 and r.json()["path"] == "proj"
    sess = await new_session(agent, project_path="proj/sub")
    r = await agent.post(
        f"/internal/agent/sessions/{sess['id']}/attachments",
        files={"file": ("说明 文档.md", b"# hello\n", "text/markdown")},
    )
    assert r.status_code == 200, r.text
    att = r.json()
    assert att["path"].startswith(f".loomvec/uploads/{sess['id']}/") and att["size"] == 8
    assert att["attachment_id"].endswith("_说明_文档.md")

    bad = await agent.post(
        f"/internal/agent/sessions/{sess['id']}/attachments",
        files={"file": ("a.exe", b"MZ", "application/x-msdownload")},
    )
    assert bad.status_code == 422

    # 附件文件确实在网关 workspace 里
    row0 = await db_row(stack, sess["id"])
    ws_id = row0["gateway_workspace_id"]
    got = gw(stack, "GET", f"/v1/workspaces/{ws_id}/files/{att['path']}")
    assert got.content == b"# hello\n"

    frames = await ask(agent, sess["id"], "看看附件", attachment_ids=[att["attachment_id"]])
    echoed = "".join(f[1]["text"] for f in frames if f[0] == "delta")
    assert att["path"] in echoed  # 假 agent 回显第一个 text 块 = 附件说明
    gs = gw(stack, "GET", f"/v1/sessions/{(await db_row(stack, sess['id']))['gateway_session_id']}")
    assert gs.json()["cwd"].endswith("proj/sub")

    nope = await agent.post(
        f"/internal/agent/sessions/{sess['id']}/messages",
        json={"question": "x", "attachment_ids": ["0123456789ab_nope.md"]},
    )
    assert nope.status_code == 422 and "附件不存在" in nope.json()["message"]
    traversal = await agent.post(
        f"/internal/agent/sessions/{sess['id']}/messages",
        json={"question": "x", "attachment_ids": ["../../etc/passwd"]},
    )
    assert traversal.status_code == 422

    # 工作区浏览：隐藏目录不出树；prefix 子树含 prefix 条目本身
    gw(stack, "PUT", f"/v1/workspaces/{ws_id}/files/proj/a.txt", content=b"hello")
    gw(stack, "PUT", f"/v1/workspaces/{ws_id}/files/proj/big.txt", content=b"y" * (300 * 1024))
    tree = (await agent.get("/internal/agent/workspace/tree")).json()["items"]
    paths = {i["path"]: i for i in tree}
    assert "proj" in paths and "proj/a.txt" in paths and paths["proj"]["type"] == "dir"
    assert not any(p.startswith(".") or "/." in p for p in paths)
    sub = (await agent.get("/internal/agent/workspace/tree", params={"prefix": "proj"})).json()
    assert sub["items"][0]["path"] == "proj" and {i["path"] for i in sub["items"]} >= {"proj/a.txt"}
    assert (
        await agent.get("/internal/agent/workspace/tree", params={"prefix": ".loomvec"})
    ).json() == {"items": []}
    assert (
        await agent.get("/internal/agent/workspace/tree", params={"prefix": "nope"})
    ).json() == {"items": []}
    assert (
        await agent.get("/internal/agent/workspace/tree", params={"prefix": "../x"})
    ).status_code == 422

    small = (
        await agent.get("/internal/agent/workspace/file", params={"path": "proj/a.txt"})
    ).json()
    assert small == {
        "path": "proj/a.txt", "name": "a.txt", "size": 5, "content": "hello", "truncated": False,
    }  # fmt: skip
    big = (
        await agent.get("/internal/agent/workspace/file", params={"path": "proj/big.txt"})
    ).json()
    assert big["size"] == 300 * 1024 and big["truncated"] is True
    assert len(big["content"]) == 256 * 1024
    assert (
        await agent.get("/internal/agent/workspace/file", params={"path": "nope.txt"})
    ).status_code == 404
    assert (
        await agent.get("/internal/agent/workspace/file", params={"path": "proj"})
    ).status_code == 404
    assert (
        await agent.get("/internal/agent/workspace/file", params={"path": "../x"})
    ).status_code == 422


async def test_scope_change_forces_mcp_token_renewal(agent, stack):
    sess = await new_session(agent)
    await ask(agent, sess["id"], "第一问")
    before = await db_row(stack, sess["id"])
    assert before["mcp_token_exp"] is not None
    r = await agent.patch(f"/internal/agent/sessions/{sess['id']}", json={"scope_space_ids": []})
    assert r.status_code == 200
    assert (await db_row(stack, sess["id"]))["mcp_token_exp"] is None  # 置空 → 下次提问重签
    await asyncio.sleep(1.1)
    await ask(agent, sess["id"], "第二问")
    after = await db_row(stack, sess["id"])
    assert after["mcp_token_exp"] is not None and after["mcp_token_exp"] > before["mcp_token_exp"]
    assert after["gateway_session_id"] == before["gateway_session_id"]


async def test_delete_session_archives_gateway_session(agent, stack):
    sess = await new_session(agent)
    await ask(agent, sess["id"], "hi")
    gsid = (await db_row(stack, sess["id"]))["gateway_session_id"]
    assert (await agent.delete(f"/internal/agent/sessions/{sess['id']}")).status_code == 204
    assert gw(stack, "GET", f"/v1/sessions/{gsid}").json()["status"] == "archived"


async def test_credentials_errors_are_readable_and_not_401(agent, stack):
    import asyncpg

    sess = await new_session(agent)
    conn = await asyncpg.connect(stack.lv_dsn)
    try:
        await conn.execute(
            "UPDATE system_config SET value = '\"acpgw_wrong\"'::jsonb "
            "WHERE key='agent.gateway.api_key'"
        )
        r = await agent.post(
            f"/internal/agent/sessions/{sess['id']}/messages", json={"question": "hi"}
        )
        assert r.status_code == 503 and "凭据无效" in r.json()["message"]
        await conn.execute(
            "UPDATE system_config SET value = '\"\"'::jsonb WHERE key='agent.gateway.api_key'"
        )
        r = await agent.post(
            f"/internal/agent/sessions/{sess['id']}/messages", json={"question": "hi"}
        )
        assert r.status_code == 503 and "未配置" in r.json()["message"]
        assert (await agent.get("/internal/agent/health")).status_code == 503
    finally:
        await conn.execute(
            "UPDATE system_config SET value = $1::jsonb WHERE key='agent.gateway.api_key'",
            json.dumps(stack.api_key),
        )
        await conn.close()


async def test_legacy_migration_script_imports_history_and_files(agent, stack, monkeypatch):
    """L2 迁移脚本对着真实网关：workspace 文件 + 旧 dsh 会话导入，可重入，历史经适配层可读。"""
    import argparse
    import importlib.util

    import asyncpg
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.agent.config import load_agent_config
    from loomvec.agent.sessions.transcript import encode_segment, physical_gen, project_key
    from loomvec.core.db.models import SystemConfig

    from .test_agent_core import _fixture_lines

    storage = stack.tmp / "legacy-storage"
    monkeypatch.setenv("LOOMVEC_AGENT_STORAGE_ROOT", str(storage))
    cfg = load_agent_config()

    sess = await new_session(agent, title="旧会话")
    env_id = sess["env_id"]
    logical = uuid.UUID(sess["id"]).hex
    conn = await asyncpg.connect(stack.lv_dsn)
    try:
        await conn.execute(
            "UPDATE agent_session SET agent='dsh', project_path='projects/p1' WHERE id=$1",
            uuid.UUID(sess["id"]),
        )
    finally:
        await conn.close()

    # 旧环境目录：workspace 文件 + dsh JSONL（路径恒等：project_key 按 workspace 宿主路径算）
    ws = cfg.workspace(env_id)
    (ws / "projects/p1").mkdir(parents=True)
    (ws / "projects/p1/report.md").write_text("# 报告\n", encoding="utf-8")
    (ws / ".loomvec/uploads" / logical).mkdir(parents=True)
    (ws / ".loomvec/uploads" / logical / "a.txt").write_text("附件", encoding="utf-8")
    (ws / ".dsh").mkdir()
    (ws / ".dsh/x").write_text("不迁移")
    sdir = (
        cfg.sessions_root(env_id) / project_key(str(ws)) / encode_segment(physical_gen(logical, 0))
    )
    sdir.mkdir(parents=True)
    with (sdir / "session.v3.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session", "version": 3}) + "\n")
        for i, line in enumerate(_fixture_lines("旧问题", "旧回答 [1]", True), start=1):
            envelope = {"seq": i, "time": f"2026-09-16T00:00:{i:02d}Z", **line}
            f.write(json.dumps(envelope, ensure_ascii=False) + "\n")

    engine = create_async_engine(stack.lv_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s, s.begin():
        s.add(SystemConfig(key="agent.gateway.admin_token", value=ADMIN_TOKEN, sensitive=True))
    await engine.dispose()

    path = Path(__file__).resolve().parents[3] / "scripts" / "migrate_agent_to_gateway.py"
    spec = importlib.util.spec_from_file_location("migrate_agent_to_gateway", path)
    script = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, script)  # dataclass 解析注解需要
    spec.loader.exec_module(script)

    def args(**kw):
        base = dict(
            dry_run=False,
            env_id=[env_id],
            skip_files=False,
            skip_sessions=False,
            include_archived=False,
            overwrite=False,
        )
        return argparse.Namespace(**{**base, **kw})

    # dry-run：不写网关也不写库（env 在本模块内与别的用例共用，workspace 可能已存在，故比较前后）
    before = await db_row(stack, sess["id"])
    ws_before = gw(stack, "GET", "/v1/workspaces", params={"external_ref": env_id}).json()["items"]
    assert await script.migrate(args(dry_run=True)) == 0
    dry = await db_row(stack, sess["id"])
    assert dry["gateway_session_id"] is None
    assert dry["gateway_workspace_id"] == before["gateway_workspace_id"]
    ws_after = gw(stack, "GET", "/v1/workspaces", params={"external_ref": env_id}).json()["items"]
    assert [w["id"] for w in ws_after] == [w["id"] for w in ws_before]

    assert await script.migrate(args()) == 0
    row = await db_row(stack, sess["id"])
    assert row["agent"] == "dsh" and row["gateway_session_id"] and row["gateway_workspace_id"]
    ws_id, gsid = row["gateway_workspace_id"], row["gateway_session_id"]

    # 文件：保留相对路径；隐藏目录（附件目录除外）不迁
    fpath = f"/v1/workspaces/{ws_id}/files"
    assert gw(stack, "GET", f"{fpath}/projects/p1/report.md").text == "# 报告\n"
    up = gw(stack, "GET", f"{fpath}/.loomvec/uploads/{logical}/a.txt")
    assert up.status_code == 200 and up.text == "附件"
    assert gw(stack, "GET", f"{fpath}/.dsh/x").status_code == 404

    # 会话：导入态，cwd / import_key 落到网关；历史经适配层可读（引用也还原）
    gs = gw(stack, "GET", f"/v1/sessions/{gsid}").json()
    assert gs["status"] == "imported" and gs["agent"] == "dsh" and gs["cwd"] == "projects/p1"
    items = (await agent.get(f"/internal/agent/sessions/{sess['id']}/messages")).json()["items"]
    assert [i["role"] for i in items] == ["user", "assistant"]
    assert items[0]["content"] == "旧问题" and items[1]["content"] == "旧回答 [1]"
    assert items[1]["citations"][0]["unit_id"] == "u1"

    # 重跑不覆盖网关侧已改的文件；--overwrite 才还原
    put = gw(stack, "PUT", f"{fpath}/projects/p1/report.md", content="# 网关侧改过\n".encode())
    assert put.status_code == 200
    assert await script.migrate(args()) == 0
    assert gw(stack, "GET", f"{fpath}/projects/p1/report.md").text == "# 网关侧改过\n"
    assert await script.migrate(args(overwrite=True)) == 0
    assert gw(stack, "GET", f"{fpath}/projects/p1/report.md").text == "# 报告\n"

    # 网关侧 import_key 幂等：库里回填丢失（置空）后重跑，拿回同一个网关会话，不重复建
    conn = await asyncpg.connect(stack.lv_dsn)
    try:
        await conn.execute(
            "UPDATE agent_session SET gateway_session_id = NULL WHERE id=$1", uuid.UUID(sess["id"])
        )
    finally:
        await conn.close()
    assert await script.migrate(args()) == 0
    assert (await db_row(stack, sess["id"]))["gateway_session_id"] == gsid
    listing = gw(stack, "GET", f"/v1/workspaces/{ws_id}/sessions?status=imported").json()
    assert sum(1 for x in listing["items"] if x["id"] == gsid) == 1
    assert len(listing["items"]) == 1
