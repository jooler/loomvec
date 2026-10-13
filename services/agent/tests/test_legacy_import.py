"""L2 迁移：旧 dsh JSONL → 网关 import 体、workspace 文件遍历、迁移脚本的网关交互。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from loomvec.agent.gateway.history import fold_history
from loomvec.agent.gateway.legacy_import import (
    MAX_ITEMS_PER_TURN,
    MAX_TURNS,
    build_import_body,
    build_turns,
    carried_instructions,
    is_unmodified_legacy_agents_md,
    iso_z,
    iter_workspace_files,
    load_session_messages,
    open_regular_nofollow,
    strip_legacy_prefixes,
)
from loomvec.agent.mentions import build_mention_block
from loomvec.agent.sessions.transcript import parse_transcript

from ._gw import error_response, json_response, make_client
from .test_agent_core import LOGICAL, _fixture_lines, _write_fixture
from .test_gateway_contract import PATHS, validate

ASSET = "11111111-1111-1111-1111-111111111111"
SEARCH = "mcp__loomvec__search_knowledge"
CREATED = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _body(tmp_path: Path, lines: list[dict], **kw):
    log = _write_fixture(tmp_path, 0, lines)
    return build_import_body(
        parse_transcript(log),
        import_key=LOGICAL,
        title=kw.get("title", "标题"),
        cwd=kw.get("cwd", ""),
        model=kw.get("model"),
        created_at=CREATED,
        metadata={"loomvec_session_id": LOGICAL},
    )


def _import_schema() -> dict:
    op = PATHS["/admin/v1/workspaces/{id}/sessions:import"]["post"]
    return op["requestBody"]["content"]["application/json"]["schema"]


def test_iso_z():
    assert iso_z("2026-09-16T00:00:01Z") == "2026-09-16T00:00:01.000Z"
    assert iso_z("2026-09-16T08:00:01.123456+08:00") == "2026-09-16T00:00:01.123Z"
    assert iso_z("2026-09-16T00:00:01") == "2026-09-16T00:00:01.000Z"  # 无时区按 UTC
    assert iso_z("") is None and iso_z(None) is None and iso_z("昨天") is None
    # 真实 dsh JSONL：毫秒级 epoch（整数或其字符串形式）；秒级也接受
    assert iso_z(1789830350980) == "2026-09-19T15:05:50.980Z"
    assert iso_z("1789830350980") == "2026-09-19T15:05:50.980Z"
    assert iso_z(1789830350) == "2026-09-19T15:05:50.000Z"
    # 不是时间的数字：范围外一律 None；紧凑日期走 ISO 解析而不是被当成 epoch
    for bad in (-5, 0, 5e10, 1e14, 1e18, "nan", "1.5.2", "99999999999999999999", True, "2026"):
        assert iso_z(bad) is None, bad
    assert iso_z("20260916") == "2026-09-16T00:00:00.000Z"


def test_body_maps_turn_items_and_matches_gateway_schema(tmp_path):
    body = _body(
        tmp_path,
        _fixture_lines("什么是 X？", "X 是 [1] 这样", True),
        cwd="projects/p1",
        model="deepseek-v4",
    )
    assert body is not None
    assert validate(_import_schema(), body) == []
    assert body["agent"] == "dsh" and body["import_key"] == LOGICAL
    assert body["cwd"] == "projects/p1" and body["model"] == "deepseek-v4"
    assert body["created_at"] == "2026-09-01T08:00:00.000Z"
    (turn,) = body["turns"]
    assert turn["prompt"] == [{"type": "text", "text": "什么是 X？"}]
    assert turn["status"] == "completed" and turn["stop_reason"] == "completed"
    assert [i["type"] for i in turn["items"]] == ["tool", "thought", "text"]
    tool = turn["items"][0]
    assert tool["tool_call_id"] == "c1" and tool["tool_name"] == SEARCH
    assert tool["status"] == "completed"
    assert json.loads(tool["raw_output"])["ref_items"][0]["unit_id"] == "u1"
    assert turn["items"][2]["text"] == "X 是 [1] 这样"


def test_imported_history_round_trips_through_fold_history(tmp_path):
    """导入体 → 网关消息视图（按网关语义铺开）→ fold_history：问答、引用、@ 文献都能还原。"""
    mention = {"asset_id": ASSET, "name": "手册.pdf"}
    lines = _fixture_lines("原问题", "答案 [1]", True)
    lines[0]["data"]["message"]["content"][0]["text"] = (
        build_mention_block([mention]) + "\n\n原问题"
    )
    body = _body(tmp_path, lines)
    assert body is not None
    (turn,) = body["turns"]

    view: list[dict] = [{"role": "user", "turn_id": "t1", "content": turn["prompt"]}]
    for item in turn["items"]:
        if item["type"] == "text":
            view.append({"role": "assistant", "turn_id": "t1", "text": item["text"]})
        elif item["type"] == "tool":
            view.append(
                {
                    "role": "tool",
                    "turn_id": "t1",
                    "tool_call_id": item["tool_call_id"],
                    "tool_name": item["tool_name"],
                    "status": item["status"],
                    "raw_output": item["raw_output"],
                }
            )
    user, assistant = fold_history(view)
    assert user["content"] == "原问题" and user["mentions"] == [mention]
    assert assistant["content"] == "答案 [1]"
    assert assistant["citations"][0]["unit_id"] == "u1"
    assert assistant["tool_calls"][0]["tool"] == SEARCH


def test_failed_cancelled_and_dangling_turns(tmp_path):
    def user(text):
        return {
            "type": "user/message",
            "data": {"message": {"content": [{"type": "text", "text": text}]}},
        }

    def answer(text, turn):
        return {
            "type": "assistant/message",
            "data": {
                "turn": turn,
                "step": 1,
                "message": {"id": f"m{turn}", "content": [{"type": "text", "text": text}]},
            },
        }

    lines = [
        user("q1"),
        answer("a1", 1),
        {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "error"}}},
        user("q2"),  # 无任何 assistant 输出：被杀
        user("q3"),
        answer("a3", 3),
        {"type": "turn/end", "data": {"turn": 3, "reason": {"kind": "completed"}}},
    ]
    body = _body(tmp_path, lines)
    assert body is not None
    assert validate(_import_schema(), body) == []
    t1, t2, t3 = body["turns"]
    assert (t1["status"], t1["error"]["code"]) == ("failed", "legacy_error")
    assert (t2["status"], t2["items"]) == ("cancelled", [])
    assert t3["status"] == "completed"


def test_empty_session_returns_none_and_keeps_most_recent_turns(tmp_path):
    assert (
        build_import_body(
            [], import_key="k", title="t", cwd="", model=None, created_at=None, metadata={}
        )
        is None
    )
    log = _write_fixture(tmp_path, 0, _fixture_lines("q", "a", False) * 3)
    msgs = parse_transcript(log)
    assert len(build_turns(msgs)) == 3


def test_multiple_physical_generations_are_concatenated(tmp_path):
    _write_fixture(tmp_path, 0, _fixture_lines("第一问", "第一答", False))
    _write_fixture(tmp_path, 1, _fixture_lines("第二问", "第二答", False))
    msgs = load_session_messages(tmp_path / "sessions", tmp_path / "ws", LOGICAL)
    prompts = [t["prompt"][0]["text"] for t in build_turns(msgs)]
    assert prompts == ["第一问", "第二问"]


def test_iter_workspace_files_skips_hidden_symlinks_and_root_agents_md(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "projects/p1/.git").mkdir(parents=True)
    (ws / ".loomvec/uploads/s1").mkdir(parents=True)
    (ws / ".loomvec/other").mkdir(parents=True)
    (ws / ".dsh/skills").mkdir(parents=True)
    (ws / "AGENTS.md").write_text("x")
    (ws / "report.md").write_text("hello")
    (ws / "projects/p1/a.txt").write_text("")
    (ws / "projects/p1/.hidden").write_text("x")
    (ws / "projects/p1/.git/config").write_text("x")
    (ws / ".loomvec/uploads/s1/f.pdf").write_bytes(b"1234")
    (ws / ".loomvec/other/o.txt").write_text("x")
    (ws / ".dsh/skills/s.md").write_text("x")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws / "link.txt").symlink_to(outside)
    (ws / "linkdir").symlink_to(tmp_path)

    got = {f.rel: f.size for f in iter_workspace_files(ws)}
    assert got == {
        "report.md": 5,
        "projects/p1/a.txt": 0,
        ".loomvec/uploads/s1/f.pdf": 4,
    }
    assert list(iter_workspace_files(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# 迁移脚本的网关交互
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def script():
    path = Path(__file__).resolve().parents[3] / "scripts" / "migrate_agent_to_gateway.py"
    spec = importlib.util.spec_from_file_location("migrate_agent_to_gateway", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_sync_files_uploads_missing_and_skips_same_size(tmp_path, script):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "same.txt").write_text("abc")
    (ws / "new.txt").write_text("hello")
    (ws / "empty.txt").write_text("")
    (ws / "bad.txt").write_text("zz")
    puts: list[tuple[str, bytes]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v1/workspaces/w1/files/")
        if req.method == "GET":  # stat
            if path == "same.txt":
                return json_response(200, {"type": "file", "size": 99})  # 大小不同也不覆盖
            return error_response(404, "not_found")
        if path == "bad.txt":
            return error_response(413, "quota_exceeded")
        puts.append((path, req.content))
        return json_response(200, {"path": path})

    client, _ = make_client(handler)
    stats = script.Stats()
    await script.sync_files(client, "w1", ws, stats, dry_run=False)
    assert sorted(puts) == [("empty.txt", b""), ("new.txt", b"hello")]
    assert (stats.files_uploaded, stats.files_skipped, stats.files_failed) == (2, 1, 1)
    # --overwrite：同名文件也覆盖，且不再 stat
    puts.clear()
    stats2 = script.Stats()
    await script.sync_files(client, "w1", ws, stats2, dry_run=False, overwrite=True)
    assert ("same.txt", b"abc") in puts and stats2.files_skipped == 0
    assert stats.bytes_uploaded == 5 and "bad.txt" in stats.failures[0]


async def test_sync_files_dry_run_touches_nothing(tmp_path, script):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "a.txt").write_text("12345")
    client, rec = make_client(lambda r: json_response(500, {}))
    stats = script.Stats()
    await script.sync_files(client, "w1", ws, stats, dry_run=True)
    assert rec.requests == [] and stats.files_uploaded == 1 and stats.bytes_uploaded == 5


async def test_import_session_uses_admin_token_and_does_not_leak_content(script):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.headers["authorization"] == "Bearer good":
            return json_response(201, {"id": "ses_9"})
        return error_response(401, "unauthorized", "secret-body-echo")

    http = httpx.AsyncClient(base_url="http://gw.test", transport=httpx.MockTransport(handler))
    body = {"turns": [{"prompt": [{"type": "text", "text": "机密问题"}]}]}
    assert await script.import_session(http, "good", "w1", body) == ("ses_9", True)
    assert seen[0].url.path == "/admin/v1/workspaces/w1/sessions:import"
    with pytest.raises(RuntimeError) as ei:
        await script.import_session(http, "bad-token-xyz", "w1", body)
    msg = str(ei.value)
    assert "401" in msg and "机密问题" not in msg and "bad-token-xyz" not in msg
    await http.aclose()


async def test_user_modified_agents_md_is_carried_over(tmp_path, script):
    from loomvec.agent.gateway.service import OwnerContext
    from loomvec.agent.runtime.envs import AGENTS_MD_WORKSPACE

    owner = OwnerContext(
        user_id="u", tenant_id=None, tenant_name="某公司", user_display_name="张三"
    )
    ws = tmp_path / "workspace"
    ws.mkdir()
    puts: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return json_response(200, {"content": "<!-- loomvec-template: abc -->\nold"})
        puts.append(json.loads(req.content))
        return json_response(200, {})

    client, _ = make_client(handler)
    agents = ws / "AGENTS.md"
    # 未改动的旧渲染：交给模板，不搬运
    agents.write_text(AGENTS_MD_WORKSPACE.format(tenant_name="某公司", user_display_name="张三"))
    assert await script.sync_user_agents_md(client, "w1", ws, owner, dry_run=False) is False
    # 用户改写过：搬运
    agents.write_text("# 我的规则\n只回答 X")
    assert await script.sync_user_agents_md(client, "w1", ws, owner, dry_run=False) is True
    assert puts and "我的规则" in json.dumps(puts[0], ensure_ascii=False)
    assert await script.sync_user_agents_md(client, "w1", ws, owner, dry_run=True) is True
    assert len(puts) == 1  # dry-run 不写


def test_items_over_limit_keep_the_final_answer(tmp_path):
    big = [{"type": "text", "text": f"t{i}"} for i in range(MAX_ITEMS_PER_TURN + 5)]
    from loomvec.agent.sessions.transcript import TranscriptMessage

    user = TranscriptMessage(role="user", content="q")
    steps = [TranscriptMessage(role="assistant", content=b["text"]) for b in big]
    (turn,) = build_turns([user, *steps])
    assert len(turn["items"]) == MAX_ITEMS_PER_TURN
    assert turn["items"][-1]["text"] == f"t{MAX_ITEMS_PER_TURN + 4}"


def test_turn_count_over_limit_keeps_most_recent():
    from loomvec.agent.sessions.transcript import TranscriptMessage

    msgs = []
    for i in range(MAX_TURNS + 2):
        msgs += [
            TranscriptMessage(role="user", content=f"q{i}"),
            TranscriptMessage(role="assistant", content="a"),
        ]
    turns = build_turns(msgs)
    assert len(turns) == MAX_TURNS and turns[-1]["prompt"][0]["text"] == f"q{MAX_TURNS + 1}"
    assert turns[0]["prompt"][0]["text"] == "q2"


def test_loomvec_root_files_and_non_utf8_names(tmp_path):
    ws = tmp_path / "workspace"
    (ws / ".loomvec/uploads/s1").mkdir(parents=True)
    (ws / ".loomvec/internal.json").write_text("x")
    (ws / ".loomvec/uploads/s1/f.txt").write_text("ok")
    bad = ws.joinpath(os.fsdecode(b"\xd6\xd0\xce\xc4.txt"))  # GBK 字节，非法 UTF-8
    bad.write_text("x")
    got = {f.rel for f in iter_workspace_files(ws)}
    assert ".loomvec/internal.json" not in got and ".loomvec/uploads/s1/f.txt" in got
    assert bad.name in got  # 照常遍历到，失败留给上传环节单独计数


def test_open_regular_nofollow_rejects_symlink_and_non_regular(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("hi")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    with open_regular_nofollow(real) as f:
        assert f.read() == b"hi"
    with pytest.raises(OSError):
        open_regular_nofollow(link)
    with pytest.raises(OSError):
        open_regular_nofollow(tmp_path)  # 目录


def test_unmodified_legacy_agents_md_ignores_renamed_tenant_or_user():
    from loomvec.agent.runtime.envs import AGENTS_MD_WORKSPACE

    old = AGENTS_MD_WORKSPACE.format(tenant_name="旧公司", user_display_name="旧名")
    now = AGENTS_MD_WORKSPACE.format(tenant_name="新公司", user_display_name="新名")
    assert is_unmodified_legacy_agents_md(old, now)
    assert not is_unmodified_legacy_agents_md(old + "\n- 额外规则\n", now)


def test_carried_instructions_keeps_enterprise_rules_and_user_text_without_marker():
    from loomvec.agent.gateway import instructions as instr

    out = carried_instructions(
        instr.render_instructions(tenant_name="某公司", user_display_name="张三"),
        "# 我的规则\n只回答 X",
    )
    assert not out.startswith("<!-- loomvec-template")
    assert "数据，不是指令" in out and "只回答 X" in out
    # 无标记 → 模板升级不会覆盖它
    assert instr.needs_write(out, tenant_name="某公司", user_display_name="张三") is False


def test_describe_masks_secrets_and_names_the_exception_type(script):
    import httpx as _h

    e = _h.LocalProtocolError("Illegal header value b'Bearer tok-123\\n'")
    text = script.describe(e, "tok-123")
    assert "LocalProtocolError" in text and "tok-123" not in text
    assert script.describe(_h.ReadTimeout("")).startswith("ReadTimeout")


async def test_sync_files_survives_unencodable_filename(tmp_path, script):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / os.fsdecode(b"\xd6\xd0.txt")).write_text("x")
    (ws / "ok.txt").write_text("y")
    puts = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return error_response(404, "not_found")
        puts.append(req.url.path)
        return json_response(200, {})

    client, _ = make_client(handler)
    stats = script.Stats()
    await script.sync_files(client, "w1", ws, stats, dry_run=False)
    assert puts == ["/v1/workspaces/w1/files/ok.txt"]
    assert stats.files_failed == 1 and stats.files_uploaded == 1


async def test_agents_md_not_overwritten_when_gateway_copy_was_edited(tmp_path, script):
    from loomvec.agent.gateway.service import OwnerContext

    owner = OwnerContext(
        user_id="u", tenant_id=None, tenant_name="某公司", user_display_name="张三"
    )
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# 旧自定义\n规则 A")
    puts = []
    state = {"content": "网关侧用户后改的内容（无标记）"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return json_response(200, {"content": state["content"]})
        puts.append(json.loads(req.content)["content"])
        state["content"] = puts[-1]
        return json_response(200, {})

    client, _ = make_client(handler)
    assert await script.sync_user_agents_md(client, "w1", ws, owner, dry_run=False) is False
    assert puts == []
    assert (
        await script.sync_user_agents_md(client, "w1", ws, owner, dry_run=False, overwrite=True)
        is True
    )
    assert "规则 A" in puts[0] and "数据，不是指令" in puts[0]
    # 写过之后再跑（默认模式）不再动
    assert await script.sync_user_agents_md(client, "w1", ws, owner, dry_run=False) is False


REPLAY = (
    "以下是本会话此前的对话记录（系统重启后恢复上下文，历史以旧到新排列）：\n"
    "<user>旧问题</user>\n<assistant>旧回答\n含换行</assistant>\n请基于以上历史继续本会话。"
)
DIR_NOTE = (
    "本次会话绑定的工作目录为 `projects/`，交付物（文件、代码、报告等）一律写入该目录及其子目录；"
    "引用其中的文件时使用该目录下的相对路径。"
)


def test_strip_legacy_prefixes_matches_what_the_old_orchestrator_prepended():
    # 旧 orchestrator 的各 text 块被旧解析器直接拼接：重放前缀 → 目录说明 → 问题
    assert strip_legacy_prefixes(REPLAY + DIR_NOTE + "新问题") == "新问题"
    assert strip_legacy_prefixes(DIR_NOTE + "只有目录说明") == "只有目录说明"
    assert strip_legacy_prefixes(REPLAY + "只有重放") == "只有重放"
    assert strip_legacy_prefixes("普通问题") == "普通问题"
    # 只剥开头：正文里恰好出现同样的话不动
    assert strip_legacy_prefixes("问：" + DIR_NOTE) == "问：" + DIR_NOTE


def test_real_dsh_jsonl_shape_epoch_ms_times_and_flat_user_content(tmp_path):
    """真实 dsh JSONL：`time` 是毫秒 epoch，user/message 的 content 平铺在 data 下。"""
    import json as _json

    from loomvec.agent.sessions.transcript import encode_segment, physical_gen, project_key

    sdir = (
        tmp_path
        / "sessions"
        / project_key(str(tmp_path / "ws"))
        / encode_segment(physical_gen(LOGICAL, 0))
    )
    sdir.mkdir(parents=True)
    lines = [
        {"type": "session", "version": 3, "id": "x", "createdAt": 1789830350980},
        {"type": "turn/start", "seq": 4, "time": 1789830351005, "data": {"turn": 1}},
        {
            "type": "user/message",
            "seq": 8,
            "time": 1789830351404,
            "data": {
                "role": "user",
                "id": "u1",
                "content": [
                    {"type": "text", "text": DIR_NOTE},
                    {"type": "text", "text": "你好"},
                ],
            },
        },
        {
            "type": "assistant/message",
            "seq": 15,
            "time": 1789830353255,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {"id": "m", "content": [{"type": "text", "text": "你好！"}]},
                "usage": {"inputTokens": 8318, "outputTokens": 74},
            },
        },
        {
            "type": "turn/end",
            "seq": 17,
            "time": 1789830353257,
            "data": {"turn": 1, "reason": {"kind": "completed"}},
        },
    ]
    (sdir / "session.v3.jsonl").write_text(
        "\n".join(_json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8"
    )
    msgs = load_session_messages(tmp_path / "sessions", tmp_path / "ws", LOGICAL)
    (turn,) = build_turns(msgs)
    assert turn["prompt"] == [{"type": "text", "text": "你好"}]
    assert turn["started_at"] == "2026-09-19T15:05:51.404Z"
    assert turn["ended_at"] == "2026-09-19T15:05:53.255Z"
    assert validate(_import_schema(), {"turns": [turn]}) == []


def test_nested_replay_prefixes_from_repeated_restarts_are_fully_stripped():
    """runtime 重启多次的旧会话：重放内容取自带前缀的用户消息，前缀是嵌套的。"""
    inner = REPLAY
    outer = (
        "以下是本会话此前的对话记录（系统重启后恢复上下文，历史以旧到新排列）：\n"
        f"<user>{inner}{DIR_NOTE}Q1</user>\n<assistant>A1</assistant>\n请基于以上历史继续本会话。"
    )
    assert strip_legacy_prefixes(outer + DIR_NOTE + "Q2") == "Q2"
    assert strip_legacy_prefixes(outer + "Q2") == "Q2"


def test_history_drops_replay_blocks_but_keeps_user_text_that_mentions_the_footer():
    from loomvec.agent.gateway.history import fold_history

    msg = {
        "role": "user",
        "turn_id": "t",
        "content": [
            {"type": "text", "text": REPLAY},
            {"type": "text", "text": "请基于以上历史继续本会话。这句话本身是我的提问"},
        ],
    }
    (entry,) = fold_history([msg])
    assert entry["content"] == "请基于以上历史继续本会话。这句话本身是我的提问"
