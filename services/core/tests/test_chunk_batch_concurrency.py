"""ChunkStep 各批 LLM 调用的并发：上限、结果顺序、单批失败的隔离。

不经 DB——直接测 ChunkStep._extract_batches；整条管线的行为见 worker 的集成测试。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from loomvec.core.config import PipelineSettings
from loomvec.core.graph.profiles import GENERAL
from loomvec.core.pipeline.base import PipelineDeps
from loomvec.core.pipeline.steps import ChunkStep


class _FakeAi:
    """按提示词里的批次起点返回可辨认的结果，并记录同时在途的调用数。"""

    def __init__(self, *, fail_starts: frozenset[int] = frozenset(), delay: float = 0.02):
        self.fail_starts = fail_starts
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    async def complete_json(self, messages):
        user = messages[-1]["content"]
        start = int(user.split("[DOC_START_LINE=")[1].split("]")[0])
        end = int(user.split("[DOC_END_LINE=")[1].split("]")[0])
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # 起点越靠前越慢：并发时完成顺序与批次顺序相反，用来验证结果仍按批次顺序返回
            await asyncio.sleep(self.delay * (10 - min(start // 100, 9)))
            if start in self.fail_starts:
                raise RuntimeError(f"boom {start}")
            return {
                "chunk_markers": [{"start_line": start, "end_line": end, "title": f"t{start}"}],
                "entities": [{"name": f"E{start}", "type": "product", "lines": [start]}],
                "relations": [],
            }
        finally:
            self.in_flight -= 1


def _deps(ai: _FakeAi, concurrency: int) -> PipelineDeps:
    # 不碰 Settings：它的嵌套配置对象在实例间共享（连深拷贝也共享），改一次会泄漏给其它测试。
    # _extract_batches 只读这两个字段，用简易对象代替即可
    pipeline = SimpleNamespace(llm_concurrency=concurrency, llm_batch_chars=6000)
    return PipelineDeps(
        settings=SimpleNamespace(pipeline=pipeline),  # type: ignore[arg-type]
        session_factory=None,
        storage=None,
        ai=ai,
        mineru=None,
        milvus=None,
    )


_LINES = [f"line {i}" for i in range(1, 1001)]
_BATCHES = [(i * 100 + 1, (i + 1) * 100) for i in range(10)]


async def _run(ai: _FakeAi, concurrency: int):
    return await ChunkStep._extract_batches(
        _deps(ai, concurrency),
        _LINES,
        _BATCHES,
        graph_on=True,
        profile=GENERAL,
        min_chars=1,
        max_chars=1000,
    )


async def test_concurrency_is_capped_and_results_keep_batch_order():
    ai = _FakeAi()
    results = await _run(ai, 3)
    assert ai.calls == 10
    assert ai.max_in_flight == 3  # 吃满上限，但不超
    # 完成顺序与批次顺序相反，结果仍与 batches 同序
    assert [r.entities[0].name for r in results] == [f"E{s}" for s, _ in _BATCHES]


async def test_concurrency_one_is_serial():
    ai = _FakeAi(delay=0.001)
    await _run(ai, 1)
    assert ai.max_in_flight == 1


async def test_one_failing_batch_does_not_affect_others():
    ai = _FakeAi(fail_starts=frozenset({301}))
    results = await _run(ai, 4)
    failed = [i for i, r in enumerate(results) if isinstance(r, BaseException)]
    assert failed == [3]  # 只有第 4 批（起点 301）失败，其余都成功解析
    assert all(r.extraction_ok for i, r in enumerate(results) if i != 3)


def test_concurrency_default_and_bounds():
    assert PipelineSettings().llm_concurrency == 8
    with pytest.raises(ValueError):
        PipelineSettings(llm_concurrency=0)
    with pytest.raises(ValueError):
        PipelineSettings(llm_concurrency=33)
