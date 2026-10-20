"""可选 e2e：services/agent 对着本地模式（mode=local）的网关 + 真实 Claude Code 跑提问流。

和 test_e2e_gateway.py 的区别：网关不是 server 模式的假 agent，而是 `mode=local`——内嵌数据库、
自动生成的本机身份、claude 直接用本机 CLI 的订阅登录（不要 Anthropic key、不经上游代理）；
loomvec 侧**没有**在 system_config 里配 `agent.gateway.api_key`，Key 由 `agent.gateway.state_dir`
下的 `local-api-key` 文件回落提供。会真的调用几次 Claude（几百 token）。

**默认整组 skip**：需显式设 `LOOMVEC_E2E_LOCAL=1`。它会用 .env 指向的开发 PG / Redis（临时建删
database、清 Redis db 14 的 agent:*）、起真实 Claude Code（消耗额度；用 CLI 登录态时，多进程刷新
token 有竞态，可能把正在使用的 claude 登出，所以优先设 CLAUDE_CODE_OAUTH_TOKEN），
并在 ~/.claude 留下会话文件（结束时尽力清理）。不要在指向线上库的 .env 下无意运行。

临时资源（结束即销毁）：网关子进程（独立端口与状态目录）、开发 PG 上临时建的 loomvec database、
Redis db 14。开关打开后，前置条件缺任何一项（node >= 22.9、网关仓库、本机 claude 登录态、
开发 PG / Redis）→ skip；网关起不来 → 失败（不静默跳过配置回归）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from .test_e2e_gateway import (
    GW_REPO,
    INTERNAL_TOKEN,
    REDIS_DB,
    _find_node,
    _free_port,
    _pg_exec,
    _swap_db,
    ask,
    db_row,
    new_session,
)

pytestmark = pytest.mark.integration

CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


class LocalStack:
    gw_url: str
    state_dir: Path
    tmp: Path
    lv_dsn: str
    lv_url: str
    redis_url: str
    user_id: str
    lv_tenant_id: str


def _prereq_or_skip() -> tuple[str, str]:
    if os.environ.get("LOOMVEC_E2E_LOCAL") != "1":
        pytest.skip("本地模式 e2e 连开发 PG / Redis 并起真实 Claude；需显式设 LOOMVEC_E2E_LOCAL=1")
    if not (CREDENTIALS.is_file() or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
        pytest.skip(
            "需要本机 claude 登录态（~/.claude/.credentials.json）或 CLAUDE_CODE_OAUTH_TOKEN"
        )
    node = _find_node()
    if node is None:
        pytest.skip("需要 Node >= 22.9（PATH 或 ~/.nvm）")
    tsx = GW_REPO / "node_modules" / ".bin" / "tsx"
    if not (GW_REPO / "src" / "local" / "bootstrap.ts").is_file() or not tsx.is_file():
        pytest.skip(f"网关仓库不可用或不含本地模式：{GW_REPO}")
    from loomvec.core.config import Settings

    settings = Settings()
    lv_dsn = settings.postgres.url.replace("postgresql+asyncpg://", "postgresql://")
    try:
        asyncio.run(_pg_exec(_swap_db(lv_dsn, "postgres"), "SELECT 1"))
    except Exception as e:
        pytest.skip(f"开发 PostgreSQL 不可用：{type(e).__name__}")
    try:
        import redis

        redis.Redis.from_url(settings.redis.url, socket_connect_timeout=2).ping()
    except Exception as e:
        pytest.skip(f"开发 Redis 不可用：{type(e).__name__}")
    return node, str(tsx)


@pytest.fixture(scope="module")
def stack(tmp_path_factory) -> Iterator[LocalStack]:
    node, tsx = _prereq_or_skip()
    from loomvec.core.config import Settings

    settings = Settings()
    tmp = tmp_path_factory.mktemp("e2e-local")
    lv_db = f"loomvec_e2e_local_{uuid.uuid4().hex[:8]}"
    lv_admin = _swap_db(settings.postgres.url.replace("+asyncpg", ""), "postgres")

    st = LocalStack()
    st.tmp = tmp
    st.state_dir = tmp / "gw-state"
    st.lv_dsn = _swap_db(settings.postgres.url.replace("+asyncpg", ""), lv_db)
    st.lv_url = _swap_db(settings.postgres.url, lv_db)
    st.redis_url = _swap_db(settings.redis.url, str(REDIS_DB))

    port = _free_port()
    st.gw_url = f"http://127.0.0.1:{port}"
    (tmp / "gw-config.json").write_text(
        json.dumps(
            {
                "mode": "local",
                "state_dir": str(st.state_dir),
                "server": {"port": port},
                "runtime": {"idle_timeout_s": 60},
            }
        )
    )
    # 干净的环境：不带 ANTHROPIC_* 等，证明用的是登录态
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANTHROPIC_", "ACPGW_", "CLAUDE_CODE_USE_"))
    }
    env.update(
        ACPGW_CONFIG=str(tmp / "gw-config.json"),
        ACPGW_LOG_LEVEL="warn",
        PATH=f"{Path(node).parent}:{os.environ.get('PATH', '')}",
    )
    log = (tmp / "gateway.log").open("w")
    proc: subprocess.Popen | None = None
    try:
        asyncio.run(_pg_exec(lv_admin, f"CREATE DATABASE {lv_db}"))
        proc = subprocess.Popen(
            [tsx, "src/index.ts"],
            cwd=GW_REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 90
        while True:
            if proc.poll() is not None:
                pytest.fail("网关启动失败：" + (tmp / "gateway.log").read_text()[-500:])
            try:
                if httpx.get(f"{st.gw_url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("网关 90s 内未就绪")
            time.sleep(0.3)
        asyncio.run(_seed_loomvec(st))
        yield st
    finally:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
        log.close()
        with contextlib.suppress(Exception):  # 失败也要继续清 Redis 与 ~/.claude
            asyncio.run(_pg_exec(lv_admin, f"DROP DATABASE IF EXISTS {lv_db} WITH (FORCE)"))
        with contextlib.suppress(Exception):
            import redis

            r = redis.Redis.from_url(st.redis_url)
            for key in r.scan_iter("agent:*"):
                r.delete(key)
        # claude 把会话写在 ~/.claude/projects/<cwd 编码>/，这是测试产生的，清掉
        enc = re.sub(r"[^A-Za-z0-9]", "-", str(tmp))
        projects = Path.home() / ".claude" / "projects"
        for d in projects.glob(f"{enc}*"):
            subprocess.run(["rm", "-rf", "--", str(d)], check=False)


async def _seed_loomvec(st: LocalStack) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import loomvec.core.db.models  # noqa: F401  注册全部表
    from loomvec.core.db.base import Base
    from loomvec.core.db.models import SystemConfig, Tenant, User

    engine = create_async_engine(st.lv_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as s, s.begin():
            tenant = Tenant(name="E2E 本地模式")
            s.add(tenant)
            await s.flush()
            user = User(username="e2e-local", display_name="测试员", tenant_id=tenant.id)
            s.add(user)
            # 刻意不配 agent.gateway.api_key / admin_token：由 state_dir 里的文件回落
            s.add(SystemConfig(key="agent.default_agent", value="claude", sensitive=False))
            await s.flush()
            st.user_id, st.lv_tenant_id = str(user.id), str(tenant.id)
    finally:
        await engine.dispose()


@pytest.fixture()
async def agent(stack: LocalStack, monkeypatch):
    cfg_path = stack.tmp / "loomvec.json"
    cfg_path.write_text(
        json.dumps(
            {
                "agent": {
                    "default_agent": "claude",
                    "gateway": {
                        "url": stack.gw_url,
                        "request_timeout_s": 30,
                        "state_dir": str(stack.state_dir),
                    },
                    "mcp": {
                        # 刻意指向不可达端口：测试不该让真实 claude 去连开发机上的线上 API / MCP
                        "url": "http://127.0.0.1:1/api/v1/mcp",
                        "url_sandbox": "http://127.0.0.1:1/api/v1/mcp",
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
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://agent",
            timeout=120,
            headers={
                "X-Internal-Token": INTERNAL_TOKEN,
                "X-Loomvec-User-Id": stack.user_id,
                "X-Loomvec-Username": "e2e-local",
                "X-Loomvec-Tenant-Id": stack.lv_tenant_id,
            },
        ) as client,
    ):
        yield client
    get_settings.cache_clear()
    get_app_config.cache_clear()


async def test_key_file_fallback_and_plain_question_with_cli_login(agent, stack):
    """没有在 system_config 配 Key：从网关状态目录读到 key，提问用 CLI 登录态答出来。"""
    assert (stack.state_dir / "local-api-key").is_file()
    sess = await new_session(agent, title=None)
    frames = await ask(agent, sess["id"], "只回答 PONG，不要使用工具。")
    kinds = [k for k, _ in frames]
    assert "error" not in kinds, frames
    done = next(d for k, d in frames if k == "done")
    assert "PONG" in done["answer"]
    row = await db_row(stack, sess["id"])
    assert row["agent"] == "claude" and row["gateway_session_id"]


async def test_follow_up_keeps_context_and_policy_rejects_bash(agent, stack):
    sess = await new_session(agent, title=None)
    first = await ask(
        agent, sess["id"], "用 Bash 工具执行 touch e2e-denied.txt，然后告诉我结果。", agent="claude"
    )
    assert "error" not in [k for k, _ in first], first
    # loomvec 的审批策略默认拒绝写 / 执行类工具：工具以失败结束，模型据此回复
    ends = [d for k, d in first if k == "tool_end"]
    assert ends and all(not d["ok"] for d in ends), first
    # 被拒绝就不该真的执行：工作区里没有这个文件
    assert not list(stack.state_dir.rglob("e2e-denied.txt"))
    second = await ask(
        agent, sess["id"], "我上一条让你创建的文件叫什么？只回答文件名，不要使用工具。"
    )
    done = next(d for k, d in second if k == "done")
    assert "e2e-denied" in done["answer"]
