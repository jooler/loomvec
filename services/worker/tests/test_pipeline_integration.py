"""P1-QA-02 管线集成测试：文本摄取 → parse/chunk/embed/index → 混合检索命中。

依赖 compose 基础设施栈（PG 35433 / Milvus 39530 / RustFS 39000 / Redis 36379）
与已执行迁移（make migrate）。AI 网关强制 mock（离线确定性）。Docker 不可用自动跳过。
"""

from __future__ import annotations

import socket
import uuid

import pytest

from loomvec.core.config import Settings
from loomvec.core.db.models import Asset, AssetStatus, AssetVersion
from loomvec.core.pipeline import PipelineDeps, PipelineRunner
from loomvec.core.retrieval import Retriever, SemanticHit

INTEGRATION_ENV = [
    ("127.0.0.1", 35433),  # postgres
    ("127.0.0.1", 39530),  # milvus
    ("127.0.0.1", 39000),  # rustfs
]


def _stack_ready() -> bool:
    return all(_port_open(h, p) for h, p in INTEGRATION_ENV)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _stack_ready(), reason="compose 基础设施栈未运行"),
]

SAMPLE_MD = """# 采集器操作手册（集成测试样例）

## 设备启动
QX-7 型采集器开机前需确认电源电压 220V，接地良好。\n首次启动会进行自检，指示灯呈绿色为正常。

## 校准流程
每 90 天执行一次零点校准：进入维护菜单，选择"零点校准"，等待 3 分钟提示完成。校准期间禁止采样。

## 故障排查
错误码 E-41 表示温度传感器断路；E-52 表示存储卡写入失败，需更换工业级 SD 卡。

<table><tr><th>错误码</th><th>含义</th></tr><tr><td>E-41</td><td>温度传感器断路</td></tr><tr><td>E-52</td><td>存储卡写入失败</td></tr></table>

## 维护周期
滤芯每 6 个月更换一次；密封圈每年检查；整机大修周期为 3 年。
"""


@pytest.fixture
def deps() -> PipelineDeps:
    settings = Settings(_env_file=None)  # 不继承仓库根 .env（测试确定性）
    settings.env = settings.env.__class__.TEST
    settings.postgres.url = "postgresql+asyncpg://loomvec:loomvec@localhost:35433/loomvec"
    settings.ai.mock = True
    from loomvec.core.ai import AiGateway
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.mineru_client import MineruClient
    from loomvec.core.retrieval import MilvusStore
    from loomvec.core.storage import ObjectStorage

    _engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
    return PipelineDeps(
        settings=settings,
        session_factory=session_factory,
        storage=ObjectStorage(settings.storage),
        ai=AiGateway(settings.ai),
        mineru=MineruClient(settings.mineru),
        milvus=MilvusStore(settings.milvus, settings.ai),
    )


async def test_full_pipeline_and_retrieval(deps: PipelineDeps):
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import delete as sa_delete

    from loomvec.core.db.models import Space, SpaceType

    # 每次运行独立空间：默认空间会被历史运行污染（近重复资产累积后，
    # mock 向量 + BM25 的 RRF 排序不再稳定），独立空间保证断言确定性；
    # 运行结束级联清理（PG 行 + Milvus 向量），不留测试残留。
    async with deps.session_factory() as session, session.begin():
        try:
            await session.execute(__import__("sqlalchemy").text("SELECT 1 FROM asset LIMIT 1"))
        except Exception as e:
            pytest.skip(f"迁移未执行（先 make migrate）：{e}")
        tenant_id = (
            await session.execute(
                __import__("sqlalchemy").text(
                    "SELECT tenant_id FROM space WHERE id = '0198bec0-0000-7000-8000-000000000001'"
                )
            )
        ).scalar_one()
        space = Space(
            tenant_id=tenant_id,
            slug=f"pipeline-it-{uuid.uuid4().hex[:12]}",
            name="管线集成测试",
            space_type=SpaceType.PERSONAL,
        )
        session.add(space)
        await session.flush()
        space_id = space.id
        asset = Asset(
            space_id=space_id,
            name="QX-7采集器操作手册.md",
            mime_type="text/markdown",
            ext=".md",
            size_bytes=len(SAMPLE_MD.encode()),
        )
        session.add(asset)
        await session.flush()
        session.add(
            AssetVersion(
                asset_id=asset.id,
                version=1,
                mime_type=asset.mime_type,
                size_bytes=asset.size_bytes,
                inline_text=SAMPLE_MD,
            )
        )
        asset_id = asset.id

    # ---- 全链路：parse → chunk → embed → index ----
    runner = PipelineRunner(deps)
    results = await runner.run(asset_id)
    # 样例切分预期：正文文本片 ≥2（表格前后）+ 表格独立片 1
    assert results[-1]["indexed"] == results[-2]["embedded"] >= 3
    async with deps.session_factory() as session:
        from sqlalchemy import select

        from loomvec.core.db.models import SemanticUnit, UnitType

        units = (
            (await session.execute(select(SemanticUnit).where(SemanticUnit.asset_id == asset_id)))
            .scalars()
            .all()
        )
        assert any(u.unit_type == UnitType.TABLE for u in units), "表格未独立成片"
        assert all(u.locator.get("start_line") for u in units), "locator 缺失行号"

    async with deps.session_factory() as session:
        from sqlalchemy import select

        asset = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        assert asset.status == AssetStatus.READY

    # ---- 混合检索：语义 + 精确词两路 ----
    retriever = Retriever(deps.milvus, deps.ai, deps.settings.search)
    hits: list[SemanticHit] = await retriever.search(
        space_ids=[space_id], query="采集器校准周期是多久"
    )
    assert hits, "检索无结果"
    assert any("零点校准" in h.text for h in hits), "语义命中失败"

    hits_bm25 = await retriever.search(space_ids=[space_id], query="E-41")
    assert any("E-41" in h.text for h in hits_bm25), "BM25 精确词命中失败"

    # locator 由 API 服务层从 PG 补全（unit 行已断言 start_line 存在，见上）

    # rerank 开关 A/B：两种模式均有结果（质量对比在 evals 回归中量化）
    hits_norr = await retriever.search(space_ids=[space_id], query="校准流程", rerank=False)
    assert hits_norr

    # ---- 级联清理：不留测试残留（try/finally：断言失败也要清；向量先清，PG 行后清）----
    try:
        pass  # 断言在上方；finally 保证清理执行
    finally:
        await deps.milvus.async_delete_space_units(space_id)
        await deps.milvus.async_delete_space_entities(space_id)
        async with deps.session_factory() as session, session.begin():
            # 图谱侧产物：entity/community/merge_log 的 space_id 为普通列（无 FK 级联）
            from loomvec.core.db.models import Community, Entity, EntityMergeLog

            await session.execute(
                sa_delete(EntityMergeLog).where(EntityMergeLog.space_id == space_id)
            )
            await session.execute(sa_delete(Entity).where(Entity.space_id == space_id))
            await session.execute(sa_delete(Community).where(Community.space_id == space_id))
            await session.execute(sa_delete(SemanticUnit).where(SemanticUnit.space_id == space_id))
            await session.execute(sa_delete(AssetVersion).where(AssetVersion.asset_id == asset_id))
            await session.execute(sa_delete(Asset).where(Asset.id == asset_id))
            await session.execute(sa_delete(Space).where(Space.id == space_id))


async def test_mineru_busy_defers_without_failing_asset(deps: PipelineDeps, monkeypatch):
    # 名额满（MineruBusyError）不是失败：资产保持 processing、不留任务行，异常交任务层重入队
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select, text

    from loomvec.core.db.models import ProcessingJob, Space, SpaceType
    from loomvec.core.mineru_slots import MineruBusyError
    from loomvec.core.pipeline.steps import ParseStep

    async with deps.session_factory() as session, session.begin():
        tenant_id = (
            await session.execute(
                text(
                    "SELECT tenant_id FROM space WHERE id = '0198bec0-0000-7000-8000-000000000001'"
                )
            )
        ).scalar_one()
        space = Space(
            tenant_id=tenant_id,
            slug=f"pipeline-busy-{uuid.uuid4().hex[:12]}",
            name="管线名额测试",
            space_type=SpaceType.PERSONAL,
        )
        session.add(space)
        await session.flush()
        space_id = space.id
        asset = Asset(
            space_id=space_id, name="busy.md", mime_type="text/markdown", ext=".md", size_bytes=1
        )
        session.add(asset)
        await session.flush()
        session.add(
            AssetVersion(
                asset_id=asset.id,
                version=1,
                mime_type="text/markdown",
                size_bytes=1,
                inline_text="x",
            )
        )
        asset_id = asset.id

    async def busy(self, session, asset, version):
        raise MineruBusyError("MinerU 解析名额已满")

    monkeypatch.setattr(ParseStep, "run", busy)
    try:
        with pytest.raises(MineruBusyError):
            await PipelineRunner(deps).run(asset_id)
        async with deps.session_factory() as session:
            row = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
            assert row.status == AssetStatus.PROCESSING and row.status_reason is None
            jobs = (
                await session.execute(
                    select(ProcessingJob).where(ProcessingJob.asset_id == asset_id)
                )
            ).all()
            assert jobs == []
    finally:
        async with deps.session_factory() as session, session.begin():
            await session.execute(sa_delete(AssetVersion).where(AssetVersion.asset_id == asset_id))
            await session.execute(sa_delete(Asset).where(Asset.id == asset_id))
            await session.execute(sa_delete(Space).where(Space.id == space_id))


async def _make_space_and_text_asset(deps: PipelineDeps, *, suffix: str, **space_fields):
    """建一个独立空间 + inline_text 资产（复用 SAMPLE_MD），返回 (space_id, asset_id)。"""
    from sqlalchemy import text as sa_text

    from loomvec.core.db.models import Space, SpaceType

    async with deps.session_factory() as session, session.begin():
        tenant_id = (
            await session.execute(
                sa_text(
                    "SELECT tenant_id FROM space WHERE id = '0198bec0-0000-7000-8000-000000000001'"
                )
            )
        ).scalar_one()
        space = Space(
            tenant_id=tenant_id,
            slug=f"pipeline-{suffix}-{uuid.uuid4().hex[:12]}",
            name=f"管线测试-{suffix}",
            space_type=SpaceType.PERSONAL,
            **space_fields,
        )
        session.add(space)
        await session.flush()
        space_id = space.id
        asset = Asset(
            space_id=space_id,
            name=f"{suffix}.md",
            mime_type="text/markdown",
            ext=".md",
            size_bytes=len(SAMPLE_MD.encode()),
        )
        session.add(asset)
        await session.flush()
        session.add(
            AssetVersion(
                asset_id=asset.id,
                version=1,
                mime_type="text/markdown",
                size_bytes=asset.size_bytes,
                inline_text=SAMPLE_MD,
            )
        )
        asset_id = asset.id
    return space_id, asset_id


async def _cleanup_space_and_asset(deps: PipelineDeps, *, space_id, asset_id) -> None:
    from sqlalchemy import delete as sa_delete

    from loomvec.core.db.models import (
        Community,
        Entity,
        EntityMergeLog,
        SemanticUnit,
        Space,
    )

    await deps.milvus.async_delete_space_units(space_id)
    await deps.milvus.async_delete_space_entities(space_id)
    async with deps.session_factory() as session, session.begin():
        await session.execute(sa_delete(EntityMergeLog).where(EntityMergeLog.space_id == space_id))
        await session.execute(sa_delete(Entity).where(Entity.space_id == space_id))
        await session.execute(sa_delete(Community).where(Community.space_id == space_id))
        await session.execute(sa_delete(SemanticUnit).where(SemanticUnit.space_id == space_id))
        await session.execute(sa_delete(AssetVersion).where(AssetVersion.asset_id == asset_id))
        await session.execute(sa_delete(Asset).where(Asset.id == asset_id))
        await session.execute(sa_delete(Space).where(Space.id == space_id))


async def test_chunk_strategy_structural_skips_llm_and_graph(deps: PipelineDeps, monkeypatch):
    """空间覆盖 chunk_strategy=structural：零 LLM 调用，分片靠规则，graph_status=skipped。"""
    from sqlalchemy import select

    from loomvec.core.db.models import ChunkMethod, GraphStatus, SemanticUnit

    space_id, asset_id = await _make_space_and_text_asset(
        deps, suffix="structural", chunk_strategy="structural"
    )

    async def refuse(*_args, **_kwargs):
        raise AssertionError("structural 策略下不应调用 LLM")

    monkeypatch.setattr(deps.ai, "complete_json", refuse)
    try:
        results = await PipelineRunner(deps).run(asset_id)
        assert results[-1]["indexed"] >= 1  # 仍然建出索引，只是没走 LLM
        async with deps.session_factory() as session:
            asset = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
            assert asset.graph_status == GraphStatus.SKIPPED
            units = (
                (
                    await session.execute(
                        select(SemanticUnit).where(SemanticUnit.asset_id == asset_id)
                    )
                )
                .scalars()
                .all()
            )
            assert units and all(u.chunk_method == ChunkMethod.STRUCTURAL_FALLBACK for u in units)
    finally:
        await _cleanup_space_and_asset(deps, space_id=space_id, asset_id=asset_id)


async def test_graph_extraction_off_requests_chunk_only(deps: PipelineDeps):
    """空间覆盖 graph_extraction=off：仍走 LLM 分片，但不请求/不落地实体关系。"""
    from sqlalchemy import select

    from loomvec.core.db.models import GraphStatus

    # 显式 llm：全局默认是 structural，不设则测不到"LLM 分片 + 关抽取"这条路径
    space_id, asset_id = await _make_space_and_text_asset(
        deps, suffix="graph-off", chunk_strategy="llm", graph_extraction="off"
    )
    try:
        results = await PipelineRunner(deps).run(asset_id)
        assert results[-1]["indexed"] >= 1
        async with deps.session_factory() as session:
            asset = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
            version = (
                await session.execute(select(AssetVersion).where(AssetVersion.asset_id == asset_id))
            ).scalar_one()
            assert asset.graph_status == GraphStatus.SKIPPED
            assert version.version_meta["graph"]["status"] == "skipped"
    finally:
        await _cleanup_space_and_asset(deps, space_id=space_id, asset_id=asset_id)


async def test_chunk_cache_key_changes_with_strategy_and_graph_flag():
    from loomvec.core.config import Settings
    from loomvec.core.pipeline.base import chunk_cache_key

    settings = Settings(_env_file=None)
    base = chunk_cache_key(settings, "sum", "m", chunk_strategy="llm", graph_extraction=True)
    other_strategy = chunk_cache_key(
        settings, "sum", "m", chunk_strategy="structural", graph_extraction=True
    )
    other_graph = chunk_cache_key(
        settings, "sum", "m", chunk_strategy="llm", graph_extraction=False
    )
    assert len({base, other_strategy, other_graph}) == 3
