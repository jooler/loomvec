"""抽取领域配置在管线里的落点：ChunkStep 写 version_meta、媒体现场抽取保留别名。

不连 DB/LLM：session 用假对象（只实现 ChunkStep.run 用到的几个调用），AI 用假网关。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from loomvec.core.config import Settings
from loomvec.core.graph.profiles import ACADEMIC, GENERAL, LEGAL, ExtractionProfile
from loomvec.core.pipeline import steps as steps_module
from loomvec.core.pipeline.base import PipelineDeps, chunk_cache_key
from loomvec.core.pipeline.graph_flow import GraphStep
from loomvec.core.pipeline.steps import ChunkStep

MARKDOWN = "# 标题\n\n甲公司与乙公司签订供货合同。\n\n## 条款\n\n保修期两年。"


class _FakeResult:
    def __init__(self, space):
        self._space = space

    def scalar_one_or_none(self):
        return self._space

    def scalars(self):
        return self

    def all(self):
        return []


class _FakeSession:
    """ChunkStep.run 的 DB 调用：查 Space、查/删已有 SemanticUnit、add、flush。"""

    def __init__(self, space):
        self.space = space
        self.added: list = []

    async def execute(self, _stmt):
        return _FakeResult(self.space)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def delete(self, _obj) -> None:  # 无旧单元，不会被调用
        raise AssertionError("不应删除任何旧单元")

    async def flush(self) -> None:
        return None


class _FakeAi:
    """返回覆盖整批的 marker + 一条自洽的实体/关系（含别名）。"""

    def __init__(self):
        self.messages: list[list[dict]] = []

    async def complete_json(self, messages):
        self.messages.append(messages)
        user = messages[-1]["content"]
        start = int(user.split("[DOC_START_LINE=")[1].split("]")[0])
        end = int(user.split("[DOC_END_LINE=")[1].split("]")[0])
        return {
            "chunk_markers": [{"start_line": start, "end_line": end, "title": "全文"}],
            "entities": [
                {
                    "name": "甲公司",
                    "type": "organization",
                    "description": "供货方",
                    "lines": [3],
                    "aliases": ["甲"],
                },
                {
                    "name": "乙公司",
                    "type": "organization",
                    "description": "采购方",
                    "lines": [3],
                },
            ],
            "relations": [
                {"head": "乙公司", "tail": "甲公司", "type": "SUPPLIED_BY", "evidence_lines": [3]}
            ],
        }


def _deps(ai, *, chunk_strategy: str = "llm", global_profile: str = "general") -> PipelineDeps:
    settings = Settings(_env_file=None)
    settings.pipeline.chunk_strategy = chunk_strategy
    settings.pipeline.extraction_profile = global_profile
    return PipelineDeps(
        settings=settings, session_factory=None, storage=None, ai=ai, mineru=None, milvus=None
    )


def _asset() -> SimpleNamespace:
    return SimpleNamespace(
        id="asset-1",
        tenant_id="tenant-1",
        space_id="space-1",
        mime_type="text/markdown",
        graph_status=None,
        asset_meta={},
    )


def _version() -> SimpleNamespace:
    return SimpleNamespace(
        id="version-1",
        checksum="sum",
        inline_text=MARKDOWN,
        version_meta={},
        chunk_cache_key=None,
    )


@pytest.fixture
def patch_profiles(monkeypatch):
    """load_extraction_profiles 读 system_config（DB）：换成内置 + 指定的自定义配置。"""

    def _patch(extra: dict[str, ExtractionProfile] | None = None) -> None:
        from loomvec.core.graph.profiles import BUILTIN_PROFILES

        async def fake_load(_session):
            return {**BUILTIN_PROFILES, **(extra or {})}

        monkeypatch.setattr(steps_module, "load_extraction_profiles", fake_load)

    return _patch


async def _run_chunk(deps, space=None, **kw):
    step = ChunkStep()
    step._deps = deps
    asset, version = _asset(), _version()
    result = await step.run(_FakeSession(space), asset, version, **kw)
    return result, asset, version


async def test_chunk_step_writes_extraction_profile_to_version_meta(patch_profiles):
    patch_profiles()
    ai = _FakeAi()
    space = SimpleNamespace(
        chunk_preset=None,
        chunk_strategy=None,
        graph_extraction=None,
        extraction_profile="legal",
    )
    _, asset, version = await _run_chunk(_deps(ai), space)

    meta = version.version_meta
    for part in (meta["chunk"], meta["graph"]):
        assert part["extraction_profile"] == "legal"
        assert part["extraction_profile_fingerprint"] == LEGAL.fingerprint
    # 抽取结果随之落盘，别名保留
    ents = {e["name"]: e for e in meta["graph"]["extraction"]["entities"]}
    assert ents["甲公司"]["aliases"] == ["甲"]
    assert meta["graph"]["status"] == "pending"
    assert asset.graph_status.value == "pending"
    # 缓存键落库，且含该配置的 key + 指纹（命中依据）
    deps = _deps(ai)
    assert version.chunk_cache_key == chunk_cache_key(
        deps.settings,
        "sum",
        deps.settings.ai.llm.model,
        chunk_strategy="llm",
        graph_extraction=True,
        extraction_profile=LEGAL,
    )
    # prompt 用的是 legal 领域配置
    assert "statute=" in ai.messages[0][0]["content"]


async def test_chunk_step_profile_follows_run_override_then_space_then_global(patch_profiles):
    patch_profiles()
    space = SimpleNamespace(
        chunk_preset=None, chunk_strategy=None, graph_extraction=None, extraction_profile=None
    )
    # 无空间覆盖 → 全局
    _, _, v = await _run_chunk(_deps(_FakeAi(), global_profile="academic"), space)
    assert v.version_meta["chunk"]["extraction_profile"] == "academic"
    assert v.version_meta["chunk"]["extraction_profile_fingerprint"] == ACADEMIC.fingerprint
    # 本次运行覆盖最优先
    _, _, v = await _run_chunk(_deps(_FakeAi()), space, extraction_profile="legal")
    assert v.version_meta["chunk"]["extraction_profile"] == "legal"


async def test_chunk_step_custom_profile_fingerprint_follows_content(patch_profiles):
    custom = replace(GENERAL, key="mine", builtin=False)
    patch_profiles({"mine": custom})
    _, _, v1 = await _run_chunk(_deps(_FakeAi()), None, extraction_profile="mine")
    assert v1.version_meta["chunk"]["extraction_profile"] == "mine"
    assert v1.version_meta["chunk"]["extraction_profile_fingerprint"] == custom.fingerprint

    patch_profiles({"mine": replace(custom, name_rules="新增规则")})
    _, _, v2 = await _run_chunk(_deps(_FakeAi()), None, extraction_profile="mine")
    assert (
        v2.version_meta["chunk"]["extraction_profile_fingerprint"]
        != v1.version_meta["chunk"]["extraction_profile_fingerprint"]
    )
    # 内容变了 → 缓存键也变（旧分片缓存不会被命中）
    assert v2.chunk_cache_key != v1.chunk_cache_key


async def test_chunk_step_structural_strategy_records_no_profile(patch_profiles):
    # 规则分片不调用 LLM、不抽图谱：领域配置不影响结果，version_meta 里记 None、不入缓存键
    patch_profiles()
    ai = _FakeAi()
    _, asset, version = await _run_chunk(_deps(ai, chunk_strategy="structural"), None)
    assert ai.messages == []
    assert version.version_meta["chunk"]["extraction_profile"] is None
    assert version.version_meta["chunk"]["extraction_profile_fingerprint"] is None
    assert asset.graph_status.value == "skipped"


async def test_media_extraction_keeps_aliases():
    """媒体资产现场抽取：别名随实体带进抽取结果（此前被丢掉）。"""
    ai = _FakeAi()
    step = GraphStep()
    step._deps = _deps(ai)
    units = [
        SimpleNamespace(title="开场", content="甲公司与乙公司签约", order_index=0),
        SimpleNamespace(title=None, content="双方握手", order_index=1),
    ]
    result = await step._media_extraction(units, GENERAL)  # type: ignore[arg-type]
    ents = {e["name"]: e for e in result["entities"]}
    assert ents["甲公司"]["aliases"] == ["甲"]
    assert ents["乙公司"]["aliases"] == []
    assert result["relations"][0]["type"] == "SUPPLIED_BY"


class _PartlyFailingAi(_FakeAi):
    """起点 >1 的批次抛异常（模拟网关 503）。"""

    async def complete_json(self, messages):
        user = messages[-1]["content"]
        if int(user.split("[DOC_START_LINE=")[1].split("]")[0]) > 1:
            raise RuntimeError("503")
        return await super().complete_json(messages)


async def test_chunk_step_records_extraction_failed_batches(patch_profiles, monkeypatch):
    """有批次 LLM 调用失败：version_meta 留下失败批数（reextract 据此判定未完成），状态机不变。"""
    patch_profiles()
    deps = _deps(_PartlyFailingAi())
    monkeypatch.setattr(deps.settings.pipeline, "llm_batch_chars", 20)  # 切成两批
    _, asset, version = await _run_chunk(deps, None)
    meta = version.version_meta
    assert meta["chunk"]["fallback_batches"] == 1
    assert meta["chunk"]["extraction_failed_batches"] == 1
    assert meta["graph"]["extraction_failed_batches"] == 1
    assert asset.graph_status.value == "pending"  # 不改变资产状态机语义


async def test_chunk_step_no_failed_batches_when_all_ok(patch_profiles):
    patch_profiles()
    _, _, version = await _run_chunk(_deps(_FakeAi()), None)
    assert version.version_meta["chunk"]["extraction_failed_batches"] == 0
    assert version.version_meta["graph"]["extraction_failed_batches"] == 0


async def test_chunk_step_structural_not_counted_as_extraction_failure(patch_profiles):
    # 规则分片本就不抽取，fallback_batches 计数但不算「抽取失败」
    patch_profiles()
    _, _, version = await _run_chunk(_deps(_FakeAi(), chunk_strategy="structural"), None)
    assert version.version_meta["chunk"]["fallback_batches"] > 0
    assert version.version_meta["chunk"]["extraction_failed_batches"] == 0


async def test_media_extraction_drops_isolated_entities():
    """媒体现场抽取与文本路径同口径：不参与任何关系的实体按 drop_isolated 丢弃。"""

    class _Ai:
        async def complete_json(self, messages):
            return {
                "chunk_markers": [],
                "entities": [
                    {"name": "甲公司", "type": "organization"},
                    {"name": "乙公司", "type": "organization"},
                    {"name": "孤立实体", "type": "product"},
                ],
                "relations": [
                    {
                        "head": "乙公司",
                        "tail": "甲公司",
                        "type": "SUPPLIED_BY",
                        "evidence_lines": [1],
                    },
                    {"head": "乙公司", "tail": "不存在", "type": "USES", "evidence_lines": [1]},
                ],
            }

    units = [SimpleNamespace(title="开场", content="甲公司与乙公司签约", order_index=0)]
    step = GraphStep()
    step._deps = _deps(_Ai())
    assert step._deps.settings.graph.drop_isolated_entities
    result = await step._media_extraction(units, GENERAL)  # type: ignore[arg-type]
    assert {e["name"] for e in result["entities"]} == {"甲公司", "乙公司"}
    assert [r["type"] for r in result["relations"]] == ["SUPPLIED_BY"]  # 端点不可解析的关系被丢
    assert result["relations"][0]["chunk_key"] == 0

    # 关掉开关则保留孤立实体
    step._deps.settings.graph.drop_isolated_entities = False
    try:
        result = await step._media_extraction(units, GENERAL)  # type: ignore[arg-type]
    finally:
        step._deps.settings.graph.drop_isolated_entities = True
    assert "孤立实体" in {e["name"] for e in result["entities"]}


class _FakeAge:
    def __init__(self, touched):
        self.touched = touched
        self.calls: list[tuple] = []

    async def delete_asset_edges(self, session, space_id, asset_id):
        self.calls.append(("delete_asset_edges", space_id, asset_id))
        return self.touched

    async def delete_orphan_nodes(self, session, space_id, keys):
        self.calls.append(("delete_orphan_nodes", space_id, list(keys)))
        return len(keys)


class _NoopSession:
    async def execute(self, *_a, **_kw):
        return None

    async def flush(self):
        return None


async def test_write_asset_graph_empty_result_still_clears_old_edges(monkeypatch):
    """重抽后零实体：旧边（chunk_ids 指向已删单元）与由此悬空的节点必须清掉，再标记 ready。"""
    from loomvec.core.pipeline import graph_flow
    from loomvec.core.pipeline.graph_flow import EntityGraphService

    age = _FakeAge(touched=["k1", "k2"])
    monkeypatch.setattr(graph_flow, "AGE", age)
    asset = SimpleNamespace(id="asset-1", space_id="space-1", tenant_id=None, graph_status=None)
    version = SimpleNamespace(version_meta={"graph": {"status": "pending"}})

    out = await EntityGraphService(_NoopSession(), _deps(_FakeAi())).write_asset_graph(  # type: ignore[arg-type]
        asset,  # type: ignore[arg-type]
        version,  # type: ignore[arg-type]
        [],
        {"entities": [], "relations": []},
    )
    assert out == {"entities": 0, "relations": 0}
    assert age.calls == [
        ("delete_asset_edges", "space-1", "asset-1"),
        ("delete_orphan_nodes", "space-1", ["k1", "k2"]),
    ]
    assert asset.graph_status.value == "ready"
    assert version.version_meta["graph"]["entities"] == 0


def test_failed_extraction_batches_prefers_new_field_and_handles_legacy():
    from loomvec.core.pipeline.base import failed_extraction_batches as f

    assert f({"extraction_failed_batches": 2, "fallback_batches": 5}, has_extraction=True) == 2
    assert f({"extraction_failed_batches": 0, "fallback_batches": 5}, has_extraction=True) == 0
    # 旧记录（无新字段）：抽过图谱才把 fallback_batches 当失败批数
    assert f({"fallback_batches": 3}, has_extraction=True) == 3
    assert f({"fallback_batches": 3}, has_extraction=False) == 0
    assert f(None, has_extraction=True) == 0


async def test_chunk_step_cache_hit_is_skipped_only_without_failed_batches(
    patch_profiles, monkeypatch
):
    """上次有批次抽取失败的结果不当作缓存：重跑（reextract / 重试）才能补抽。"""
    patch_profiles()

    class _SessionWithUnits(_FakeSession):
        async def execute(self, _stmt):
            return _UnitIds(self.space)

        async def delete(self, _obj) -> None:  # 重跑会清掉旧单元
            return None

    class _UnitIds(_FakeResult):
        def scalars(self):
            return self

        def all(self):
            return ["unit-1"]  # 已有语义单元

    deps = _deps(_FakeAi())
    asset, version = _asset(), _version()
    step = ChunkStep()
    step._deps = deps
    # 先跑一遍拿到真实缓存键
    await step.run(_FakeSession(None), asset, version)
    key = version.chunk_cache_key

    async def run_again(chunk_meta):
        version.chunk_cache_key = key
        version.version_meta = {"chunk": chunk_meta, "graph": {"extraction": {}}}
        return await step.run(_SessionWithUnits(None), asset, version)

    hit = await run_again({"extraction_failed_batches": 0})
    assert hit.get("skipped") is True
    # 有失败批次：不命中缓存，重新分片抽取（假 session 无旧单元可删，只验证走到了 LLM）
    again = await run_again({"extraction_failed_batches": 1})
    assert "skipped" not in again and again["units"] >= 1
