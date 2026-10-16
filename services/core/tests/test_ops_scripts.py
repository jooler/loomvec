"""运维脚本里的纯函数：reextract 的跳过/待重抽判定与并发闸门、gc_entities 的保留集。

脚本在仓库根 scripts/ 下（不是包），按路径加载；这里不连库、不跑 main。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_script_{name}", _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reextract = _load("reextract_graph")
gc_entities = _load("gc_entities")

PV, KEY, FP = "chunk-graph-v5", "academic", "abcd1234"


def _state(**kw):
    base = {
        "status": "ready",
        "graph_status": "ready",
        "has_parse": True,
        "prompt_version": PV,
        "profile_key": KEY,
        "profile_fp": FP,
        "llm_model": "claude-haiku",
        "graph_reason": None,
        "chunk_meta": {"fallback_batches": 0, "extraction_failed_batches": 0},
        "has_extraction": True,
    }
    base.update(kw)
    return reextract.AssetState(**base)


def _classify(**kw) -> str:
    return reextract.classify_asset(
        _state(**kw), prompt_version=PV, profile_key=KEY, profile_fingerprint=FP
    )


def test_classify_done_when_everything_matches():
    assert _classify() == "done"
    # 新记录以 extraction_failed_batches 为准：marker 失效的兜底批次不影响抽取完整性
    assert _classify(chunk_meta={"fallback_batches": 3, "extraction_failed_batches": 0}) == "done"
    # graph_status=skipped（图谱总开关关闭等）不是图谱降级，不反复重抽
    assert _classify(graph_status="skipped") == "done"


@pytest.mark.parametrize("status", ["pending", "processing"])
def test_classify_in_flight_assets_are_never_dispatched(status):
    # 即使其它条件都「未完成」，在途资产也不碰（否则同一资产两条管线并发写）
    assert _classify(status=status) == "in_flight"
    assert _classify(status=status, prompt_version=None, has_parse=False) == "in_flight"


def test_classify_unparsed_assets_are_not_touched():
    # 从未解析成功：从 chunk 起跑没有输入
    assert _classify(has_parse=False, prompt_version=None, status="failed") == "unparsed"
    assert _classify(has_parse=False, prompt_version=None) == "unparsed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt_version": "chunk-graph-v4"},
        {"prompt_version": None},
        {"profile_key": "general"},
        {"profile_fp": "other"},
        {"profile_key": None, "profile_fp": None},
        {"llm_model": "mock"},
        {"graph_reason": "chunk_strategy=structural"},
        {"graph_status": "pending_retry"},
        {"graph_status": "failed"},
        {"status": "failed"},
        {"chunk_meta": {"extraction_failed_batches": 2}},  # 网关 503 导致有批次没抽到实体
        {"chunk_meta": {"fallback_batches": 1}},  # 旧记录没有该字段，看 fallback_batches
    ],
)
def test_classify_todo_when_not_completed(overrides):
    assert _classify(**overrides) == "todo"


def test_classify_legacy_structural_fallbacks_are_not_extraction_failures():
    # 旧记录没抽过图谱（规则分片）：fallback_batches 不算抽取失败
    assert _classify(chunk_meta={"fallback_batches": 4}, has_extraction=False) == "done"


def test_concurrency_error_guards_gateway_capacity():
    assert reextract.concurrency_error(2, 8, 16) is None
    assert reextract.concurrency_error(1, 8, 16) is None
    msg = reextract.concurrency_error(3, 8, 16)
    assert msg and "24" in msg and "--inflight" in msg
    assert reextract.concurrency_error(4, 8, 32) is None
    assert reextract.concurrency_error(5, 8, 32)


def test_cli_rejects_non_positive_inflight():
    import argparse

    assert reextract._positive_int("2") == 2
    for bad in ("0", "-1"):
        with pytest.raises(argparse.ArgumentTypeError):
            reextract._positive_int(bad)


def _edge(h: str, t: str):
    return SimpleNamespace(head_key=h, tail_key=t)


def test_gc_plan_keeps_only_nodes_with_edges():
    nodes = {"a": 1, "b": 2, "c": 3, "iso": 4}  # AGE 节点（key → 节点）
    edges = [_edge("a", "b"), _edge("b", "c")]
    active = ["a", "b", "c", "iso", "ghost"]  # PG 活跃实体（ghost 在 AGE 里已不存在）
    with_edge, isolated, stale = gc_entities.plan_gc(nodes, edges, active)
    assert with_edge == {"a", "b", "c"}
    assert isolated == ["iso"]  # AGE 孤立节点
    assert stale == ["iso", "ghost"]  # PG 里无边的活跃实体（含已不在 AGE 的）


def test_gc_plan_empty_graph_marks_everything_stale():
    # 全空间无边（如全部重抽后零实体）：保留集为空，主表活跃实体全部陈旧
    with_edge, isolated, stale = gc_entities.plan_gc({"x": 1}, [], ["x", "y"])
    assert with_edge == set() and isolated == ["x"] and stale == ["x", "y"]
