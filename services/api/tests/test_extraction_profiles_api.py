"""抽取领域配置的 API 契约：可选配置列表、空间创建/设置/管理端/运营端的 key 校验、
自定义配置（system_config `pipeline.extraction_profiles`）写入校验与被引用时的删除拦截、
已失效配置的标记与「值未变放行」、重抽接口的覆盖校验。

dev JWT + testcontainers 临时 PG（conftest 的 pg_url_module，模块内共用一个容器；redis/milvus/
对象存储指向不可达地址）：本文件会建用户/空间并写 system_config，绝不能落到真实 settings 指向的库。
用例里「配置被删」通过直接改临时库模拟（API 层已拦截删除被引用的配置，等价于手工改库/历史遗留的
失效引用）。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from loomvec.api.app import create_app
from loomvec.core.config import Settings, get_settings
from loomvec.core.graph.profiles import CUSTOM_PROFILES_SETTING_KEY

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = [pytest.mark.integration]

_UNREACHABLE = "127.0.0.1:1"

_CUSTOM = {
    "label": "专利文献",
    "entity_types": [
        {"type": "patent", "label": "专利"},
        {"type": "organization", "label": "申请人"},
        {"type": "other", "label": "其他"},
    ],
    "priority_hint": "专利与申请人",
    "relation_types": ["FILED_BY"],
    "example": {
        "entities": [
            {"name": "CN1A", "type": "patent", "description": "一件专利"},
            {"name": "甲公司", "type": "organization", "description": "专利申请人"},
        ],
        "relation": {"head": "CN1A", "tail": "甲公司", "type": "FILED_BY"},
    },
}


@pytest.fixture(scope="module")
def client(pg_url_module):
    settings = Settings(
        _env_file=None,
        auth={"dev_mode": True},
        postgres={"url": pg_url_module},
        redis={"url": f"redis://{_UNREACHABLE}/0"},
        milvus={"uri": f"http://{_UNREACHABLE}"},
        storage={"endpoint": f"http://{_UNREACHABLE}"},
        security={"rate_limit_enabled": False},
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def _auth(client: TestClient, username: str, roles: list[str]) -> dict[str, str]:
    resp = client.post("/api/v1/auth/dev/token", json={"username": username, "roles": roles})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _put_custom(client: TestClient, headers: dict, value):
    return client.put(
        f"/api/v1/admin/settings/{CUSTOM_PROFILES_SETTING_KEY}",
        headers=headers,
        json={"value": value, "reason": "测试"},
    )


def _pg_url(client: TestClient) -> str:
    return client.app.state.settings.postgres.url


def _force_custom(client: TestClient, value) -> None:
    """绕过 API 直接改临时库里的自定义配置（模拟配置被手工删除/改坏）。"""

    async def _run() -> None:
        engine = create_async_engine(_pg_url(client))
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE system_config SET value = CAST(:v AS jsonb) WHERE key = :k"),
                    {"v": json.dumps(value), "k": CUSTOM_PROFILES_SETTING_KEY},
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_space(client, headers, name, profile=None) -> dict:
    resp = client.post(
        "/api/v1/spaces", headers=headers, json={"name": name, "extraction_profile": profile}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture()
def admin_user(client):
    """super_admin + 普通用户；结束时让本用例建的空间不再引用自定义配置并清空它。

    system_config 在模块共用的临时库里，清空只是让用例间互不影响，不触及任何真实库。"""
    admin = _auth(client, _name("epf-admin"), ["super_admin"])
    user = _auth(client, _name("epf-user"), ["user"])
    created: list[str] = []
    yield admin, user, created
    for sid in created:
        client.patch(f"/api/v1/spaces/{sid}", headers=user, json={"extraction_profile": None})
        client.delete(f"/api/v1/spaces/{sid}", headers=user)
    _force_custom(client, {})


def test_list_profiles_and_space_key_validation(client):
    user = _auth(client, _name("epf-user"), ["user"])
    resp = client.get("/api/v1/extraction-profiles", headers=user)
    assert resp.status_code == 200
    body = resp.json()
    keys = {p["key"] for p in body["items"]}
    assert {"general", "academic", "biomedical", "legal"} <= keys
    assert body["default"] == "general"
    legal = next(p for p in body["items"] if p["key"] == "legal")
    assert (
        legal["is_builtin"]
        and {"type": "statute", "label": "法律法规及具体条款"} in (legal["entity_types"])
    )

    bad = client.post(
        "/api/v1/spaces",
        headers=user,
        json={"name": _name("epf-bad"), "extraction_profile": "nope"},
    )
    assert bad.status_code == 422
    assert bad.json()["message"] == "不支持的抽取领域配置"
    assert bad.json()["details"]["got"] == "nope"

    created = _create_space(client, user, _name("epf-legal"), "legal")
    space_id = created["id"]
    try:
        assert created["extraction_profile"] == "legal"
        assert created["extraction_profile_valid"] is True
        bad_patch = client.patch(
            f"/api/v1/spaces/{space_id}", headers=user, json={"extraction_profile": "x"}
        )
        assert bad_patch.status_code == 422
        assert bad_patch.json()["details"]["got"] == "x"
        cleared = client.patch(
            f"/api/v1/spaces/{space_id}", headers=user, json={"extraction_profile": None}
        )
        assert cleared.status_code == 200 and cleared.json()["extraction_profile"] is None
        assert cleared.json()["extraction_profile_valid"] is True
    finally:
        client.delete(f"/api/v1/spaces/{space_id}", headers=user)


def test_custom_profiles_setting_validated_and_selectable(client, admin_user):
    admin, user, created = admin_user
    assert _put_custom(client, admin, {"general": _CUSTOM}).status_code == 422  # 不得覆盖内置
    assert (
        _put_custom(client, admin, {"patent": {**_CUSTOM, "entity_types": []}}).status_code == 422
    )
    # 旧结构（单个 example.entity）已不再接受
    old_example = {"entity": _CUSTOM["example"]["entities"][0], "relation": {}}
    assert (
        _put_custom(client, admin, {"patent": {**_CUSTOM, "example": old_example}}).status_code
        == 422
    )
    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200

    items = client.get("/api/v1/extraction-profiles", headers=user).json()["items"]
    patent = next(p for p in items if p["key"] == "patent")
    assert not patent["is_builtin"]

    space = _create_space(client, user, _name("epf-patent"), "patent")
    created.append(space["id"])
    assert space["extraction_profile_valid"] is True

    # 管理端调整：必填理由，key 须可用
    url = f"/api/v1/admin/spaces/{space['id']}/extraction-profile"
    nope = client.put(url, headers=admin, json={"extraction_profile": "nope", "reason": "测试"})
    assert nope.status_code == 422 and nope.json()["details"]["got"] == "nope"
    ok = client.put(url, headers=admin, json={"extraction_profile": "biomedical", "reason": "t"})
    assert ok.status_code == 200
    detail = client.get(f"/api/v1/admin/spaces/{space['id']}", headers=admin).json()
    assert detail["extraction_profile"] == "biomedical"
    assert detail["extraction_profile_valid"] is True


def test_custom_profiles_setting_is_super_admin_only(client, admin_user):
    """配置文本原样进所有空间抽取 LLM 的 system prompt：operator 有 admin:write 也不能改。"""
    admin, _user, _created = admin_user
    operator = _auth(client, _name("epf-op"), ["operator"])
    auditor = _auth(client, _name("epf-aud"), ["auditor"])

    for headers in (operator, auditor):
        denied = _put_custom(client, headers, {"patent": _CUSTOM})
        assert denied.status_code == 403
    # 被拒绝的写入没有落库
    items = client.get("/api/v1/extraction-profiles", headers=admin).json()["items"]
    assert "patent" not in {p["key"] for p in items}

    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200
    # operator 仍可读（admin:read）
    got = client.get(f"/api/v1/admin/settings/{CUSTOM_PROFILES_SETTING_KEY}", headers=operator)
    assert got.status_code == 200
    # operator 同样不能删/清空
    assert _put_custom(client, operator, {}).status_code == 403
    assert _put_custom(client, admin, {}).status_code == 200


def test_delete_referenced_custom_profile_is_blocked(client, admin_user):
    admin, user, created = admin_user
    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200
    name = _name("epf-ref")
    space = _create_space(client, user, name, "patent")
    created.append(space["id"])

    # 删除（写成不含 patent 的集合）被拦：422，信息列出空间名称与 ID
    blocked = _put_custom(client, admin, {})
    assert blocked.status_code == 422
    body = blocked.json()
    assert name in body["message"] and space["id"] in body["message"]
    assert {"id": space["id"], "name": name, "extraction_profile": "patent"} in body["details"][
        "spaces"
    ]
    items = client.get("/api/v1/extraction-profiles", headers=user).json()["items"]
    assert "patent" in {p["key"] for p in items}  # 拦截后配置仍在

    # 改写配置内容（key 仍在）不受影响
    assert (
        _put_custom(client, admin, {"patent": {**_CUSTOM, "label": "专利文献2"}}).status_code == 200
    )

    # 空间改用其他配置后可删除
    assert (
        client.patch(
            f"/api/v1/spaces/{space['id']}", headers=user, json={"extraction_profile": "legal"}
        ).status_code
        == 200
    )
    assert _put_custom(client, admin, {}).status_code == 200


def test_delete_custom_profile_ignores_soft_deleted_spaces(client, admin_user):
    admin, user, created = admin_user
    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200
    space = _create_space(client, user, _name("epf-soft"), "patent")
    created.append(space["id"])
    client.delete(f"/api/v1/spaces/{space['id']}", headers=user)  # 软删（级联清理另测）
    assert _put_custom(client, admin, {}).status_code == 200


def test_delete_custom_profile_blocked_by_global_default(client, admin_user, monkeypatch):
    admin, _user, _created = admin_user
    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200
    monkeypatch.setattr(get_settings().pipeline, "extraction_profile", "patent")
    blocked = _put_custom(client, admin, {})
    assert blocked.status_code == 422
    assert "pipeline.extraction_profile" in blocked.json()["message"]
    assert blocked.json()["details"]["global_profile"] == "patent"
    # 全局默认不再引用后可删
    monkeypatch.setattr(get_settings().pipeline, "extraction_profile", "general")
    assert _put_custom(client, admin, {}).status_code == 200


def test_dangling_profile_flagged_and_unchanged_value_passes(client, admin_user):
    admin, user, created = admin_user
    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200
    space = _create_space(client, user, _name("epf-dangling"), "patent")
    sid = space["id"]
    created.append(sid)

    _force_custom(client, {})  # 配置被手工删除，空间仍引用 patent

    got = client.get(f"/api/v1/spaces/{sid}", headers=user).json()
    assert got["extraction_profile"] == "patent" and got["extraction_profile_valid"] is False
    listed = client.get("/api/v1/spaces", headers=user).json()["items"]
    assert next(s for s in listed if s["id"] == sid)["extraction_profile_valid"] is False
    admin_detail = client.get(f"/api/v1/admin/spaces/{sid}", headers=admin).json()
    assert admin_detail["extraction_profile_valid"] is False
    admin_list = client.get("/api/v1/admin/spaces", headers=admin, params={"q": space["slug"]})
    assert admin_list.json()["items"][0]["extraction_profile_valid"] is False

    # 只改名称、其余字段原样提交（含已失效的 patent）：放行，标记仍为 false
    new_name = _name("epf-renamed")
    renamed = client.patch(
        f"/api/v1/spaces/{sid}",
        headers=user,
        json={"name": new_name, "extraction_profile": "patent"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == new_name
    assert renamed.json()["extraction_profile"] == "patent"
    assert renamed.json()["extraction_profile_valid"] is False

    # 改成另一个不可用的值仍被拒
    assert (
        client.patch(
            f"/api/v1/spaces/{sid}", headers=user, json={"extraction_profile": "nope"}
        ).status_code
        == 422
    )

    # 管理端提交与当前相同的值：放行；改成可用值后标记恢复
    url = f"/api/v1/admin/spaces/{sid}/extraction-profile"
    same = client.put(url, headers=admin, json={"extraction_profile": "patent", "reason": "t"})
    assert same.status_code == 200
    fixed = client.patch(
        f"/api/v1/spaces/{sid}", headers=user, json={"extraction_profile": "legal"}
    )
    assert fixed.status_code == 200 and fixed.json()["extraction_profile_valid"] is True


def test_ops_space_extraction_profile(client, admin_user):
    admin, _user, _created = admin_user
    ops = _auth(client, _name("epf-ops"), ["operator"])
    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200

    bad = client.post(
        "/api/v1/ops/spaces",
        headers=ops,
        json={"name": _name("epf-ops-bad"), "extraction_profile": "nope"},
    )
    assert bad.status_code == 422

    created = client.post(
        "/api/v1/ops/spaces",
        headers=ops,
        json={"name": _name("epf-ops"), "extraction_profile": "patent"},
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    try:
        assert created.json()["extraction_profile"] == "patent"
        assert created.json()["extraction_profile_valid"] is True

        _force_custom(client, {})
        detail = client.get(f"/api/v1/ops/spaces/{sid}", headers=ops).json()
        assert detail["extraction_profile_valid"] is False
        listed = client.get("/api/v1/ops/spaces", headers=ops).json()["items"]
        assert next(s for s in listed if s["id"] == sid)["extraction_profile_valid"] is False

        # 值未变放行；改成不可用值被拒；改成可用值后标记恢复
        renamed = client.patch(
            f"/api/v1/ops/spaces/{sid}",
            headers=ops,
            json={"name": _name("epf-ops-renamed"), "extraction_profile": "patent"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["extraction_profile_valid"] is False
        assert (
            client.patch(
                f"/api/v1/ops/spaces/{sid}", headers=ops, json={"extraction_profile": "nope"}
            ).status_code
            == 422
        )
        fixed = client.patch(
            f"/api/v1/ops/spaces/{sid}", headers=ops, json={"extraction_profile": "legal"}
        )
        assert fixed.status_code == 200 and fixed.json()["extraction_profile_valid"] is True
    finally:
        client.patch(f"/api/v1/ops/spaces/{sid}", headers=ops, json={"extraction_profile": None})
        client.delete(f"/api/v1/ops/spaces/{sid}", headers=ops)


class _FakeCelery:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def send_task(self, name, args=None, kwargs=None, **_opts):
        self.sent.append((name, args, kwargs))


def _insert_asset(client: TestClient, space_id: str) -> str:
    from loomvec.core.db.models import Asset

    async def _run() -> str:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = create_async_engine(_pg_url(client))
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                asset = Asset(
                    space_id=uuid.UUID(space_id),
                    name="epf-retry.md",
                    mime_type="text/markdown",
                    ext=".md",
                    size_bytes=1,
                )
                session.add(asset)
                await session.commit()
                return str(asset.id)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_retry_extraction_profile_override(client, admin_user):
    admin, user, created = admin_user
    assert _put_custom(client, admin, {"patent": _CUSTOM}).status_code == 200
    space = _create_space(client, user, _name("epf-retry"))
    created.append(space["id"])
    asset_id = _insert_asset(client, space["id"])
    celery = _FakeCelery()
    client.app.state.celery = celery
    url = f"/api/v1/assets/{asset_id}/retry"

    # 未知 key / 格式非法的 key：422，不投递任务
    for bad in ("nope", "Bad Key!"):
        resp = client.post(url, headers=user, json={"step": "parse", "extraction_profile": bad})
        assert resp.status_code == 422, resp.text
        assert resp.json()["message"] == "不支持的抽取领域配置"
        assert resp.json()["details"]["got"] == bad
    assert celery.sent == []

    # 覆盖只在从 parse/chunk 起跑时有意义：embed 起跑带覆盖被拒（即使 key 可用）
    late = client.post(url, headers=user, json={"step": "embed", "extraction_profile": "legal"})
    assert late.status_code == 422 and "parse 或 chunk" in late.json()["message"]
    assert celery.sent == []

    # 内置与自定义配置都可作为一次性覆盖，随任务 kwargs 透传，且不落到空间配置
    for key in ("legal", "patent"):
        ok = client.post(url, headers=user, json={"step": "parse", "extraction_profile": key})
        assert ok.status_code == 202, ok.text
        assert celery.sent[-1] == (
            "pipeline.process_asset",
            [asset_id, "parse"],
            {"extraction_profile": key},
        )
    assert (
        client.get(f"/api/v1/spaces/{space['id']}", headers=user).json()["extraction_profile"]
        is None
    )

    # 自定义配置被删后，再用它做覆盖同样被拒
    sent = len(celery.sent)
    _force_custom(client, {})
    gone = client.post(url, headers=user, json={"step": "parse", "extraction_profile": "patent"})
    assert gone.status_code == 422 and gone.json()["details"]["got"] == "patent"
    assert len(celery.sent) == sent
