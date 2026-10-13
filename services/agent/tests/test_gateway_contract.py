"""契约测试：客户端对网关的每个请求都对得上网关 openapi 快照。

不是比较文件是否相等，而是：
1. 客户端声明的每个 (method, path) 都存在于 `openapi.snapshot.json`；
2. 实际发出的请求体按快照里的 JSON Schema 校验（字段名、必填、枚举、`additionalProperties:false`）；
3. 实际带的查询参数都是快照声明过的；
4. 客户端读取的响应字段（如 `id`、`cancelled`）在快照的响应 schema 里存在；
5. 声明的端点必须全部被本测试走到（防止登记表与实际调用漂移）。

网关升级时：把网关仓库的 `openapi.json` 拷到 `gateway/openapi.snapshot.json`，跑本测试看差异。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from loomvec.agent.approvals import build_permission_policy
from loomvec.agent.config import GatewayConnection
from loomvec.agent.gateway import client as client_mod
from loomvec.agent.gateway.client import ENDPOINTS
from loomvec.agent.gateway.orchestrator import AskContext, build_prompt_blocks
from loomvec.agent.gateway.service import GatewayService, PreparedSession
from loomvec.core.config import AgentSettings, Settings
from loomvec.core.db.models import AgentSession

from ._gw import ev, make_client, sse_response

SNAPSHOT = json.loads(
    (Path(client_mod.__file__).parent / "openapi.snapshot.json").read_text(encoding="utf-8")
)
PATHS: dict[str, dict] = SNAPSHOT["paths"]


# ---------------------------------------------------------------------------
# 迷你 JSON Schema 校验（覆盖网关 openapi 用到的关键字）
# ---------------------------------------------------------------------------

_TYPES = {
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _is_type(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def validate(schema: dict, value: Any, where: str = "$") -> list[str]:
    """返回违规描述列表（空 = 通过）。"""
    if "$ref" in schema:
        schema = SNAPSHOT["components"]["schemas"][schema["$ref"].split("/")[-1]]
    errs: list[str] = []
    for key in ("anyOf", "oneOf"):
        if key in schema:
            if not any(not validate(sub, value, where) for sub in schema[key]):
                errs.append(f"{where}: 不匹配 {key} 的任何分支")
            return errs
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_is_type(value, t) for t in types):
            return [f"{where}: 类型应为 {types}，实际 {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} 不在枚举 {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errs.append(f"{where}: 太短")
        if len(value) > schema.get("maxLength", 10**12):
            errs.append(f"{where}: 太长")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            errs.append(f"{where}: 元素个数越界")
        for i, item in enumerate(value):
            errs += validate(schema.get("items", {}), item, f"{where}[{i}]")
    if isinstance(value, dict) and ("properties" in schema or "required" in schema):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{where}: 缺少必填字段 {req}")
        for k, v in value.items():
            if k in props:
                errs += validate(props[k], v, f"{where}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{where}: 未知字段 {k}")
    return errs


def _body_schema(method: str, tmpl: str) -> dict | None:
    rb = PATHS[tmpl][method.lower()].get("requestBody")
    return rb["content"]["application/json"]["schema"] if rb else None


def _template_regex(tmpl: str) -> re.Pattern[str]:
    out = re.escape(tmpl).replace(r"\{\*\}", r".+")
    out = re.sub(r"\\\{(\w+)\\\}", r"[^/]+", out)
    return re.compile(f"^{out}$")


def _match_endpoint(request: httpx.Request):
    for ep in ENDPOINTS.values():
        if ep.method == request.method and _template_regex(ep.path).match(request.url.path):
            return ep
    return None


# ---------------------------------------------------------------------------
# 走遍所有端点
# ---------------------------------------------------------------------------


def _canned(request: httpx.Request) -> httpx.Response:
    """对每个端点回一个 schema 合理的响应。"""
    path, method = request.url.path, request.method
    if path.endswith("/events") or path.endswith("/turns"):
        return sse_response([ev("turn.completed", 1, stop_reason="end_turn", usage=None)])
    if path == "/healthz":
        return httpx.Response(200, json={"ok": True})
    if path == "/v1/workspaces" and method == "POST":
        return httpx.Response(201, json={"id": "ws_1", "external_ref": "e", "status": "active"})
    if path == "/v1/workspaces":
        return httpx.Response(
            200, json={"items": [{"id": "ws_1", "external_ref": "e", "status": "active"}]}
        )
    if path.endswith("/instructions"):
        return httpx.Response(200, json={"content": "x", "bytes": 1})
    if path.endswith("/sessions") and method == "POST":
        return httpx.Response(201, json={"id": "ses_1"})
    if path.endswith("/cancel"):
        return httpx.Response(200, json={"cancelled": True, "turn_id": "trn_1"})
    if path.endswith("/messages"):
        return httpx.Response(200, json={"items": [], "next_cursor": None})
    if "/turns/" in path:
        return httpx.Response(200, json={"id": "trn_1", "usage": None})
    if path.endswith("/tree"):
        return httpx.Response(200, json={"path": "", "entries": [], "truncated": False})
    if "/files/" in path and method == "GET":
        if request.url.params.get("stat"):
            return httpx.Response(200, json={"name": "a", "type": "file", "size": 1})
        return httpx.Response(200, content=b"abc", headers={"content-length": "3"})
    if "/files/" in path:
        return httpx.Response(200, json={"path": "a", "size": 3})
    if "/dirs/" in path:
        return httpx.Response(200, json={"path": "p"})
    return httpx.Response(200, json={"id": "ses_1"})  # PATCH / DELETE session


def _row(agent: str) -> AgentSession:
    import uuid

    return AgentSession(
        id=uuid.uuid4(),
        env_id=uuid.uuid4(),
        title="新会话",
        project_path="projects/p",
        scope_space_ids=[],
        agent=agent,
    )


async def _exercise_all() -> list[httpx.Request]:
    client, rec = make_client(_canned)
    await client.healthz()
    ws_id, _ = await client.get_or_create_workspace("env-1", {"k": "v"})
    await client.find_workspace_by_ref("env-1")
    await client.get_instructions(ws_id)
    await client.put_instructions(ws_id, "# AGENTS")

    svc = GatewayService(
        type("Cfg", (), {"agent": AgentSettings(), "settings": Settings()})(),  # type: ignore[arg-type]
        None,
    )
    server = {
        "type": "http",
        "name": "loomvec",
        "url": "http://host.docker.internal:38080/api/v1/mcp",
        "headers": [{"name": "Authorization", "value": "Bearer t"}],
    }
    for agent, model in (("claude", "claude-sonnet"), ("dsh", "deepseek-v4-flash")):
        conn = GatewayConnection(
            url="http://gw",
            api_key="k",
            request_timeout_s=30,
            default_agent=agent,
            models={agent: model},
        )
        await client.create_session(ws_id, svc._session_body(conn, _row(agent), server))
    await client.patch_session("ses_1", {"mcp_servers": [server]})
    await client.archive_session("ses_1")

    prepared = PreparedSession(
        workspace_id=ws_id, gateway_session_id="ses_1", agent="dsh", model="m"
    )
    from loomvec.agent.attachments import AttachmentMeta

    ctx = AskContext(
        env_id="e",
        session_id="s",
        question="问题",
        prepared=prepared,
        mentions=[{"asset_id": "11111111-1111-1111-1111-111111111111", "name": "文献"}],
        attachments=[
            AttachmentMeta(
                "a_img.png", ".loomvec/uploads/s/a_img.png", "img.png", 3, "image/png", True
            ),
            AttachmentMeta(
                "b_doc.pdf", ".loomvec/uploads/s/b_doc.pdf", "doc.pdf", 3, "application/pdf", False
            ),
        ],
    )
    blocks = await build_prompt_blocks(client, ws_id, ctx)
    async with client.open_turn("ses_1", blocks) as events:
        [m async for m in events]
    async with client.open_events("ses_1", after_seq=5, follow=False) as events:
        [m async for m in events]
    await client.cancel_turn("ses_1")
    await client.get_turn("ses_1", "trn_1")
    await client.list_messages("ses_1", cursor="trn_0", limit=50)
    await client.all_messages("ses_1")
    await client.tree(ws_id, "projects/p", 8)
    await client.mkdir(ws_id, "projects/p")
    await client.stat_file(ws_id, "a/b.txt")
    await client.put_file(ws_id, "a/b.txt", b"abc", size=3)

    async def chunks():
        yield b"abc"

    await client.put_file(ws_id, "a/c.txt", chunks(), size=3)
    await client.read_file(ws_id, "a/b.txt", max_bytes=10)
    return rec.requests


@pytest.fixture(scope="module")
async def requests_made() -> list[httpx.Request]:
    return await _exercise_all()


def test_every_declared_endpoint_exists_in_snapshot():
    for ep in ENDPOINTS.values():
        if not ep.in_openapi:
            continue
        assert ep.path in PATHS, f"{ep.name}: {ep.path} 不在网关 openapi 快照里"
        assert ep.method.lower() in PATHS[ep.path], f"{ep.name}: 快照里 {ep.path} 没有 {ep.method}"


async def test_all_declared_endpoints_are_exercised(requests_made):
    hit = {ep.name for r in requests_made if (ep := _match_endpoint(r))}
    assert hit == set(ENDPOINTS), f"未被走到的端点：{set(ENDPOINTS) - hit}"


async def test_every_request_matches_a_declared_endpoint(requests_made):
    for r in requests_made:
        assert _match_endpoint(r) is not None, f"未登记的请求 {r.method} {r.url.path}"


async def test_request_bodies_match_snapshot_schema(requests_made):
    checked = 0
    for r in requests_made:
        ep = _match_endpoint(r)
        if ep is None or not ep.in_openapi:
            continue
        schema = _body_schema(ep.method, ep.path)
        if schema is None:
            assert not r.content or r.method == "PUT", f"{ep.name} 快照无请求体却发了 body"
            continue
        errs = validate(schema, json.loads(r.content))
        assert not errs, f"{ep.name} 请求体不符合快照 schema：{errs}"
        checked += 1
    assert checked == 6  # workspace、instructions、session×2、patch、turn


async def test_query_params_are_declared_in_snapshot(requests_made):
    seen: set[str] = set()
    for r in requests_made:
        ep = _match_endpoint(r)
        if ep is None or not ep.in_openapi or not r.url.params:
            continue
        declared = {
            p["name"]
            for p in PATHS[ep.path][ep.method.lower()].get("parameters", [])
            if p["in"] == "query"
        }
        for name in r.url.params:
            assert name in declared, f"{ep.name} 带了快照未声明的查询参数 {name}"
            seen.add(name)
    assert {
        "external_ref",
        "after_seq",
        "follow",
        "limit",
        "cursor",
        "path",
        "depth",
        "stat",
    } <= seen


def _resp_props(ep_name: str, status: str) -> set[str]:
    ep = ENDPOINTS[ep_name]
    resp = PATHS[ep.path][ep.method.lower()]["responses"][status]
    schema = resp["content"]["application/json"]["schema"]
    if "$ref" in schema:
        schema = SNAPSHOT["components"]["schemas"][schema["$ref"].split("/")[-1]]
    return set(schema.get("properties", {}))


def test_response_fields_read_by_client_exist_in_snapshot():
    """客户端读取的响应字段必须在快照的响应 schema 里。"""
    assert "id" in _resp_props("workspace_create", "201")
    assert {"id", "external_ref", "status"} <= _resp_props("workspace_create", "201")
    assert "items" in _resp_props("workspace_list", "200")
    assert "content" in _resp_props("instructions_get", "200")
    assert "id" in _resp_props("session_create", "201")
    assert "cancelled" in _resp_props("turn_cancel", "200")
    assert {"items", "next_cursor"} <= _resp_props("messages", "200")
    assert {"entries", "truncated"} <= _resp_props("tree", "200")
    assert "usage" in _resp_props("turn_get", "200")


async def test_prompt_blocks_match_gateway_content_block_shapes(requests_made):
    """openapi 里 turns 的 content 只声明为 [object]；块结构按网关 prompt.ts 的 zod 规则校验。"""
    required = {
        "text": {"text"},
        "image": {"data", "mimeType"},
        "resource_link": {"uri", "name"},
    }
    turn_req = next(
        r for r in requests_made if r.method == "POST" and r.url.path.endswith("/turns")
    )
    body = json.loads(turn_req.content)
    assert body["stream"] is True
    kinds = set()
    for block in body["content"]:
        assert block["type"] in required, block
        assert required[block["type"]] <= set(block), block
        kinds.add(block["type"])
    assert kinds == {"text", "image", "resource_link"}
    link = next(b for b in body["content"] if b["type"] == "resource_link")
    assert link["uri"].startswith("workspace:///.loomvec/uploads/")


def test_permission_policy_matches_snapshot_schema():
    schema = _body_schema("POST", "/v1/workspaces/{id}/sessions")["properties"]["permission_policy"]
    assert validate(schema, build_permission_policy()) == []


def test_validator_actually_rejects_bad_bodies():
    """校验器本身要能报错（防止「永远通过」）。"""
    schema = _body_schema("POST", "/v1/workspaces/{id}/sessions")
    assert validate(schema, {"agent": "dsh", "bogus": 1})
    assert validate(schema, {"permission_policy": {"default": "maybe"}})
    assert validate(_body_schema("PUT", "/v1/workspaces/{id}/instructions"), {})  # 缺 content
