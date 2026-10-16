"""MCP search_knowledge：本轮 @ 文献按会话写入 Redis，检索强制收敛（不依赖模型传参）。"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import pytest

from loomvec.api.routes import mcp
from loomvec.core.agent_tokens import MENTIONS_PREFIX, AgentTokenClaims

ENV_ID = "env-1"
SESSION_ID = "s"
USER_ID = str(uuid.uuid4())


class _Redis:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store = store or {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _Retriever:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return []


class _Session:
    @asynccontextmanager
    async def begin(self):
        yield

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _state(redis: _Redis, retriever: _Retriever) -> mcp.McpAppState:
    return mcp.McpAppState(
        settings=None,  # type: ignore[arg-type]
        retriever=retriever,  # type: ignore[arg-type]
        session_factory=_Session,
        redis=redis,  # type: ignore[arg-type]
    )


@pytest.fixture()
def claims():
    token = mcp._current_caller.set(
        mcp.McpCaller.from_agent_claims(
            AgentTokenClaims(
                user_id=USER_ID,
                env_id=ENV_ID,
                session_id=SESSION_ID,
                scope_space_ids=(str(uuid.uuid4()),),
            )
        )
    )
    yield
    mcp._current_caller.reset(token)


@pytest.fixture()
def one_space(monkeypatch):
    space_id = uuid.uuid4()

    async def fake_scope(session, user_id, requested):
        return [space_id], None, {}

    async def fake_enrich(session, hits, roles):
        return []

    monkeypatch.setattr(mcp, "_effective_scope", fake_scope)
    monkeypatch.setattr("loomvec.api.services.search.enrich_hits", fake_enrich)


async def test_mentioned_assets_are_forced_into_retrieval(claims, one_space):
    assets = [uuid.uuid4(), uuid.uuid4()]
    redis = _Redis({f"{MENTIONS_PREFIX}{SESSION_ID}": json.dumps([str(a) for a in assets])})
    retriever = _Retriever()
    await mcp.tool_search_knowledge(_state(redis, retriever), "方法是什么")
    assert retriever.calls[0]["asset_ids"] == assets


async def test_no_mentions_means_no_asset_filter(claims, one_space):
    retriever = _Retriever()
    await mcp.tool_search_knowledge(_state(_Redis(), retriever), "方法是什么")
    assert retriever.calls[0]["asset_ids"] is None


async def test_mentions_are_scoped_per_session(claims, one_space):
    """别的会话（同 env 下并发回合）的 @ 不会影响本会话的检索。"""
    redis = _Redis({f"{MENTIONS_PREFIX}other-session": json.dumps([str(uuid.uuid4())])})
    retriever = _Retriever()
    await mcp.tool_search_knowledge(_state(redis, retriever), "q")
    assert retriever.calls[0]["asset_ids"] is None


async def test_corrupt_mentions_value_does_not_break_search(claims, one_space):
    redis = _Redis({f"{MENTIONS_PREFIX}{SESSION_ID}": "not-json"})
    retriever = _Retriever()
    await mcp.tool_search_knowledge(_state(redis, retriever), "q")
    assert retriever.calls[0]["asset_ids"] is None
