"""ACP 网关管理代理（/api/v1/admin/acp-gateway/*）与 agent 组敏感配置契约。

全部打桩，不连任何真实数据库 / 网关：
- 网关用 httpx.MockTransport 模拟（记录每个请求，断言方法 / 路径 / 鉴权头 / 请求体）；
- 身份、会话经 dependency_overrides 替换（FakeSession 收集审计行与 system_config 行）；
- 覆盖：角色矩阵、审计写入且不含明文、敏感项只写不读、代理白名单、网关错误映射。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from loomvec.api.context import Identity
from loomvec.api.deps import get_identity, get_session, scopes_for_roles
from loomvec.api.errors import register_exception_handlers
from loomvec.api.routes.admin import admin_api_router
from loomvec.api.routes.admin.acp_gateway import get_gateway_admin
from loomvec.api.services import acp_gateway_admin as gw
from loomvec.api.services import admin_settings
from loomvec.core.db.models import AuditLog, SystemConfig
from loomvec.core.errors import ValidationError

ADMIN_TOKEN = "adm-token-0123456789"  # 管理 token（测试桩，绝不应出现在任何响应 / 审计里）
PLAINTEXT_KEY = "acpgw_k1_ABCDEFGHIJKLMNOP"  # 网关签发的 Key 明文（桩）
BASE = "/api/v1/admin/acp-gateway"

TENANT = {
    "id": "ten_01",
    "name": "loomvec",
    "status": "active",
    "default_agent": "claude",
    "quotas": {"daily_tokens": 1000},
    "settings": {"mcp": {"allowed_hosts": ["host.docker.internal"]}},
    "created_at": "2026-10-01T00:00:00.000Z",
}
KEY_META = {
    "id": "key_01",
    "tenant_id": "ten_01",
    "name": "loomvec-prod",
    "prefix": "acpgw_k1",
    "scopes": ["sessions:read", "sessions:write"],
    "permission_policy": {},
    "rate_limit_rps": None,
    "expires_at": None,
    "revoked_at": None,
    "last_used_at": None,
    "created_at": "2026-10-01T00:00:00.000Z",
}


# ---------------------------------------------------------------------------
# 桩
# ---------------------------------------------------------------------------


class FakeSession:
    """收集 add() 的 ORM 行；execute() 只支持 `select(SystemConfig).where(key == :k)`。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.fail_commit = False

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("db down")
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def execute(self, stmt: Any) -> Any:
        wanted = set(stmt.compile().params.values())
        rows = [r for r in self.added if isinstance(r, SystemConfig) and r.key in wanted]

        class _Result:
            def scalar_one_or_none(self_inner) -> Any:
                return rows[-1] if rows else None

            def scalars(self_inner) -> Any:
                return self_inner

            def all(self_inner) -> list[Any]:
                return [r for r in self.added if isinstance(r, SystemConfig)]

        return _Result()

    @property
    def audits(self) -> list[AuditLog]:
        """全部审计行：端点显式审计 + admin_audit 域级兜底（`auto:` 前缀，只含路径参数）。"""
        return [r for r in self.added if isinstance(r, AuditLog)]

    @property
    def explicit(self) -> list[AuditLog]:
        return [a for a in self.audits if a.action.startswith("admin.acp_gateway.")]


class Gateway:
    """记录请求并按路由表回包的网关桩。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}

    def on(self, method: str, path: str, status: int = 200, body: Any = None) -> None:
        self.routes[(method, path)] = lambda _r: httpx.Response(status, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fn = self.routes.get((request.method, request.url.path))
        if fn is None:
            return httpx.Response(
                404, json={"error": {"code": "not_found", "message": "unrouted in test"}}
            )
        return fn(request)

    def json_of(self, i: int = -1) -> Any:
        return json.loads(self.requests[i].content)


def _identity(roles: tuple[str, ...]) -> Identity:
    return Identity(
        user_id=str(uuid.uuid4()),
        username="tester",
        tenant_id=str(uuid.uuid4()),
        roles=roles,
        scopes=scopes_for_roles(roles),
    )


class Env:
    def __init__(self, roles: tuple[str, ...] = ("super_admin",)) -> None:
        self.gateway = Gateway()
        self.session = FakeSession()
        self.app = FastAPI()
        register_exception_handlers(self.app)
        self.app.include_router(admin_api_router)
        self.identity = _identity(roles)
        ov = self.app.dependency_overrides
        ov[get_identity] = lambda: self.identity
        ov[get_session] = lambda: self.session
        self.token = ADMIN_TOKEN
        ov[get_gateway_admin] = self._client

    def _client(self) -> gw.GatewayAdminClient:
        return gw.GatewayAdminClient(
            base_url="http://gw.test",
            admin_token=self.token,
            transport=httpx.MockTransport(self.gateway.handler),
        )

    def as_role(self, *roles: str) -> Env:
        self.identity = _identity(roles)
        return self

    def http(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app), base_url="http://testserver")


@pytest.fixture()
def env() -> Env:
    e = Env()
    g = e.gateway
    g.on("GET", "/admin/v1/tenants", body={"items": [TENANT]})
    g.on("GET", "/admin/v1/tenants/ten_01", body=TENANT)
    g.on("POST", "/admin/v1/tenants", 201, TENANT)
    g.on("PATCH", "/admin/v1/tenants/ten_01", body={**TENANT, "status": "suspended"})
    g.on("GET", "/admin/v1/tenants/ten_01/api-keys", body={"items": [KEY_META]})
    g.on("POST", "/admin/v1/tenants/ten_01/api-keys", 201, {**KEY_META, "api_key": PLAINTEXT_KEY})
    g.on("DELETE", "/admin/v1/api-keys/key_01", body={"revoked": True})
    g.on("GET", "/admin/v1/tenants/ten_01/usage", body={"quotas": {}, "current": {}, "daily": []})
    g.on("POST", "/admin/v1/workspaces/ws_01/stop", body={"stopped": True})
    g.on(
        "GET",
        "/admin/v1/runtimes",
        body={"agents": ["claude", "dsh"], "provider": "docker", "running_turns": 1, "items": []},
    )
    g.on("GET", "/healthz", body={"ok": True})
    return e


def _dumped(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _audit_text(audits: list[AuditLog]) -> str:
    return _dumped(
        [
            {
                "action": a.action,
                "object_id": a.object_id,
                "reason": a.reason,
                "before": a.before_value,
                "after": a.after_value,
            }
            for a in audits
        ]
    )


# ---------------------------------------------------------------------------
# 角色矩阵
# ---------------------------------------------------------------------------

READS = [
    ("GET", f"{BASE}/status"),
    ("GET", f"{BASE}/tenants"),
    ("GET", f"{BASE}/tenants/ten_01"),
    ("GET", f"{BASE}/tenants/ten_01/api-keys"),
    ("GET", f"{BASE}/tenants/ten_01/usage"),
]
MANAGE_WRITES = [
    ("POST", f"{BASE}/tenants", {"name": "t2", "reason": "新建"}),
    ("PATCH", f"{BASE}/tenants/ten_01", {"status": "suspended", "reason": "欠费"}),
    ("POST", f"{BASE}/tenants/ten_01/api-keys", {"scopes": ["sessions:read"], "reason": "签发"}),
    ("POST", f"{BASE}/api-keys/key_01/revoke", {"reason": "泄漏"}),
]
STOP = ("POST", f"{BASE}/workspaces/ws_01/stop", {"reason": "卡死"})


@pytest.mark.parametrize("roles", [("super_admin",), ("operator",), ("auditor",)])
@pytest.mark.parametrize(("method", "path"), READS)
async def test_reads_open_to_all_three_platform_roles(env, roles, method, path):
    async with env.as_role(*roles).http() as c:
        assert (await c.request(method, path)).status_code == 200


@pytest.mark.parametrize(("method", "path"), READS)
async def test_reads_forbidden_for_plain_user(env, method, path):
    async with env.as_role("user").http() as c:
        assert (await c.request(method, path)).status_code == 403


@pytest.mark.parametrize(("method", "path"), READS)
async def test_reads_require_authentication(method, path):
    e = Env()
    del e.app.dependency_overrides[get_identity]  # 真实 get_identity：无凭据 → 401
    from loomvec.api.auth import get_identity_provider
    from loomvec.api.deps import get_redis

    e.app.dependency_overrides[get_redis] = lambda: None
    e.app.dependency_overrides[get_identity_provider] = lambda: None
    async with e.http() as c:
        resp = await c.request(method, path)
    assert resp.status_code == 401
    assert e.gateway.requests == []  # 鉴权失败不触达网关


@pytest.mark.parametrize(("method", "path", "body"), MANAGE_WRITES)
async def test_tenant_key_quota_writes_super_admin_only(env, method, path, body):
    for roles in (("operator",), ("auditor",), ("user",)):
        async with env.as_role(*roles).http() as c:
            assert (await c.request(method, path, json=body)).status_code == 403
    assert env.gateway.requests == []  # 被拒的写操作一次都没转发
    assert env.session.audits == []
    async with env.as_role("super_admin").http() as c:
        assert (await c.request(method, path, json=body)).status_code in (200, 201)


async def test_stop_workspace_allows_operator_not_auditor(env):
    method, path, body = STOP
    async with env.as_role("auditor").http() as c:
        assert (await c.request(method, path, json=body)).status_code == 403
    assert env.gateway.requests == []
    for role in ("operator", "super_admin"):
        async with env.as_role(role).http() as c:
            assert (await c.request(method, path, json=body)).status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), [*MANAGE_WRITES, STOP])
async def test_writes_require_reason(env, method, path, body):
    no_reason = {k: v for k, v in body.items() if k != "reason"}
    async with env.http() as c:
        resp = await c.request(method, path, json=no_reason)
    assert resp.status_code == 422
    assert env.gateway.requests == []


# ---------------------------------------------------------------------------
# 签发 Key：明文只在响应里，审计 / 日志不含
# ---------------------------------------------------------------------------


async def test_issue_key_plaintext_only_in_response_not_in_audit(env):
    async with env.http() as c:
        resp = await c.post(
            f"{BASE}/tenants/ten_01/api-keys",
            json={
                "name": "loomvec-prod",
                "scopes": ["sessions:read", "sessions:write", "sessions:read"],
                "expires_at": "2099-01-01T08:00:00+08:00",
                "reason": "P2 切换签发",
            },
        )
    assert resp.status_code == 201
    assert resp.json()["api_key"] == PLAINTEXT_KEY
    assert resp.headers["cache-control"] == "no-store"

    # 发往网关：管理 token 在头里，scopes 去重，过期时间规整为 UTC Z
    sent = env.gateway.requests[-1]
    assert sent.headers["authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert env.gateway.json_of() == {
        "scopes": ["sessions:read", "sessions:write"],
        "name": "loomvec-prod",
        "expires_at": "2099-01-01T00:00:00Z",
    }

    # 审计：一条 api_key_issue，带理由与非敏感元数据；明文 / 管理 token 都不在其中
    [audit] = env.session.explicit
    assert audit.action == "admin.acp_gateway.api_key_issue"
    assert audit.object_type == "acp_gateway"
    assert audit.object_id == "key_01"
    assert audit.reason == "P2 切换签发"
    assert audit.after_value["prefix"] == "acpgw_k1"
    text = _audit_text(env.session.audits)
    assert PLAINTEXT_KEY not in text and ADMIN_TOKEN not in text
    assert "api_key" not in audit.after_value

    # 列表接口只含前缀
    async with env.http() as c:
        listing = await c.get(f"{BASE}/tenants/ten_01/api-keys")
    assert PLAINTEXT_KEY not in listing.text
    assert listing.json()["items"][0]["prefix"] == "acpgw_k1"


@pytest.mark.parametrize(
    "bad",
    [
        {"scopes": [], "reason": "x"},
        {"scopes": ["root"], "reason": "x"},
        {"scopes": ["sessions:read"], "expires_at": "2000-01-01T00:00:00Z", "reason": "x"},
        {"scopes": ["sessions:read"], "expires_at": "2099-01-01T00:00:00", "reason": "x"},
        {"scopes": ["sessions:read"], "rate_limit_rps": 0, "reason": "x"},
        {"scopes": ["sessions:read"], "unknown": 1, "reason": "x"},
    ],
)
async def test_issue_key_body_validation(env, bad):
    async with env.http() as c:
        resp = await c.post(f"{BASE}/tenants/ten_01/api-keys", json=bad)
    assert resp.status_code == 422
    assert env.gateway.requests == []


async def test_audit_failure_does_not_fail_or_leak_when_gateway_write_succeeded(env):
    """网关写已成功，审计落库失败时仍返回结果（唯一一次的明文不能丢），只告警。"""
    env.session.fail_commit = True
    async with env.http() as c:
        resp = await c.post(
            f"{BASE}/tenants/ten_01/api-keys", json={"scopes": ["files:read"], "reason": "r"}
        )
    assert resp.status_code == 201 and resp.json()["api_key"] == PLAINTEXT_KEY


async def test_revoke_key_audited_with_reason(env):
    async with env.http() as c:
        resp = await c.post(f"{BASE}/api-keys/key_01/revoke", json={"reason": "泄漏"})
    assert resp.status_code == 200 and resp.json() == {"revoked": True}
    assert env.gateway.requests[-1].method == "DELETE"
    [audit] = env.session.explicit
    assert (audit.action, audit.object_id, audit.reason) == (
        "admin.acp_gateway.api_key_revoke",
        "key_01",
        "泄漏",
    )


# ---------------------------------------------------------------------------
# 租户 / 配额
# ---------------------------------------------------------------------------


async def test_patch_tenant_replaces_quotas_and_audits_before_after(env):
    body = {
        "quotas": {"daily_tokens": 5000, "max_workspaces": 20},
        "settings": {"mcp": {"allowed_hosts": ["host.docker.internal:38080"]}},
        "reason": "调整配额与 MCP 白名单",
    }
    async with env.http() as c:
        resp = await c.patch(f"{BASE}/tenants/ten_01", json=body)
    assert resp.status_code == 200
    [get_req, patch_req] = env.gateway.requests
    assert (get_req.method, patch_req.method) == ("GET", "PATCH")
    sent = json.loads(patch_req.content)
    assert sent == {
        "quotas": {"daily_tokens": 5000, "max_workspaces": 20},
        "settings": {
            "mcp": {"allowed_hosts": ["host.docker.internal:38080"], "allow_stdio": False}
        },
    }
    [audit] = env.session.explicit
    assert audit.action == "admin.acp_gateway.tenant_update"
    assert audit.before_value["quotas"] == {"daily_tokens": 1000}  # 修改前快照来自网关
    assert set(audit.before_value) == {"quotas", "settings"}  # 只记被改动的字段
    assert audit.reason == "调整配额与 MCP 白名单"


async def test_patch_tenant_null_quotas_clears_limits(env):
    async with env.http() as c:
        await c.patch(f"{BASE}/tenants/ten_01", json={"quotas": None, "reason": "取消限制"})
    assert json.loads(env.gateway.requests[-1].content) == {"quotas": {}}


@pytest.mark.parametrize(
    "bad",
    [
        {"reason": "什么都不改"},
        {"quotas": {"nope": 1}, "reason": "x"},
        {"quotas": {"daily_tokens": -1}, "reason": "x"},
        {"status": "deleted", "reason": "x"},
        {"default_agent": "gpt", "reason": "x"},
        {"settings": {"mcp": {"allow_stdio": True}}, "reason": "x"},
        {"settings": {"mcp": {"allowed_hosts": ["http://evil.com/x"]}}, "reason": "x"},
    ],
)
async def test_patch_tenant_body_validation(env, bad):
    async with env.http() as c:
        resp = await c.patch(f"{BASE}/tenants/ten_01", json=bad)
    assert resp.status_code == 422
    assert env.gateway.requests == []


async def test_create_tenant(env):
    async with env.http() as c:
        resp = await c.post(
            f"{BASE}/tenants",
            json={
                "name": "loomvec",
                "default_agent": "dsh",
                "quotas": {"max_workspaces": 5},
                "reason": "新建",
            },
        )
    assert resp.status_code == 201
    assert env.gateway.json_of() == {
        "name": "loomvec",
        "default_agent": "dsh",
        "quotas": {"max_workspaces": 5},
    }
    assert env.session.explicit[0].action == "admin.acp_gateway.tenant_create"


async def test_tenants_list_paginates_and_filters(env):
    many = [{**TENANT, "id": f"ten_{i:02d}", "name": f"acme-{i}"} for i in range(5)]
    env.gateway.on("GET", "/admin/v1/tenants", body={"items": [*many, TENANT]})
    async with env.http() as c:
        page = (await c.get(f"{BASE}/tenants", params={"limit": 2, "offset": 2})).json()
        found = (await c.get(f"{BASE}/tenants", params={"q": "LOOM"})).json()
    assert (page["total"], page["limit"], page["offset"], len(page["items"])) == (6, 2, 2, 2)
    assert [t["name"] for t in found["items"]] == ["loomvec"]


async def test_usage_forwards_date_range(env):
    async with env.http() as c:
        resp = await c.get(
            f"{BASE}/tenants/ten_01/usage", params={"from": "2026-10-01", "to": "2026-10-08"}
        )
        bad = await c.get(f"{BASE}/tenants/ten_01/usage", params={"from": "yesterday"})
    assert resp.status_code == 200 and bad.status_code == 422
    assert dict(env.gateway.requests[-1].url.params) == {"from": "2026-10-01", "to": "2026-10-08"}


# ---------------------------------------------------------------------------
# 运行时状态：网关故障降级而不是报错
# ---------------------------------------------------------------------------


async def test_status_merges_health_and_runtimes(env):
    async with env.http() as c:
        data = (await c.get(f"{BASE}/status")).json()
    assert data["configured"] and data["reachable"] and data["healthy"]
    assert data["agents"] == ["claude", "dsh"] and data["provider"] == "docker"
    assert data["error"] is None
    healthz = next(r for r in env.gateway.requests if r.url.path == "/healthz")
    assert "authorization" not in healthz.headers  # 健康探测不带管理 token


async def test_status_degrades_when_gateway_down(env):
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {ADMIN_TOKEN}")

    env.gateway.routes[("GET", "/healthz")] = boom
    async with env.http() as c:
        resp = await c.get(f"{BASE}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reachable"] is False and data["error"]
    assert ADMIN_TOKEN not in resp.text


async def test_status_without_admin_token_still_probes_health(env):
    env.token = ""
    async with env.http() as c:
        data = (await c.get(f"{BASE}/status")).json()
    assert data["configured"] is False and data["healthy"] is True
    assert "agent.gateway.admin_token" in data["error"]
    assert all(r.url.path == "/healthz" for r in env.gateway.requests)


async def test_config_reports_flags_but_no_secrets(env, monkeypatch):
    async def fake_effective(_session, key):
        return {"agent.gateway.admin_token": ADMIN_TOKEN, "agent.gateway.api_key": "  "}.get(key)

    monkeypatch.setattr(admin_settings, "effective_value", fake_effective)
    async with env.as_role("auditor").http() as c:
        resp = await c.get(f"{BASE}/config")
    data = resp.json()
    assert data["admin_token_configured"] is True and data["api_key_configured"] is False
    assert data["required_key_scopes"] == [
        "sessions:read",
        "sessions:write",
        "files:read",
        "files:write",
    ]
    assert data["mcp_sandbox"]["hostname"] and data["url"].startswith("http")
    assert ADMIN_TOKEN not in resp.text


# ---------------------------------------------------------------------------
# 网关错误 → 平台统一错误结构
# ---------------------------------------------------------------------------


def _gw_error(status: int, code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


@pytest.mark.parametrize(
    ("gw_status", "want_status", "want_code"),
    [
        (401, 409, "acp_gateway_auth_failed"),  # 绝不透传 401：会触发前端登出
        (403, 409, "acp_gateway_auth_failed"),
        (404, 404, "not_found"),
        (409, 409, "conflict"),
        (429, 429, "rate_limited"),
        (400, 422, "validation_error"),
        (500, 502, "upstream_unavailable"),
        (503, 502, "upstream_unavailable"),
    ],
)
async def test_gateway_errors_mapped_to_platform_envelope(env, gw_status, want_status, want_code):
    leaky = {"stack": "at secret.ts:1", "token": ADMIN_TOKEN}
    env.gateway.on(
        "GET", "/admin/v1/tenants/ten_01", gw_status, _gw_error(gw_status, "x_code", "boom", leaky)
    )
    async with env.http() as c:
        resp = await c.get(f"{BASE}/tenants/ten_01")
    assert resp.status_code == want_status
    body = resp.json()
    assert body["code"] == want_code
    assert set(body) == {"code", "message", "details"}
    assert body["details"]["gateway_status"] == gw_status
    assert "secret.ts" not in resp.text and ADMIN_TOKEN not in resp.text  # 网关 details 不透传


async def test_gateway_error_message_scrubbed_of_secrets(env):
    msg = f"bad key {PLAINTEXT_KEY} / token {ADMIN_TOKEN}"
    env.gateway.on("GET", "/admin/v1/tenants/ten_01", 404, _gw_error(404, "not_found", msg))
    async with env.http() as c:
        resp = await c.get(f"{BASE}/tenants/ten_01")
    assert resp.status_code == 404
    assert PLAINTEXT_KEY not in resp.text and ADMIN_TOKEN not in resp.text


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("x"), httpx.ReadTimeout("x")], ids=["connect", "timeout"]
)
async def test_gateway_unreachable_maps_to_502(env, exc):
    def boom(_request: httpx.Request) -> httpx.Response:
        raise exc

    env.gateway.routes[("GET", "/admin/v1/tenants")] = boom
    async with env.http() as c:
        resp = await c.get(f"{BASE}/tenants")
    assert resp.status_code == 502 and resp.json()["code"] == "upstream_unavailable"
    assert resp.json()["details"]["upstream"] == "acp-gateway"


async def test_gateway_non_json_success_maps_to_502(env):
    env.gateway.routes[("GET", "/admin/v1/tenants")] = lambda _r: httpx.Response(200, text="<html>")
    async with env.http() as c:
        assert (await c.get(f"{BASE}/tenants")).status_code == 502


async def test_missing_admin_token_gives_actionable_conflict(env):
    env.token = ""
    async with env.http() as c:
        resp = await c.get(f"{BASE}/tenants")
    assert resp.status_code == 409
    assert resp.json()["code"] == "acp_gateway_not_configured"
    assert "agent.gateway.admin_token" in resp.json()["message"]
    assert env.gateway.requests == []


# ---------------------------------------------------------------------------
# 白名单：不是通用转发
# ---------------------------------------------------------------------------


def test_proxy_routes_are_a_closed_set():
    spec = Env().app.openapi()["paths"]
    prefix = "/api/v1/admin/acp-gateway"
    found = {
        (method.upper(), path.removeprefix(prefix))
        for path, item in spec.items()
        if path.startswith(prefix)
        for method in item
    }
    assert found == {
        ("GET", "/config"),
        ("GET", "/status"),
        ("GET", "/tenants"),
        ("POST", "/tenants"),
        ("GET", "/tenants/{tenant_id}"),
        ("PATCH", "/tenants/{tenant_id}"),
        ("GET", "/tenants/{tenant_id}/api-keys"),
        ("POST", "/tenants/{tenant_id}/api-keys"),
        ("GET", "/tenants/{tenant_id}/usage"),
        ("POST", "/api-keys/{key_id}/revoke"),
        ("POST", "/workspaces/{workspace_id}/stop"),
    }


def test_client_operation_whitelist_excludes_migration_and_sessions_import():
    ops = {(o.method, o.path) for o in gw._OPERATIONS.values()}
    assert all(p.startswith("/admin/v1/") for _, p in ops)
    assert not any("import" in p or "audit" in p for _, p in ops)
    assert len(ops) == 10


async def test_client_rejects_unregistered_op_and_path_tricks():
    client = gw.GatewayAdminClient(
        base_url="http://gw.test",
        admin_token=ADMIN_TOKEN,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with pytest.raises(ValueError):
        await client.call("sessions.import")
    for evil in ("..", "a/b", "ten_01/../x", "ws_1:import", "a?b=1", "a#x", "", "x" * 65):
        with pytest.raises(ValidationError):
            await client.call("tenants.get", path_params={"tenant_id": evil})


async def test_route_path_params_cannot_escape_whitelisted_paths(env):
    async with env.http() as c:
        resp = await c.get(f"{BASE}/tenants/ws_1:import")
    assert resp.status_code == 422
    assert env.gateway.requests == []


def test_client_requires_http_url_and_hides_token_in_repr():
    with pytest.raises(gw.GatewayNotConfiguredError):
        gw.GatewayAdminClient(base_url="ftp://gw", admin_token="t")
    c = gw.GatewayAdminClient(base_url="http://gw.test", admin_token=ADMIN_TOKEN)
    assert ADMIN_TOKEN not in repr(c)


# ---------------------------------------------------------------------------
# 「设置 → agent」：敏感项只写不读，经 /admin/settings 写入
# ---------------------------------------------------------------------------

AGENT_KEYS = {
    "agent.default_agent",
    "agent.gateway.models.claude",
    "agent.gateway.models.dsh",
    "agent.gateway.api_key",
    "agent.gateway.admin_token",
}


def test_agent_group_registered():
    reg = {k: d for k, d in admin_settings.SETTING_REGISTRY.items() if d.group == "agent"}
    assert set(reg) == AGENT_KEYS
    assert all(d.admin_only and d.effect == "immediate" for d in reg.values())
    assert {k for k, d in reg.items() if d.sensitive} == {
        "agent.gateway.api_key",
        "agent.gateway.admin_token",
    }
    # 两个敏感项没有文件回落：默认值恒为空
    assert reg["agent.gateway.api_key"].default is None
    assert reg["agent.gateway.admin_token"].default is None
    # 文件默认值取自 AgentSettings
    assert reg["agent.default_agent"].default in ("claude", "dsh")


def test_agent_setting_validators():
    v = admin_settings.SETTING_REGISTRY["agent.default_agent"].validate
    v("claude")
    v("dsh")
    for bad in ("gpt", "", None):
        with pytest.raises(ValidationError):
            v(bad)
    m = admin_settings.SETTING_REGISTRY["agent.gateway.models.claude"].validate
    for ok in (None, "", "claude-sonnet-4-5"):
        m(ok)
    for bad in ("x" * 129, "a\nb", 5):
        with pytest.raises(ValidationError):
            m(bad)


def test_mask_treats_empty_as_unset():
    assert admin_settings.mask(None) is None
    assert admin_settings.mask("") is None  # 写入 "" = 清除，回显「未设置」
    assert admin_settings.mask("sk-1") == "******"


@pytest.mark.parametrize("key", ["agent.gateway.api_key", "agent.gateway.admin_token"])
async def test_sensitive_agent_settings_write_only(env, key):
    secret = "super-secret-value-123"
    async with env.http() as c:
        put = await c.put(f"/api/v1/admin/settings/{key}", json={"value": secret, "reason": "配置"})
        got = await c.get(f"/api/v1/admin/settings/{key}")
        listing = await c.get("/api/v1/admin/settings")
    assert put.status_code == 200 and put.json()["value"] == "******"
    assert got.json()["value"] == "******" and got.json()["sensitive"] is True
    assert secret not in put.text + got.text
    # 已落库的是明文（业务侧读取用），但任何读接口都不回显
    row = next(r for r in env.session.added if isinstance(r, SystemConfig) and r.key == key)
    assert row.value == secret and row.sensitive is True
    assert secret not in listing.text
    # 审计只记键名，不含值
    audit_blob = _audit_text(env.session.audits)
    assert secret not in audit_blob and key in audit_blob


@pytest.mark.parametrize("role", ["operator", "auditor"])
async def test_agent_settings_require_super_admin(env, role):
    async with env.as_role(role).http() as c:
        resp = await c.put("/api/v1/admin/settings/agent.gateway.admin_token", json={"value": "x"})
    assert resp.status_code == 403
    assert not any(isinstance(r, SystemConfig) for r in env.session.added)


async def test_agent_default_agent_rejects_unknown_value(env):
    async with env.http() as c:
        resp = await c.put("/api/v1/admin/settings/agent.default_agent", json={"value": "gpt"})
    assert resp.status_code == 422
