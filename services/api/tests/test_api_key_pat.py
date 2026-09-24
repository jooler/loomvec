"""P5 API Key 双语义单测：租户级 Key 空间闸门回归 + 绑定用户 Key（PAT）身份派生。

背景（Loomvec 知识库集成实施方案 · Plan 5.1/5.2）：
- 修复前：API Key + space_id 检索在 search._resolve_scope 取 access.role 时
  AttributeError → 500（SpaceAccess(member=None) 无 role）；此处以假 session
  直测该调用链（全栈集成需 Milvus，不做 HTTP 级回归）；
- PAT：ApiKey.user_id 非空 → 身份取绑定用户（平台角色单源 DB），scopes =
  key 声明 scopes ∩ 角色派生 scopes；租户级 Key（user_id 空）行为不变。

DB/Redis 全部打桩（与 test_mineru_compat.py 同风格），无 Docker 依赖。
"""

from __future__ import annotations

import uuid

import pytest

from loomvec.api.context import Identity
from loomvec.api.deps import resolve_space_access
from loomvec.api.identity import hash_api_key, identity_from_api_key
from loomvec.api.routes.search import _resolve_scope
from loomvec.core.authz import SpaceAccess
from loomvec.core.db.models import ApiKey, Space, SpaceRole, User, UserStatus
from loomvec.core.errors import UnauthenticatedError

TENANT = uuid.uuid4()
SPACE_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


class _FakeSession:
    """按入队顺序吐结果（select 次序即实现内部查询次序）；commit/flush 免实现。

    execute → 走 _FakeResult（scalar_one_or_none / scalars().all()）；
    scalar → 直接吐值（_ensure_tenant_active 的 Tenant.status 单值查询）。
    """

    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, stmt):
        return _FakeResult(self._results.pop(0))

    async def scalar(self, stmt):
        return self._results.pop(0)

    async def commit(self):
        return None

    async def flush(self):
        return None


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def expire(self, key, ttl):
        return True


def _space() -> Space:
    return Space(id=SPACE_ID, tenant_id=TENANT, slug="demo", name="Demo")


def _tenant_key(scopes: list[str] | None = None) -> ApiKey:
    return ApiKey(
        name="app-key",
        key_hash="h",
        scopes=scopes or ["read"],
        rate_limit_per_min=600,
        tenant_id=TENANT,
    )


def _bound_key(scopes: list[str] | None = None) -> ApiKey:
    return ApiKey(
        name="pat",
        key_hash="h",
        scopes=scopes or ["read"],
        rate_limit_per_min=600,
        tenant_id=TENANT,
        user_id=USER_ID,
    )


def _user(status: UserStatus = UserStatus.ACTIVE) -> User:
    u = User(tenant_id=TENANT, username="alice", display_name="alice")
    u.id = USER_ID
    u.status = status
    return u


# ---------------------------------------------------------------------------
# SpaceAccess：member=None（API Key 租户兜底）role 兜底 editor（5.1 回归根）
# ---------------------------------------------------------------------------


def test_space_access_role_defaults_to_editor_without_member():
    access = SpaceAccess(space=_space(), member=None)
    assert access.role == SpaceRole.EDITOR


def test_space_access_role_reads_member_role():
    from loomvec.core.db.models import SpaceMember

    member = SpaceMember(space_id=SPACE_ID, user_id=USER_ID, role=SpaceRole.VIEWER)
    access = SpaceAccess(space=_space(), member=member)
    assert access.role == SpaceRole.VIEWER


# ---------------------------------------------------------------------------
# resolve_space_access（API Key 租户兜底）× _resolve_scope（原 500 现场）
# ---------------------------------------------------------------------------


async def test_resolve_space_access_apikey_role_is_editor():
    identity = Identity(
        user_id="apikey:00000000-0000-0000-0000-0000000000f0",
        username="app-key",
        tenant_id=str(TENANT),
        roles=("api_key",),
        scopes=("read",),
    )
    access = await resolve_space_access(_FakeSession(_space()), identity, SPACE_ID)
    assert access.role == SpaceRole.EDITOR  # 修复前：AttributeError → 500


async def test_search_resolve_scope_with_api_key_and_space_id():
    """API Key + space_id 检索范围解析不再 500（原 bug 现场）。"""
    identity = Identity(
        user_id="apikey:00000000-0000-0000-0000-0000000000f0",
        username="app-key",
        tenant_id=str(TENANT),
        roles=("api_key",),
        scopes=("read",),
    )
    space_ids, tenant_id, roles = await _resolve_scope(_FakeSession(_space()), identity, SPACE_ID)
    assert space_ids == [SPACE_ID]
    assert str(tenant_id) == str(TENANT)
    assert roles == {SPACE_ID: SpaceRole.EDITOR}


async def test_search_resolve_scope_apikey_aggregate_still_rejected():
    identity = Identity(
        user_id="apikey:00000000-0000-0000-0000-0000000000f0",
        username="app-key",
        tenant_id=str(TENANT),
        roles=("api_key",),
        scopes=("read",),
    )
    from loomvec.core.errors import ValidationError

    with pytest.raises(ValidationError):
        await _resolve_scope(_FakeSession(), identity, None)


# ---------------------------------------------------------------------------
# identity_from_api_key：租户级语义不变 / PAT 用户语义
# ---------------------------------------------------------------------------


async def test_tenant_key_identity_unchanged():
    key = _tenant_key()
    identity = await identity_from_api_key("lv_whatever", _FakeSession(key, None), _FakeRedis())
    assert identity.user_id.startswith("apikey:")
    assert identity.roles == ("api_key",)
    assert identity.scopes == ("read",)


async def test_bound_key_identity_uses_user_semantics():
    key = _bound_key()
    session = _FakeSession(key, None, _user(), ["operator"])
    identity = await identity_from_api_key("lv_whatever", session, _FakeRedis())
    assert identity.user_id == str(USER_ID)
    assert identity.username == "alice"
    assert identity.tenant_id == str(TENANT)
    assert identity.roles == ("operator",)
    # key 声明 scopes ∩ 角色派生：operator 派生全量，交集收在 key 的 ["read"]
    assert identity.scopes == ("read",)


async def test_bound_key_scopes_capped_by_key_declaration():
    key = _bound_key(scopes=["read", "write"])
    session = _FakeSession(key, None, _user(), [])  # 普通用户：派生 read+write
    identity = await identity_from_api_key("lv_whatever", session, _FakeRedis())
    assert identity.scopes == ("read", "write")


async def test_bound_key_of_disabled_user_rejected():
    key = _bound_key()
    session = _FakeSession(key, None, _user(UserStatus.DISABLED))
    with pytest.raises(UnauthenticatedError):
        await identity_from_api_key("lv_whatever", session, _FakeRedis())


async def test_expired_key_rejected():
    from datetime import UTC, datetime, timedelta

    key = _tenant_key()
    key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(UnauthenticatedError):
        await identity_from_api_key("lv_whatever", _FakeSession(key), _FakeRedis())


def test_hash_api_key_stable():
    assert hash_api_key("lv_x") == hash_api_key("lv_x")
    assert hash_api_key("lv_x") != hash_api_key("lv_y")
