"""ChunkStep 分片策略/图谱抽取开关的解析：空间覆盖 > 全局配置，脏数据防御性回落。

不经 DB/LLM——纯函数级测试，整条管线的端到端行为见
services/worker/tests/test_pipeline_integration.py。
"""

from __future__ import annotations

from loomvec.core.config import Settings
from loomvec.core.db.models import Space, SpaceType
from loomvec.core.pipeline.base import PipelineDeps
from loomvec.core.pipeline.steps import ChunkStep


def _deps(*, chunk_strategy: str = "llm", graph_extraction_enabled: bool = True) -> PipelineDeps:
    settings = Settings(_env_file=None)
    settings.pipeline.chunk_strategy = chunk_strategy
    settings.graph.extraction_enabled = graph_extraction_enabled
    return PipelineDeps(
        settings=settings, session_factory=None, storage=None, ai=None, mineru=None, milvus=None
    )


def _space(**kw) -> Space:
    return Space(name="x", slug="x", space_type=SpaceType.PERSONAL, **kw)


def test_chunk_strategy_defaults_to_global_when_no_space():
    assert ChunkStep._resolve_chunk_strategy(_deps(), None) == "llm"
    assert ChunkStep._resolve_chunk_strategy(_deps(chunk_strategy="structural"), None) == (
        "structural"
    )


def test_chunk_strategy_space_override_wins():
    deps = _deps(chunk_strategy="llm")
    assert ChunkStep._resolve_chunk_strategy(deps, _space(chunk_strategy="structural")) == (
        "structural"
    )


def test_chunk_strategy_unset_space_falls_back_to_global():
    deps = _deps(chunk_strategy="structural")
    assert ChunkStep._resolve_chunk_strategy(deps, _space(chunk_strategy=None)) == "structural"


def test_chunk_strategy_invalid_space_value_falls_back_to_global():
    # 白名单外的脏数据（如手工改库）不应中断管线，按未设置处理
    deps = _deps(chunk_strategy="llm")
    assert ChunkStep._resolve_chunk_strategy(
        deps, _space(chunk_strategy="not-a-real-strategy")
    ) == ("llm")


def test_graph_extraction_defaults_to_global_when_no_space():
    assert ChunkStep._resolve_graph_extraction(_deps(graph_extraction_enabled=True), None) is True
    assert ChunkStep._resolve_graph_extraction(_deps(graph_extraction_enabled=False), None) is False


def test_graph_extraction_space_override_wins():
    deps = _deps(graph_extraction_enabled=True)
    assert ChunkStep._resolve_graph_extraction(deps, _space(graph_extraction="off")) is False
    deps = _deps(graph_extraction_enabled=False)
    assert ChunkStep._resolve_graph_extraction(deps, _space(graph_extraction="on")) is True


def test_graph_extraction_invalid_space_value_falls_back_to_global():
    deps = _deps(graph_extraction_enabled=True)
    assert ChunkStep._resolve_graph_extraction(deps, _space(graph_extraction="maybe")) is True
