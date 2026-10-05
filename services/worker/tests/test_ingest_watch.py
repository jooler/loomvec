"""目录监听入库：单层扫描过滤、PDF 完整性检查与关闭语义；LLM 建议新知识库的采纳闸门。"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest

from loomvec.core import knowledge_base_auto
from loomvec.core.config import IngestWatchSettings
from loomvec.core.errors import ValidationError
from loomvec.worker import ingest_watch, tasks


def test_list_candidates_is_single_level_and_filters(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 a")
    (tmp_path / "B.PDF").write_bytes(b"%PDF-1.4 b")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / ".syncing.pdf").write_bytes(b"partial")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "deep.pdf").write_bytes(b"%PDF-1.4 deep")
    (tmp_path / "dir.pdf").mkdir()  # 目录即使后缀匹配也不算文件
    outside = tmp_path.parent / f"secret-{uuid.uuid4().hex}.txt"
    outside.write_text("secret")
    (tmp_path / "innocent.pdf").symlink_to(outside)  # 软链接不跟随
    # 非 UTF-8 文件名（如 GBK 字节）：跳过而不是让整轮扫描失败
    with open(os.path.join(os.fsencode(tmp_path), "测试".encode("gbk") + b".pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4 gbk")

    try:
        found = ingest_watch.list_candidates(str(tmp_path), ["pdf"])
    finally:
        outside.unlink()

    assert sorted(f.name for f in found) == ["B.PDF", "a.pdf"]
    a = next(f for f in found if f.name == "a.pdf")
    assert a.path == str(tmp_path / "a.pdf")
    assert a.size == len(b"%PDF-1.4 a")


def test_read_regular_file_refuses_symlink(tmp_path):
    target = tmp_path / "real.pdf"
    target.write_bytes(b"%PDF-1.4")
    (tmp_path / "link.pdf").symlink_to(target)
    data, _st = ingest_watch._read_regular_file(str(target))
    assert data == b"%PDF-1.4"
    with pytest.raises(OSError):
        ingest_watch._read_regular_file(str(tmp_path / "link.pdf"))


def test_check_pdf():
    ingest_watch._check_pdf(b"%PDF-1.7\n...\n%%EOF\n")
    with pytest.raises(ValidationError, match="%PDF-"):
        ingest_watch._check_pdf(b"<html>not a pdf</html>%%EOF")
    with pytest.raises(ValidationError, match="%%EOF"):
        ingest_watch._check_pdf(b"%PDF-1.7\n" + b"x" * 4096)


def test_scan_watch_dir_noop_when_disabled(monkeypatch):
    import asyncio

    # 显式注入空配置：不读本机 config/loomvec.json，避免测试触达真实监听目录
    disabled = SimpleNamespace(ingest_watch=IngestWatchSettings())
    deps = SimpleNamespace(settings=disabled)
    assert asyncio.run(ingest_watch.scan_watch_dir(deps)) == {"status": "disabled"}

    async def must_not_build():
        raise AssertionError("关闭时不应装配依赖")

    monkeypatch.setattr(tasks, "get_settings", lambda: disabled)
    monkeypatch.setattr(tasks, "build_deps", must_not_build)
    assert tasks.ingest_watch_scan.apply().get() == {"status": "disabled"}


def test_auto_tag_prompt_respects_switch():
    assert "new_knowledge_base" in tasks._auto_tag_system_prompt(True)
    no_new = tasks._auto_tag_system_prompt(False)
    assert "new_knowledge_base" not in no_new
    assert '"tags"' in no_new


async def test_suggested_knowledge_base_gating(monkeypatch):
    calls: list[dict] = []
    tag_id = uuid.uuid4()

    async def fake_create_system_tag(session, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=tag_id)

    monkeypatch.setattr(knowledge_base_auto, "create_system_tag", fake_create_system_tag)
    parent = uuid.uuid4()
    catalog = {parent}

    # 非 dict / 空名 / 置信度不足：不建
    for bad in (None, "x", {"name": " ", "confidence": 0.9}, {"name": "单细胞", "confidence": 0.5}):
        assert await tasks._create_suggested_knowledge_base(None, None, bad, catalog) is None
    assert calls == []

    # 上级不在目录白名单内 → 挂根；pending 上限透传
    got = await tasks._create_suggested_knowledge_base(
        None,
        None,
        {"name": "单细胞测序", "confidence": 0.9, "parent_id": str(uuid.uuid4())},
        catalog,
        max_pending=7,
    )
    assert got == (tag_id, 0.9)
    assert calls[-1]["parent_id"] is None
    assert calls[-1]["name"] == "单细胞测序"
    assert calls[-1]["max_pending"] == 7

    # 上级在白名单内 → 透传
    await tasks._create_suggested_knowledge_base(
        None,
        None,
        {"name": "单细胞测序", "confidence": 0.8, "parent_id": str(parent), "reason": "r"},
        catalog,
    )
    assert calls[-1]["parent_id"] == parent
    assert calls[-1]["reason"] == "r"


async def test_suggested_knowledge_base_suppressed(monkeypatch):
    async def fake_create_system_tag(session, **kwargs):
        return None  # 同名节点曾被人工删除 / pending 已达上限

    monkeypatch.setattr(knowledge_base_auto, "create_system_tag", fake_create_system_tag)
    suggestion = {"name": "单细胞测序", "confidence": 0.9}
    assert await tasks._create_suggested_knowledge_base(None, None, suggestion, set()) is None
