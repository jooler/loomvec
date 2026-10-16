"""MCP 接受用户 PAT：凭证识别、错误语义、用户全部可检索空间、无会话副作用。

DB/Redis/检索全部打桩（同 test_mcp_mentions.py 风格），无 Docker 依赖。
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from loomvec.api.context import Identity
from loomvec.api.routes import mcp
from loomvec.core.agent_tokens import CITATIONS_PREFIX, MENTIONS_PREFIX, AgentTokenClaims
from loomvec.core.errors import RateLimitedError, UnauthenticatedError

USER_ID = uuid.uuid4()


class _Redis:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _Session:
    @asynccontextmanager
    async def begin(self):
        yield

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Retriever:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return [object()]


def _state(redis: _Redis, retriever: _Retriever | None = None) -> mcp.McpAppState:
    return mcp.McpAppState(
        settings=None,  # type: ignore[arg-type]
        retriever=retriever,  # type: ignore[arg-type]
        session_factory=_Session,
        redis=redis,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# ASGI 鉴权层
# ---------------------------------------------------------------------------


async def _call(app, headers: list[tuple[bytes, bytes]]) -> tuple[int, dict | None]:
    sent: list = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/v1/mcp", "headers": headers}
    await app(scope, _receive, _send)
    status = sent[0]["status"]
    body = next((m["body"] for m in sent if m["type"] == "http.response.body"), b"")
    return status, json.loads(body) if body else None


def _capturing_app():
    seen: list[mcp.McpCaller] = []

    async def downstream(scope, receive, send):
        seen.append(mcp._caller())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return downstream, seen


def _fake_identity(monkeypatch, result):
    received: list[str] = []

    async def fake(raw_key, session, redis):
        received.append(raw_key)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(mcp, "identity_from_api_key", fake)
    return received


def _user_identity(scopes=("read",)) -> Identity:
    return Identity(user_id=str(USER_ID), username="alice", scopes=scopes)


@pytest.mark.parametrize(
    "headers",
    [
        [(b"authorization", b"Bearer lv_abc")],
        [(b"x-api-key", b"lv_abc")],
    ],
)
async def test_pat_accepted_via_bearer_or_x_api_key(monkeypatch, headers):
    received = _fake_identity(monkeypatch, _user_identity())
    downstream, seen = _capturing_app()
    status, _ = await _call(mcp._McpAuthASGI(downstream, _state(_Redis())), headers)
    assert status == 200
    assert received == ["lv_abc"]
    assert seen == [mcp.McpCaller(user_id=USER_ID, scope_space_ids=None, session_id=None)]


async def test_tenant_level_key_rejected(monkeypatch):
    _fake_identity(
        monkeypatch, Identity(user_id=f"apikey:{uuid.uuid4()}", username="k", scopes=("read",))
    )
    downstream, seen = _capturing_app()
    status, body = await _call(
        mcp._McpAuthASGI(downstream, _state(_Redis())), [(b"x-api-key", b"lv_abc")]
    )
    assert status == 403
    assert "绑定用户" in body["error"]["message"]
    assert seen == []


async def test_pat_without_read_scope_rejected(monkeypatch):
    _fake_identity(monkeypatch, _user_identity(scopes=("write",)))
    downstream, seen = _capturing_app()
    status, _ = await _call(
        mcp._McpAuthASGI(downstream, _state(_Redis())), [(b"x-api-key", b"lv_abc")]
    )
    assert status == 403
    assert seen == []


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (UnauthenticatedError(reason="API Key 无效或已过期"), 401, "API Key 无效或已过期"),
        (RateLimitedError(limit=1, window="60s"), 429, RateLimitedError.message),
    ],
)
async def test_pat_errors_keep_http_semantics(monkeypatch, error, expected, message):
    """状态码随错误语义；文案优先取 details.reason（具体原因），无则取默认 message。"""
    _fake_identity(monkeypatch, error)
    downstream, seen = _capturing_app()
    status, body = await _call(
        mcp._McpAuthASGI(downstream, _state(_Redis())), [(b"authorization", b"Bearer lv_x")]
    )
    assert status == expected
    assert body["error"]["message"] == message
    assert seen == []


async def test_non_prefixed_bearer_still_goes_agent_token_path(monkeypatch):
    """不带 PAT 前缀的 Bearer 不进 API Key 校验（agent JWT 路径，非法即 401）。"""
    received = _fake_identity(monkeypatch, _user_identity())

    settings = SimpleNamespace(auth=SimpleNamespace(jwt_secret="s", jwt_algorithm="HS256"))
    downstream, seen = _capturing_app()
    state = mcp.McpAppState(
        settings=settings,  # type: ignore[arg-type]
        retriever=None,  # type: ignore[arg-type]
        session_factory=_Session,
        redis=_Redis(),  # type: ignore[arg-type]
    )
    status, _ = await _call(
        mcp._McpAuthASGI(downstream, state), [(b"authorization", b"Bearer eyJ.not.jwt")]
    )
    assert status == 401
    assert received == []
    assert seen == []


# ---------------------------------------------------------------------------
# agent token 主路径（ASGI 层）
# ---------------------------------------------------------------------------


class _AuthRedis(_Redis):
    def __init__(self, revoked: set[str] | None = None) -> None:
        super().__init__()
        self.revoked = revoked or set()

    async def exists(self, key: str) -> int:
        return 1 if key.rsplit(":", 1)[-1] in self.revoked else 0


def _agent_state(redis: _Redis) -> mcp.McpAppState:
    from loomvec.core.config import Settings

    return mcp.McpAppState(
        settings=Settings(_env_file=None, auth={"jwt_secret": "test-secret-0123456789abcdef-xyz"}),
        retriever=None,  # type: ignore[arg-type]
        session_factory=_Session,
        redis=redis,  # type: ignore[arg-type]
    )


def _agent_token(state: mcp.McpAppState, **overrides) -> str:
    from loomvec.core.agent_tokens import issue_agent_token

    kwargs = {
        "user_id": str(USER_ID),
        "env_id": "env-1",
        "session_id": "sess-1",
        "scope_space_ids": [str(uuid.uuid4())],
    }
    kwargs.update(overrides)
    return issue_agent_token(state.settings, **kwargs)


async def test_valid_agent_token_becomes_scoped_session_caller():
    state = _agent_state(_AuthRedis())
    space = str(uuid.uuid4())
    token = _agent_token(state, scope_space_ids=[space])
    downstream, seen = _capturing_app()
    status, _ = await _call(
        mcp._McpAuthASGI(downstream, state), [(b"authorization", f"Bearer {token}".encode())]
    )
    assert status == 200
    assert seen == [
        mcp.McpCaller(
            user_id=USER_ID, scope_space_ids=frozenset({uuid.UUID(space)}), session_id="sess-1"
        )
    ]


async def test_revoked_agent_token_rejected():
    from loomvec.core.agent_tokens import decode_agent_token

    state = _agent_state(_AuthRedis())
    token = _agent_token(state)
    state.redis.revoked.add(decode_agent_token(state.settings, token).jti)  # type: ignore[attr-defined]
    downstream, seen = _capturing_app()
    status, body = await _call(
        mcp._McpAuthASGI(downstream, state), [(b"authorization", f"Bearer {token}".encode())]
    )
    assert status == 401
    assert "已失效" in body["error"]["message"]
    assert seen == []


@pytest.mark.parametrize(
    "overrides",
    [{"user_id": "not-a-uuid"}, {"scope_space_ids": ["not-a-uuid"]}],
)
async def test_agent_token_with_malformed_uuid_claims_is_401_not_500(overrides):
    state = _agent_state(_AuthRedis())
    token = _agent_token(state, **overrides)
    downstream, seen = _capturing_app()
    status, _ = await _call(
        mcp._McpAuthASGI(downstream, state), [(b"authorization", f"Bearer {token}".encode())]
    )
    assert status == 401
    assert seen == []


async def test_x_api_key_takes_priority_over_bearer_agent_token(monkeypatch):
    """两种凭证同时出现：按 PAT 校验（与 REST 的 get_identity 一致），agent token 不被采用。"""
    received = _fake_identity(monkeypatch, _user_identity())
    state = _agent_state(_AuthRedis())
    token = _agent_token(state)
    downstream, seen = _capturing_app()
    status, _ = await _call(
        mcp._McpAuthASGI(downstream, state),
        [(b"authorization", f"Bearer {token}".encode()), (b"x-api-key", b"lv_abc")],
    )
    assert status == 200
    assert received == ["lv_abc"]
    assert seen == [mcp.McpCaller(user_id=USER_ID, scope_space_ids=None, session_id=None)]


# ---------------------------------------------------------------------------
# 检索范围与会话副作用
# ---------------------------------------------------------------------------


@pytest.fixture()
def visible(monkeypatch):
    spaces = [uuid.uuid4(), uuid.uuid4()]

    async def fake_visible(session, user_id):
        return [spaces[0]]

    async def fake_linked(session, user_id):
        return [spaces[1]]

    class _Members:
        def __init__(self, session):
            pass

        async def list_for_user(self, user_id):
            return []

    monkeypatch.setattr(mcp, "visible_space_ids", fake_visible)
    monkeypatch.setattr(mcp, "linked_public_space_ids", fake_linked)
    monkeypatch.setattr("loomvec.core.db.repos.SpaceMemberRepo", _Members)
    return spaces


class _TenantSession:
    async def execute(self, stmt):
        class _R:
            def scalars(self):
                return iter([])

        return _R()


def as_caller(caller: mcp.McpCaller) -> None:
    """测试体内设置调用方（pytest-asyncio 每个测试跑在独立 Context 副本，不外泄）。"""
    mcp._current_caller.set(caller)


async def test_pat_scope_is_all_user_visible_spaces(visible):
    as_caller(mcp.McpCaller(user_id=USER_ID, scope_space_ids=None))
    effective, _, _ = await mcp._effective_scope(_TenantSession(), USER_ID, None)
    assert effective == visible


async def test_pat_requested_spaces_still_intersect_visible(visible):
    as_caller(mcp.McpCaller(user_id=USER_ID, scope_space_ids=None))
    foreign = uuid.uuid4()
    effective, _, _ = await mcp._effective_scope(_TenantSession(), USER_ID, [visible[1], foreign])
    assert effective == [visible[1]]


async def test_agent_empty_scope_stays_fail_closed(visible):
    claims = AgentTokenClaims(user_id=str(USER_ID), session_id="s", scope_space_ids=())
    as_caller(mcp.McpCaller.from_agent_claims(claims))
    effective, _, _ = await mcp._effective_scope(_TenantSession(), USER_ID, None)
    assert effective == []


@pytest.fixture()
def one_hit(monkeypatch):
    space_id = uuid.uuid4()

    async def fake_scope(session, user_id, requested):
        return [space_id], None, {}

    async def fake_enrich(session, hits, roles):
        return [
            {
                "unit_id": uuid.uuid4(),
                "asset_id": uuid.uuid4(),
                "asset_name": "a.pdf",
                "space_id": space_id,
                "unit_type": "text",
                "text": "正文",
            }
        ]

    monkeypatch.setattr(mcp, "_effective_scope", fake_scope)
    monkeypatch.setattr("loomvec.api.services.search.enrich_hits", fake_enrich)


async def test_pat_search_has_no_session_side_effects(one_hit):
    """PAT 无会话：不读任何会话的 @ 文献、不写引用旁路、batch_id 为空。"""
    as_caller(mcp.McpCaller(user_id=USER_ID, scope_space_ids=None))
    redis = _Redis({f"{MENTIONS_PREFIX}None": json.dumps([str(uuid.uuid4())])})
    retriever = _Retriever()
    out = await mcp.tool_search_knowledge(_state(redis, retriever), "q")
    assert retriever.calls[0]["asset_ids"] is None
    assert out["batch_id"] == ""
    assert len(out["ref_items"]) == 1
    assert not any(k.startswith(CITATIONS_PREFIX) for k in redis.store)


async def test_agent_search_still_writes_citations(one_hit):
    claims = AgentTokenClaims(
        user_id=str(USER_ID), session_id="sess", scope_space_ids=(str(uuid.uuid4()),)
    )
    as_caller(mcp.McpCaller.from_agent_claims(claims))
    redis = _Redis()
    out = await mcp.tool_search_knowledge(_state(redis, _Retriever()), "q")
    assert out["batch_id"]
    assert f"{CITATIONS_PREFIX}sess:{out['batch_id']}" in redis.store


async def test_list_my_spaces_serializes_space_rows(monkeypatch):
    """回归：空间类型字段是 Space.space_type（曾误读不存在的 .type → 工具恒报错）。"""
    from loomvec.core.db.models import Space, SpaceMember, SpaceRole, SpaceType

    space = Space(id=uuid.uuid4(), slug="s", name="团队库", space_type=SpaceType.SHARED)

    async def fake_scope(session, user_id, requested):
        return [space.id], None, {}

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [space]

    class _SpaceSession(_Session):
        async def execute(self, stmt):
            return _Rows()

    class _Members:
        def __init__(self, session):
            pass

        async def list_for_user(self, user_id):
            return [SpaceMember(space_id=space.id, user_id=USER_ID, role=SpaceRole.EDITOR)]

    monkeypatch.setattr(mcp, "_effective_scope", fake_scope)
    monkeypatch.setattr("loomvec.core.db.repos.SpaceMemberRepo", _Members)
    as_caller(mcp.McpCaller(user_id=USER_ID, scope_space_ids=None))
    state = mcp.McpAppState(
        settings=None,  # type: ignore[arg-type]
        retriever=None,  # type: ignore[arg-type]
        session_factory=_SpaceSession,
        redis=_Redis(),  # type: ignore[arg-type]
    )
    out = await mcp.tool_list_my_spaces(state)
    assert out == [
        {"space_id": str(space.id), "name": "团队库", "type": "shared", "role": "editor"}
    ]
