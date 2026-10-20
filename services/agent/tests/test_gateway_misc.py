"""网关适配层的纯逻辑：历史折叠、工作区树展平、用量折叠、附件、审批策略、配置读取。"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from loomvec.agent.approvals import ALLOWED_TOOL_EXACT, build_permission_policy, decide
from loomvec.agent.attachments import (
    AttachmentError,
    resolve_attachment,
    sanitize_filename,
    store_attachment,
)
from loomvec.agent.config import (
    AgentRuntimeConfig,
    load_gateway_connection,
)
from loomvec.agent.gateway.files import MAX_TREE_ITEMS, flatten_tree
from loomvec.agent.gateway.history import fold_history, replay_context
from loomvec.agent.gateway.usage import fold_usages, usage_to_event
from loomvec.agent.mentions import build_mention_block
from loomvec.core.config import AgentSettings, Settings

from ._gw import SEARCH_RESULT, error_response, json_response, make_client

SEARCH = "mcp__loomvec__search_knowledge"
ASSET = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# 历史：网关消息视图 → AgentMessageOut 形状
# ---------------------------------------------------------------------------


def _tool_msg(call_id, name, output, turn="t1", status="completed"):
    return {
        "role": "tool",
        "turn_id": turn,
        "seq": 3,
        "ts": "2026-10-13T00:00:03Z",
        "tool_call_id": call_id,
        "tool_name": name,
        "title": name,
        "kind": "other",
        "status": status,
        "raw_input": {},
        "content": [{"type": "content", "content": {"type": "text", "text": output}}],
        "raw_output": output,
    }


def test_fold_history_one_user_one_assistant_per_turn():
    mention = build_mention_block([{"asset_id": ASSET, "name": "文献 A"}])
    messages = [
        {
            "role": "user",
            "turn_id": "t1",
            "seq": 1,
            "ts": "2026-10-13T00:00:01Z",
            "content": [
                {"type": "text", "text": mention},
                {"type": "image", "mimeType": "image/png", "bytes": 10},  # 摘要：只留元信息
                {"type": "text", "text": "这篇讲了什么？"},
            ],
        },
        {"role": "thought", "turn_id": "t1", "seq": 2, "ts": "x", "text": "想"},
        _tool_msg("c1", SEARCH, json.dumps(SEARCH_RESULT)),
        {
            "role": "assistant",
            "turn_id": "t1",
            "seq": 4,
            "ts": "2026-10-13T00:00:04Z",
            "text": "依据[1]",
        },
        {"role": "assistant", "turn_id": "t1", "seq": 6, "ts": "x", "text": "，不是[9]"},
        {
            "role": "user",
            "turn_id": "t2",
            "seq": 7,
            "ts": "2026-10-13T00:01:00Z",
            "content": [{"type": "text", "text": "再问"}],
        },
        {
            "role": "assistant",
            "turn_id": "t2",
            "seq": 8,
            "ts": "2026-10-13T00:01:02Z",
            "text": "好的",
        },
    ]
    items = fold_history(messages)
    assert [i["role"] for i in items] == ["user", "assistant", "user", "assistant"]
    user, assistant = items[0], items[1]
    assert user["content"] == "这篇讲了什么？"
    assert user["mentions"] == [{"asset_id": ASSET, "name": "文献 A"}]
    assert user["created_at"] == "2026-10-13T00:00:01Z"
    # assistant：文本按序拼接（与实时 delta 拼接一致），越界引用标记剔除
    assert assistant["content"] == "依据[1]，不是"
    assert [c["unit_id"] for c in assistant["citations"]] == ["u1"]
    assert assistant["graph_evidence"] == SEARCH_RESULT["graph_evidence"]
    assert assistant["tool_calls"] == [
        {
            "call_id": "c1",
            "tool": SEARCH,
            "ok": True,
            "result_summary": json.dumps(SEARCH_RESULT)[:200],
        }
    ]
    assert items[3]["content"] == "好的" and items[3]["citations"] == []


def test_fold_history_failed_turn_and_context_reset():
    items = fold_history(
        [
            {
                "role": "user",
                "turn_id": "t1",
                "ts": "a",
                "content": [{"type": "text", "text": "q"}],
            },
            {
                "role": "system",
                "turn_id": "t1",
                "ts": "b",
                "kind": "turn_failed",
                "detail": {"code": "turn_timeout", "message": "超时"},
            },
            {"role": "system", "turn_id": None, "ts": "c", "kind": "context_reset", "detail": {}},
            {
                "role": "user",
                "turn_id": "t2",
                "ts": "d",
                "content": [{"type": "text", "text": "q2"}],
            },
            _tool_msg("c9", "Bash", "boom", turn="t2", status="failed"),
        ]
    )
    assert [i["role"] for i in items] == ["user", "assistant", "user", "assistant"]
    assert "超时" in items[1]["content"]
    # 只有工具调用没有文本的回合（被取消 / 失败）：仍保留 assistant 条目与 ok=False
    assert items[3]["content"] == "" and items[3]["tool_calls"][0]["ok"] is False


def test_fold_history_empty_and_thought_only():
    assert fold_history([]) == []
    assert fold_history([{"role": "thought", "turn_id": "t", "text": "x", "ts": "a"}]) == []


# ---------------------------------------------------------------------------
# 工作区树
# ---------------------------------------------------------------------------


def _e(path, type_="file", size=3, children=None):
    out = {"name": path.rsplit("/", 1)[-1], "path": path, "type": type_, "size": size}
    if children is not None:
        out["children"] = children
    return out


def _pairs(n: int, size: int = 10) -> list[dict]:
    items = []
    for i in range(n):
        items.append({"role": "user", "content": f"Q{i}" + "q" * size})
        items.append({"role": "assistant", "content": f"A{i}" + "a" * size})
    return items


def test_replay_context_keeps_whole_messages_newest_first_and_starts_on_user():
    items = _pairs(40)  # 80 条，超过 30 条上限
    note = replay_context(items)
    assert note.count("<user>") + note.count("<assistant>") <= 30
    assert "A39" in note and "Q0" not in note
    # 每条消息完整（不会从标签中间切开），且最早一条是 user
    assert note.count("<user>") == note.count("</user>")
    assert note.count("<assistant>") == note.count("</assistant>")
    first = note.split("\n")[1]
    assert first.startswith("<user>")


def test_replay_context_char_budget_drops_oldest_whole_messages():
    items = _pairs(10, size=1000)
    note = replay_context(items, max_messages=100, max_chars=5000)
    assert "A9" in note and "Q0" not in note
    # 每条约 1002 字符，5000 预算只放得下最新 4 条：A9,Q9,A8,Q8
    assert [t[:2] for _, t in _bodies(note)] == ["Q8", "A8", "Q9", "A9"]
    assert note.count("<user>") == note.count("</user>")
    assert len(note) < 5000 + 400  # 预算 + 标签与首尾说明


def _bodies(note: str) -> list[tuple[str, str]]:
    import re

    return re.findall(r"<(user|assistant)>(.*?)</\1>", note, flags=re.S)


def test_replay_context_message_cap_is_exact_and_starts_on_user():
    # 奇数上限：取最后 3 条是 A1,Q2,A2；开头 A1 是 assistant，被丢弃 → 只剩 Q2,A2
    # （上限多放宽 1 条、或「从用户开始」逻辑被删，都会让这里失败）
    note = replay_context(_pairs(3), max_messages=3)
    assert [role for role, _ in _bodies(note)] == ["user", "assistant"]
    assert "Q2" in note and "A2" in note and "A1" not in note and "Q1" not in note


def test_replay_context_drops_answer_whose_question_was_squeezed_out():
    items = [
        {"role": "user", "content": "Q" * 3000},
        {"role": "assistant", "content": "短回答"},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "content": "新回答"},
    ]
    # 预算放得下「短回答」，放不下它的提问 → 短回答不能孤零零地留在开头
    note = replay_context(items, max_chars=3 + 3 + 3 + 3 + 10)
    assert "短回答" not in note and "Q" * 10 not in note
    assert _bodies(note) == [("user", "新问题"), ("assistant", "新回答")]


def test_replay_context_clips_one_long_message_but_keeps_earlier_turns():
    long = "H" * 5000 + "M" * 5000 + "T" * 5000
    items = [
        {"role": "user", "content": "早先的问题"},
        {"role": "assistant", "content": "早先的回答"},
        {"role": "user", "content": "最新的问题"},
        {"role": "assistant", "content": long},
    ]
    note = replay_context(items)
    assert "早先的问题" in note and "最新的问题" in note
    assert "（中间省略）" in note and note.count("M") == 0
    assert note.count("H") == 2000 and note.count("T") == 1000
    assert "<assistant>H" in note and "T</assistant>" in note


def test_replay_context_always_keeps_latest_and_handles_empty():
    assert replay_context([]) == ""
    assert replay_context([{"role": "user", "content": ""}]) == ""
    note = replay_context([{"role": "user", "content": "x" * 99999}], max_chars=10)
    assert "<user>" in note and "（中间省略）" in note
    # 开头是 assistant 且没有对应提问时丢弃，但不把唯一一条丢光
    only_a = replay_context([{"role": "assistant", "content": "单独回答"}])
    assert "<assistant>单独回答</assistant>" in only_a


def test_flatten_tree_hides_hidden_segments_and_non_file_dir_entries():
    entries = [
        _e(".loomvec", "dir", children=[_e(".loomvec/uploads", "dir")]),
        _e("a", "dir", children=[_e("a/b.txt"), _e("a/.secret"), _e("a/ln", "symlink")]),
        _e("fifo", "other"),
        _e("z.md", size=7),
    ]
    items = flatten_tree("", entries)
    assert [(i["path"], i["type"], i["size"]) for i in items] == [
        ("a", "dir", 0),
        ("a/b.txt", "file", 3),
        ("z.md", "file", 7),
    ]
    assert set(items[0]) == {"path", "name", "type", "size"}


def test_flatten_tree_with_prefix_starts_with_prefix_node_itself():
    items = flatten_tree("proj/sub", [_e("proj/sub/f.py", size=5)])
    assert items[0] == {"path": "proj/sub", "name": "sub", "type": "dir", "size": 0}
    assert items[1]["path"] == "proj/sub/f.py"
    assert flatten_tree(".hidden/x", [_e(".hidden/x/f")]) == []


def test_flatten_tree_caps_at_500_items():
    entries = [_e(f"f{i:04d}.txt") for i in range(800)]
    assert len(flatten_tree("", entries)) == MAX_TREE_ITEMS == 500
    nested = [_e("d", "dir", children=entries)]
    assert len(flatten_tree("d", nested)) == 500


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------


def test_usage_shapes():
    assert usage_to_event(None) == {}
    one = usage_to_event({"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3})
    assert one["uncached_input_tokens"] == 1 and one["cache_read_tokens"] == 3
    assert one["cache_hit_rate"] == 0.75
    empty = fold_usages([None, None])
    assert empty["steps"] == 0 and empty["cache_hit_rate"] == 0.0


# ---------------------------------------------------------------------------
# 附件
# ---------------------------------------------------------------------------


def _cfg() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(settings=Settings(), agent=AgentSettings())


def test_sanitize_filename_keeps_extension_within_byte_budget():
    assert sanitize_filename("../../etc/pass wd.txt") == "pass_wd.txt"
    assert sanitize_filename("") == "attachment"
    long_cn = "很长的文件名" * 30 + ".pdf"
    out = sanitize_filename(long_cn)
    assert out.endswith(".pdf") and len(out.encode()) <= 200


async def test_store_attachment_uploads_raw_bytes_to_session_uploads_dir():
    client, rec = make_client(lambda r: json_response(200, {"path": "x", "size": 11}))
    meta = await store_attachment(
        _cfg(),
        client,
        "ws_1",
        "sess-uuid",
        "周报 2026.md",
        io.BytesIO(b"hello world"),
        "text/markdown",
    )
    req = rec.requests[0]
    assert req.method == "PUT" and req.content == b"hello world"
    assert req.headers["content-length"] == "11"
    assert meta.path.startswith(".loomvec/uploads/sess-uuid/")
    assert meta.attachment_id.endswith("_周报_2026.md") and meta.name == "周报_2026.md"
    assert (meta.size, meta.mime, meta.is_image) == (11, "text/markdown", False)
    assert (
        req.url.path.endswith("/v1/workspaces/ws_1/files/" + meta.path)
        or "%E5%91%A8" in req.url.raw_path.decode()
    )
    # 随机前缀防重名
    meta2 = await store_attachment(
        _cfg(), client, "ws_1", "sess-uuid", "周报 2026.md", io.BytesIO(b"x"), "text/markdown"
    )
    assert meta2.attachment_id != meta.attachment_id


async def test_store_attachment_validation():
    client, rec = make_client(lambda r: json_response(200, {}))
    with pytest.raises(AttachmentError, match="不支持的附件类型"):
        await store_attachment(
            _cfg(), client, "ws", "s", "a.exe", io.BytesIO(b"x"), "application/x-msdownload"
        )
    with pytest.raises(AttachmentError, match="为空"):
        await store_attachment(_cfg(), client, "ws", "s", "a.txt", io.BytesIO(b""), "text/plain")
    cfg = _cfg()
    big = io.BytesIO(b"x" * (cfg.agent.session.attachments.max_file_mb * 1024 * 1024 + 1))
    with pytest.raises(AttachmentError, match="上限"):
        await store_attachment(cfg, client, "ws", "s", "a.txt", big, "text/plain")
    assert rec.requests == []  # 校验失败不上传
    img = await store_attachment(
        _cfg(), client, "ws", "s", "p.png", io.BytesIO(b"\x89PNG"), "image/png; charset=x"
    )
    assert img.is_image and img.mime == "image/png"


async def test_resolve_attachment_uses_gateway_stat():
    aid = "0123456789ab_报告.pdf"

    def handler(request):
        assert request.url.params["stat"] == "1"
        assert request.url.path.endswith("/files/.loomvec/uploads/sess-uuid/" + aid) or (
            "%E6%8A%A5" in request.url.raw_path.decode()
        )
        return json_response(200, {"name": aid, "type": "file", "size": 1234})

    client, _ = make_client(handler)
    meta = await resolve_attachment(client, "ws_1", "sess-uuid", aid)
    assert meta is not None
    assert (meta.name, meta.size, meta.mime, meta.is_image) == (
        "报告.pdf",
        1234,
        "application/pdf",
        False,
    )
    assert meta.path == f".loomvec/uploads/sess-uuid/{aid}"


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "a/b",
        "0123456789ab_../x",
        "x" * 300,
        "",
        "ZZZZZZZZZZZZ_a.txt",
        ".hidden",
    ],
)
async def test_resolve_attachment_rejects_malicious_ids_without_calling_gateway(bad_id):
    client, rec = make_client(lambda r: json_response(200, {"type": "file", "size": 1}))
    assert await resolve_attachment(client, "ws_1", "s", bad_id) is None
    assert rec.requests == []


async def test_resolve_attachment_missing_or_not_a_file():
    client, _ = make_client(lambda r: error_response(404, "not_found"))
    assert await resolve_attachment(client, "ws", "s", "0123456789ab_a.txt") is None
    client, _ = make_client(lambda r: json_response(200, {"type": "dir", "size": 0}))
    assert await resolve_attachment(client, "ws", "s", "0123456789ab_a.txt") is None
    client, _ = make_client(lambda r: error_response(500, "internal"))
    with pytest.raises(Exception, match="500"):
        await resolve_attachment(client, "ws", "s", "0123456789ab_a.txt")


# ---------------------------------------------------------------------------
# 审批策略（approvals.py 矩阵 → 网关 PermissionPolicy）
# ---------------------------------------------------------------------------


def test_permission_policy_translates_the_matrix():
    policy = build_permission_policy()
    assert policy["default"] == "reject"
    rules = policy["rules"]
    assert all(r["decision"] == "allow" for r in rules)
    assert rules[0] == {"match": {"tool_name": "mcp__loomvec__*"}, "decision": "allow"}
    assert {"match": {"kind": ["read", "search", "edit"]}, "decision": "allow"} in rules
    named = {r["match"]["tool_name"] for r in rules if set(r["match"]) == {"tool_name"}}
    assert named == {"mcp__loomvec__*"} | set(ALLOWED_TOOL_EXACT)
    # 破坏性 / 网络类 kind 不在放行列表里
    allowed_kinds = {k for r in rules for k in r["match"].get("kind", [])}
    assert not allowed_kinds & {"execute", "delete", "move", "fetch", "other"}
    assert len(rules) < 200


def test_permission_policy_agrees_with_decide_for_every_matrix_tool():
    """声明式矩阵与 decide() 同源：decide 放行的工具名，策略里都能按 tool_name 匹配到。"""
    import fnmatch

    def policy_allows(tool: str) -> bool:
        for r in build_permission_policy()["rules"]:
            name = r["match"].get("tool_name")
            if name and fnmatch.fnmatchcase(tool, name):
                return r["decision"] == "allow"
        return False

    for tool in [*ALLOWED_TOOL_EXACT, "mcp__loomvec__search_knowledge"]:
        assert decide(tool).action == "allow-once" and policy_allows(tool)
    for tool in ("bash", "web_fetch", "rm"):
        assert decide(tool).action == "reject-once" and not policy_allows(tool)


# ---------------------------------------------------------------------------
# 配置读取（system_config 敏感项 + 文件默认）
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, kv: dict[str, object]):
        self.kv = kv

    async def execute(self, stmt):
        return _Rows([SimpleNamespace(key=k, value=v) for k, v in self.kv.items()])


async def test_gateway_connection_reads_api_key_plaintext_with_no_file_fallback():
    agent = AgentSettings()
    conn = await load_gateway_connection(_FakeSession({}), agent)
    assert conn.api_key is None  # 无文件回落：空 = 未配置
    assert conn.url == "http://127.0.0.1:38095" and conn.request_timeout_s == 30.0
    assert conn.default_agent == "claude"

    conn = await load_gateway_connection(
        _FakeSession({"agent.gateway.api_key": "  acpgw_abc  "}), agent
    )
    assert conn.api_key == "acpgw_abc"  # 敏感项库里是明文（回显才脱敏），去首尾空白
    conn = await load_gateway_connection(_FakeSession({"agent.gateway.api_key": "   "}), agent)
    assert conn.api_key is None


async def test_gateway_connection_falls_back_to_local_gateway_key_file(tmp_path, monkeypatch):
    """本机网关（回环地址）：admin 没配 Key 时读状态目录里的 local-api-key；配了则以 admin 为准。"""
    key_file = tmp_path / "local-api-key"
    key_file.write_text("acpgw_local\n", encoding="utf-8")
    key_file.chmod(0o600)
    agent = AgentSettings(gateway={"state_dir": str(tmp_path)})

    conn = await load_gateway_connection(_FakeSession({}), agent)
    assert conn.api_key == "acpgw_local"
    conn = await load_gateway_connection(_FakeSession({"agent.gateway.api_key": "acpgw_db"}), agent)
    assert conn.api_key == "acpgw_db"

    # 远程网关不读本机文件
    remote = AgentSettings(gateway={"url": "https://gw.example.com", "state_dir": str(tmp_path)})
    assert (await load_gateway_connection(_FakeSession({}), remote)).api_key is None

    # 缺省状态目录跟随 XDG_DATA_HOME
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg").mkdir()
    (tmp_path / "xdg" / "acp-gateway").mkdir(mode=0o700)  # 与网关一致；umask 不应放宽它
    xdg_key = tmp_path / "xdg" / "acp-gateway" / "local-api-key"
    xdg_key.write_text("acpgw_xdg", encoding="utf-8")
    xdg_key.chmod(0o600)
    assert (await load_gateway_connection(_FakeSession({}), AgentSettings())).api_key == "acpgw_xdg"


async def test_gateway_connection_db_overrides_win_over_file_when_non_empty():
    agent = AgentSettings(
        default_agent="dsh",
        gateway={"models": {"claude": "file-claude", "dsh": None}},
        model={"name": "deepseek-chat"},
    )
    base = await load_gateway_connection(_FakeSession({}), agent)
    assert base.default_agent == "dsh"
    assert base.models == {"claude": "file-claude", "dsh": "deepseek-chat"}  # dsh 回落 model.name

    over = await load_gateway_connection(
        _FakeSession(
            {
                "agent.default_agent": "claude",
                "agent.gateway.models.claude": "db-claude",
                "agent.gateway.models.dsh": "deepseek-v4-pro",
            }
        ),
        agent,
    )
    assert over.default_agent == "claude"
    assert over.models == {"claude": "db-claude", "dsh": "deepseek-v4-pro"}

    # 非法 / 空值回落文件配置
    bad = await load_gateway_connection(
        _FakeSession({"agent.default_agent": "gpt", "agent.gateway.models.claude": ""}), agent
    )
    assert bad.default_agent == "dsh" and bad.models["claude"] == "file-claude"


def test_agent_settings_contract_defaults():
    s = AgentSettings()
    assert s.default_agent == "claude"
    assert s.gateway.url == "http://127.0.0.1:38095" and s.gateway.request_timeout_s == 30
    assert s.gateway.models.claude is None and s.gateway.models.dsh is None
    assert s.model_for("claude") is None and s.model_for("dsh") == s.model.name
    with pytest.raises(ValueError):
        AgentSettings(default_agent="gpt")
