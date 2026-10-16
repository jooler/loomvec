"""目录监听入库：有限层扫描与过滤、PDF 完整性检查与关闭语义；LLM 建议新知识库的采纳闸门。"""

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
        found, complete = ingest_watch.list_candidates(str(tmp_path), ["pdf"])
    finally:
        outside.unlink()

    assert complete
    assert sorted(f.name for f in found) == ["B.PDF", "a.pdf"]
    a = next(f for f in found if f.name == "a.pdf")
    assert a.path == str(tmp_path / "a.pdf")
    assert a.size == len(b"%PDF-1.4 a")


def test_list_candidates_descends_up_to_max_depth(tmp_path):
    (tmp_path / "top.pdf").write_bytes(b"%PDF-1.4")
    d1 = tmp_path / "01_主题"
    d2 = d1 / "子类"
    d3 = d2 / "更深"
    d3.mkdir(parents=True)
    (d1 / "one.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "two.pdf").write_bytes(b"%PDF-1.4")
    (d3 / "three.pdf").write_bytes(b"%PDF-1.4")
    for skipped in ("@eaDir", ".sync", "#recycle"):  # NAS 系统目录与隐藏目录不进入
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "x.pdf").write_bytes(b"%PDF-1.4")
    elsewhere = tmp_path.parent / f"elsewhere-{uuid.uuid4().hex}"
    elsewhere.mkdir()
    (elsewhere / "outside.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "linked").symlink_to(elsewhere, target_is_directory=True)  # 目录软链接不跟随

    try:
        by_depth = {
            depth: ingest_watch.list_candidates(str(tmp_path), [".pdf"], max_depth=depth)
            for depth in (0, 1, 2, 3)
        }
    finally:
        (elsewhere / "outside.pdf").unlink()
        elsewhere.rmdir()

    names = {d: sorted(f.name for f in files) for d, (files, _) in by_depth.items()}
    assert names[0] == ["top.pdf"]
    assert names[1] == ["one.pdf", "top.pdf"]
    assert names[2] == ["one.pdf", "top.pdf", "two.pdf"]
    assert names[3] == ["one.pdf", "three.pdf", "top.pdf", "two.pdf"]
    assert all(complete for _, complete in by_depth.values())
    three = next(f for f in by_depth[3][0] if f.name == "three.pdf")
    assert three.path == str(d3 / "three.pdf")


def test_list_candidates_limits_mark_incomplete(tmp_path):
    for i in range(3):
        sub = tmp_path / f"d{i}"
        sub.mkdir()
        (sub / f"f{i}.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "top.pdf").write_bytes(b"%PDF-1.4")

    scan = ingest_watch.list_candidates
    files, complete = scan(str(tmp_path), [".pdf"], max_depth=1, max_dirs=2)
    assert not complete
    # 按名字排序，只进前 2 个目录
    assert sorted(f.name for f in files) == ["f0.pdf", "f1.pdf", "top.pdf"]

    files, complete = scan(str(tmp_path), [".pdf"], max_depth=1, max_files=2)
    assert not complete
    assert len(files) == 2

    files, complete = scan(str(tmp_path), [".pdf"], max_depth=1, max_files=4)
    assert complete and len(files) == 4  # 恰好等于上限：已列完，不算不完整

    # 目录项累计上限：根目录 4 项读完即停，不再进入子目录
    files, complete = scan(str(tmp_path), [".pdf"], max_depth=1, max_entries=4)
    assert not complete
    assert [f.name for f in files] == ["top.pdf"]


def test_list_candidates_entry_type_error_is_incomplete(tmp_path, monkeypatch):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "sub").mkdir()
    real_scandir = os.scandir

    class StaleEntry:
        def __init__(self, entry):
            self._entry = entry
            self.name, self.path = entry.name, entry.path

        def is_symlink(self):
            return False

        def is_dir(self, follow_symlinks=True):
            raise OSError(116, "Stale file handle")

    def scandir(path):
        it = real_scandir(path)

        class Wrapped:
            def __enter__(self):
                return (StaleEntry(e) if e.name == "sub" else e for e in it.__enter__())

            def __exit__(self, *exc):
                return it.__exit__(*exc)

        return Wrapped()

    monkeypatch.setattr(ingest_watch.os, "scandir", scandir)
    files, complete = ingest_watch.list_candidates(str(tmp_path), [".pdf"], max_depth=1)
    assert not complete
    assert [f.name for f in files] == ["a.pdf"]


def test_list_candidates_unreadable_subdir_is_incomplete(tmp_path, monkeypatch):
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "bad").mkdir()
    real_scandir = os.scandir

    def flaky_scandir(path):
        if os.path.basename(path) == "bad":
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(ingest_watch.os, "scandir", flaky_scandir)
    files, complete = ingest_watch.list_candidates(str(tmp_path), [".pdf"], max_depth=1)
    assert not complete
    assert [f.name for f in files] == ["a.pdf"]

    monkeypatch.setattr(
        ingest_watch.os, "scandir", lambda path: (_ for _ in ()).throw(OSError("gone"))
    )
    with pytest.raises(OSError):  # 监听目录本身不可读：交给调用方记 dir_unavailable
        ingest_watch.list_candidates(str(tmp_path), [".pdf"], max_depth=1)


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
