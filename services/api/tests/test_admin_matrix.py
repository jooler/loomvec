"""P4-QA-01 平台角色矩阵测试：super_admin / operator / auditor × 管理 API。

矩阵语义（docs/04 §三）：
- 未认证 → 401；普通用户 → 403；
- super_admin：全部读写（含系统配置 ai 组、平台角色分配）；
- operator：治理/运营读写；ai/sso/extensions 配置组与平台角色分配 403；
- auditor：仅 admin:read；写操作 403；
- 管理 API 域写操作成功后必须有兜底审计留痕（admin_audit）。

角色注入方式：迁移 0004 种子平台角色行 + dev JWT 携带角色
（identity.resolve_user_row 将 DB 角色合并进 Identity.roles；DB 不可用时
JWT claims 兜底——单测内存库走 PG 集成测试，此处验证守卫与契约语义）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from loomvec.api.app import create_app
from loomvec.core.config import Settings


def _new_client():
    settings = Settings(_env_file=None, auth={"dev_mode": True})
    # with 上下文触发 lifespan：管理域依赖（get_session）在守卫 401 前不触库，
    # 但需要 app.state 可用；角色注入经 dev JWT claims（resolve 兜底语义）
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture()
def client():
    with _new_client() as c:
        yield c


def _token(client: TestClient, username: str, roles: list[str]) -> str:
    resp = client.post("/api/v1/auth/dev/token", json={"username": username, "roles": roles})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(roles: list[str], username: str = "matrix-user") -> dict[str, str]:
    with _new_client() as c:
        return {"Authorization": f"Bearer {_token(c, username, roles)}"}


# 守卫矩阵：端点 → 允许角色集合
READ_ENDPOINTS = [
    "/api/v1/admin/system/status",
    "/api/v1/admin/tenants",
    "/api/v1/admin/users",
    "/api/v1/admin/spaces",
    "/api/v1/admin/reviews",
    "/api/v1/admin/pipeline/jobs",
    "/api/v1/admin/settings",
    "/api/v1/admin/audit-logs",
]

WRITE_ENDPOINTS = [
    "/api/v1/admin/tenants",
    "/api/v1/admin/webhooks/subscriptions",
]


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_matrix_unauthenticated_gets_401(client: TestClient, endpoint: str):
    assert client.get(endpoint).status_code == 401


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_matrix_regular_user_gets_403(client: TestClient, endpoint: str):
    headers = _auth(["user"])
    assert client.get(endpoint, headers=headers).status_code == 403


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_matrix_super_admin_can_read(client: TestClient, endpoint: str):
    headers = _auth(["super_admin"])
    resp = client.get(endpoint, headers=headers)
    assert resp.status_code in (200, 409)  # 409 仅当 DB 不可用的聚合降级
    assert resp.status_code != 401 and resp.status_code != 403


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_matrix_operator_can_read(client: TestClient, endpoint: str):
    headers = _auth(["operator"])
    resp = client.get(endpoint, headers=headers)
    assert resp.status_code != 401 and resp.status_code != 403


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_matrix_auditor_can_read(client: TestClient, endpoint: str):
    headers = _auth(["auditor"])
    resp = client.get(endpoint, headers=headers)
    assert resp.status_code != 401 and resp.status_code != 403


@pytest.mark.parametrize("endpoint", WRITE_ENDPOINTS)
def test_matrix_auditor_cannot_write(client: TestClient, endpoint: str):
    headers = _auth(["auditor"])
    resp = client.post(endpoint, headers=headers, json={})
    assert resp.status_code == 403


def test_matrix_super_admin_can_create_tenant(client: TestClient):
    headers = _auth(["super_admin"], username="matrix-super")
    resp = client.post(
        "/api/v1/admin/tenants",
        headers=headers,
        json={"name": "矩阵测试租户", "quota_storage_bytes": 0, "quota_file_count": 0},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "active"


def test_matrix_operator_can_create_tenant_but_not_platform_role(client: TestClient):
    headers = _auth(["operator"], username="matrix-operator")
    resp = client.post("/api/v1/admin/tenants", headers=headers, json={"name": "运营创建租户"})
    assert resp.status_code == 201
    # 平台角色分配仅 super_admin
    resp = client.put(
        f"/api/v1/admin/users/{'0' * 32}/platform-role",
        headers=headers,
        json={"role": "auditor", "reason": "test"},
    )
    assert resp.status_code == 403


def test_matrix_suspension_requires_reason(client: TestClient):
    headers = _auth(["super_admin"], username="matrix-reason")
    resp = client.post("/api/v1/admin/tenants", headers=headers, json={"name": "待停用租户"})
    tenant_id = resp.json()["id"]
    no_reason = client.post(f"/api/v1/admin/tenants/{tenant_id}/suspend", headers=headers, json={})
    assert no_reason.status_code == 422  # reason 必填（pydantic 校验）
    ok = client.post(
        f"/api/v1/admin/tenants/{tenant_id}/suspend",
        headers=headers,
        json={"reason": "欠费停用"},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "suspended"


def test_matrix_settings_masking_and_admin_only(client: TestClient):
    # super_admin 写敏感配置
    headers = _auth(["super_admin"], username="matrix-settings")
    resp = client.put(
        "/api/v1/admin/settings/ai.embedding.api_key",
        headers=headers,
        json={"value": "sk-secret-123"},
    )
    assert resp.status_code == 200
    assert resp.json()["value"] == "******"  # 脱敏回显
    # operator 改 ai 组 403
    op_headers = _auth(["operator"], username="matrix-settings-op")
    resp = client.put(
        "/api/v1/admin/settings/ai.embedding.api_key",
        headers=op_headers,
        json={"value": "x"},
    )
    assert resp.status_code == 403
    # 检索组 operator 可改（即时生效组）
    resp = client.put("/api/v1/admin/settings/search.rrf_k", headers=op_headers, json={"value": 42})
    assert resp.status_code == 200


def test_matrix_delete_with_reason_records_audit(client: TestClient):
    """危险删除（OAuth 应用 / Webhook 订阅）携带理由入审计；无 body 的 DELETE 保持兼容。"""
    headers = _auth(["super_admin"], username="matrix-del-reason")

    # OAuth 应用：带理由删除 → 审计留痕
    resp = client.post(
        "/api/v1/admin/oauth/clients",
        headers=headers,
        json={"name": "待删除应用", "redirect_uris": ["https://app.example.com/cb"]},
    )
    assert resp.status_code == 201, resp.text
    client_db_id = resp.json()["id"]
    resp = client.request(
        "DELETE",
        f"/api/v1/admin/oauth/clients/{client_db_id}",
        headers=headers,
        json={"reason": "应用下线"},
    )
    assert resp.status_code == 204, resp.text
    resp = client.get(
        "/api/v1/admin/audit-logs",
        headers=headers,
        params={"action": "admin.oauth.client_delete", "limit": 5},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and items[0]["reason"] == "应用下线"

    # 无 body 的 DELETE 兼容（脚本/旧客户端不传 json 也可删除）
    resp = client.post(
        "/api/v1/admin/oauth/clients",
        headers=headers,
        json={"name": "无理由删除", "redirect_uris": ["https://app.example.com/cb2"]},
    )
    assert resp.status_code == 201, resp.text
    client_db_id = resp.json()["id"]
    resp = client.request(
        "DELETE", f"/api/v1/admin/oauth/clients/{client_db_id}", headers=headers
    )
    assert resp.status_code == 204, resp.text

    # Webhook 订阅：带理由删除 → 审计留痕
    resp = client.post(
        "/api/v1/admin/webhooks/subscriptions",
        headers=headers,
        json={
            "name": "待删除订阅",
            "url": "https://hooks.example.com/loomvec",
            "event_types": ["asset.ready"],
        },
    )
    assert resp.status_code == 201, resp.text
    sub_id = resp.json()["id"]
    resp = client.request(
        "DELETE",
        f"/api/v1/admin/webhooks/subscriptions/{sub_id}",
        headers=headers,
        json={"reason": "接收方迁移"},
    )
    assert resp.status_code == 204, resp.text
    resp = client.get(
        "/api/v1/admin/audit-logs",
        headers=headers,
        params={"action": "admin.webhook.subscription_delete", "limit": 5},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and items[0]["reason"] == "接收方迁移"


def test_matrix_reembed_cancel_reason_contract(client: TestClient):
    """取消理由模型生效：超长 reason 422；任务不存在 404（不经 celery/DB 状态）。"""
    headers = _auth(["super_admin"], username="matrix-reembed-cancel")
    missing = f"{('0' * 31)}1"
    resp = client.post(
        f"/api/v1/admin/reembed/{missing}/cancel",
        headers=headers,
        json={"reason": "x" * 501},
    )
    assert resp.status_code == 422  # reason 超长（max_length=500）
    resp = client.post(
        f"/api/v1/admin/reembed/{missing}/cancel",
        headers=headers,
        json={"reason": "仅测试"},
    )
    assert resp.status_code == 404  # 不存在 → NotFoundError，body 合法性先通过
