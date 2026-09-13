"""P3-QA-01 图谱一致性集成测试：抽取→写入→召回→合并→回滚→重跑幂等→删除级联。

依赖 compose 基础设施栈（PG 5433 含 AGE / Milvus 19530）与已执行迁移。
AI 网关强制 mock（离线确定性）；实体链接阈值在测试内放宽（mock 向量为
伪随机，不代表真实语义）以驱动 L3 全链路。
封闭性：测试在专用空间执行，结束（含失败）时清理三方（PG/AGE/Milvus）痕迹。
"""

from __future__ import annotations

import contextlib
import socket
import uuid

import pytest
from sqlalchemy import delete, select, text

from loomvec.core.config import Settings
from loomvec.core.constants import SEED_TENANT_ID
from loomvec.core.db.models import (
    Asset,
    AssetVersion,
    Entity,
    EntityMergeLog,
    SemanticUnit,
    Space,
)
from loomvec.core.pipeline import PipelineDeps, PipelineRunner
from loomvec.core.retrieval import MilvusStore, Retriever
from loomvec.core.retrieval.graph_retrieval import GraphRetriever

INTEGRATION_ENV = [("127.0.0.1", 5433), ("127.0.0.1", 19530)]


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(_port_open(h, p) for h, p in INTEGRATION_ENV),
        reason="compose 基础设施栈未运行",
    ),
]

SAMPLE = """# 企业关系说明（图谱集成测试）

## 股权结构
华山制造集团有限公司是云枢控股集团股份有限公司的全资子公司。
云枢控股集团股份有限公司的控股股东为岭南资本管理有限公司。
岭南资本管理有限公司的法定代表人是陈国峰。
云枢控股集团股份有限公司的法定代表人是林静怡。
岭南资本管理公司的注册地为广州市。岭南资本管理公司由陈国峰创立。

## 供应商
华山制造集团有限公司的核心供应商为南粤精密零部件厂。
"""


@pytest.fixture
def deps() -> PipelineDeps:
    settings = Settings()
    settings.env = settings.env.__class__.TEST
    settings.postgres.url = "postgresql+asyncpg://loomvec:loomvec@localhost:5433/loomvec"
    settings.ai.mock = True
    settings.graph.entity_link_threshold = -1.0  # mock 向量无语义：强制链接走通全链路
    settings.search.graph_enabled = True
    from loomvec.core.ai import AiGateway
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.mineru_client import MineruClient
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


@pytest.fixture
def space_id(deps: PipelineDeps):
    """专用测试空间的 id（创建/清理在测试协程内执行，避免跨事件循环复用连接）。"""
    _ = deps
    return uuid.uuid4()


async def create_space(deps: PipelineDeps, sid: uuid.UUID) -> None:
    async with deps.session_factory() as session, session.begin():
        session.add(
            Space(
                id=sid,
                tenant_id=uuid.UUID(SEED_TENANT_ID),
                slug=f"graph-it-{sid.hex[:12]}",
                name="图谱集成测试",
            )
        )


async def cleanup_space(deps: PipelineDeps, sid: uuid.UUID) -> None:
    from loomvec.core.graph.age import AgeStore

    age = AgeStore()
    async with deps.session_factory() as session, session.begin():
        asset_ids = (
            (await session.execute(select(Asset.id).where(Asset.space_id == sid))).scalars().all()
        )
        for aid in asset_ids:
            await session.execute(
                text("DELETE FROM processing_job WHERE asset_id = :a"), {"a": str(aid)}
            )
        await session.execute(delete(SemanticUnit).where(SemanticUnit.space_id == sid))
        await session.execute(delete(AssetVersion).where(AssetVersion.asset_id.in_(asset_ids)))
        await session.execute(delete(Asset).where(Asset.space_id == sid))
        await session.execute(delete(EntityMergeLog).where(EntityMergeLog.space_id == sid))
        await session.execute(delete(Entity).where(Entity.space_id == sid))
        with contextlib.suppress(Exception):
            await age.clear_space(session, str(sid))
        await session.execute(delete(Space).where(Space.id == sid))
    try:
        await deps.milvus.async_delete_space_units(sid)
        await deps.milvus.async_delete_space_entities(sid)
    except Exception:
        pass


async def test_graph_pipeline_consistency(deps: PipelineDeps, space_id):
    await create_space(deps, space_id)
    try:
        await _run_consistency(deps, space_id)
    finally:
        await cleanup_space(deps, space_id)


async def _run_consistency(deps: PipelineDeps, space_id: uuid.UUID):
    # ---- 迁移已执行检查 ----
    async with deps.session_factory() as session:
        try:
            await session.execute(text("SELECT 1 FROM entity LIMIT 1"))
        except Exception as e:
            pytest.skip(f"迁移未执行（先 make migrate）：{e}")

    # ---- 登记文本资产 ----
    async with deps.session_factory() as session, session.begin():
        asset = Asset(
            space_id=space_id,
            name="企业关系图谱集成测试.md",
            mime_type="text/markdown",
            ext=".md",
            size_bytes=len(SAMPLE.encode()),
        )
        session.add(asset)
        await session.flush()
        session.add(
            AssetVersion(
                asset_id=asset.id,
                version=1,
                mime_type=asset.mime_type,
                size_bytes=asset.size_bytes,
                inline_text=SAMPLE,
            )
        )
        asset_id = asset.id

    # ---- 全链路（chunk → graph → embed → index）----
    results = await PipelineRunner(deps).run(asset_id, from_step="chunk")
    assert results[-1]["indexed"] >= 1

    # ---- PG 主表：实体入库，graph_status=ready ----
    from loomvec.core.graph.age import AgeStore

    age = AgeStore()
    async with deps.session_factory() as session:
        asset = (await session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        assert asset.graph_status is not None and asset.graph_status.value == "ready"
        entities = (
            (
                await session.execute(
                    select(Entity).where(Entity.space_id == space_id, Entity.merged_into.is_(None))
                )
            )
            .scalars()
            .all()
        )
        assert len(entities) >= 3, "mock 抽取实体未入库"

    # ---- AGE：节点与边存在，边溯源指向本资产单元 ----
    async with deps.session_factory() as session:
        nodes = await age.load_space_nodes(session, str(space_id))
        edges = await age.load_space_edges(session, str(space_id))
        assert len(nodes) >= 3 and len(edges) >= 1
        unit_rows = (
            (await session.execute(select(SemanticUnit).where(SemanticUnit.asset_id == asset_id)))
            .scalars()
            .all()
        )
        unit_ids = {str(u.id) for u in unit_rows}
        assert all(set(e.chunk_ids) <= unit_ids for e in edges if e.chunk_ids)

    # ---- L3 图谱召回：链接 → 扩展 → chunk 回收 → 融合检索 ----
    graph_retriever = GraphRetriever(deps.milvus, deps.settings.graph, deps.settings.search)
    retriever = Retriever(deps.milvus, deps.ai, deps.settings.search, graph=graph_retriever)
    qvec = (await deps.ai.embed(["华山制造"]))[0]
    async with deps.session_factory() as session:
        recall = await graph_retriever.recall(
            session, space_ids=[space_id], query_vector=qvec, unit_types=None
        )
        assert recall.linked_entities, "实体链接失败"
        hits = await retriever.search(
            space_ids=[space_id],
            tenant_id=None,
            query="华山制造",
            use_graph=True,
            session=session,
            top_k=10,
        )
        assert hits, "L1+L2+L3 融合检索无命中"

    # ---- 阶段二合并：岭南资本管理有限公司 ↔ 岭南资本管理公司（编辑距离 2）----
    from loomvec.core.graph.merge import run_space_merge

    async with deps.session_factory() as session, session.begin():
        result = await run_space_merge(
            session,
            deps.settings,
            space_id,
            vector_fetch=None,  # mock 向量无语义：仅名称候选
            created_by="integration-test",
        )
    assert result["merged"] >= 1, "名称候选合并未发生"

    async with deps.session_factory() as session:
        loser = (
            (
                await session.execute(
                    select(Entity).where(
                        Entity.space_id == space_id, Entity.merged_into.is_not(None)
                    )
                )
            )
            .scalars()
            .first()
        )
        assert loser is not None
        loser_key = loser.entity_key
        nodes = await age.load_space_nodes(session, str(space_id))
        assert loser_key not in nodes, "合并后败者节点应已删除"

    # ---- 回滚：败者实体行与 AGE 节点/边恢复 ----
    from loomvec.core.graph.merge import rollback_merge

    async with deps.session_factory() as session, session.begin():
        log = (
            (
                await session.execute(
                    select(EntityMergeLog).where(
                        EntityMergeLog.space_id == space_id,
                        EntityMergeLog.loser_id == loser.id,
                    )
                )
            )
            .scalars()
            .first()
        )
        assert log is not None and log.status.value == "applied"
        await rollback_merge(session, deps.settings, log.id)

    async with deps.session_factory() as session:
        restored = (await session.execute(select(Entity).where(Entity.id == loser.id))).scalar_one()
        assert restored.merged_into is None and restored.deleted_at is None
        nodes = await age.load_space_nodes(session, str(space_id))
        assert loser_key in nodes, "回滚后败者节点未恢复"

    # ---- 重跑图写入：幂等（不产生重复边/丢边）----
    async with deps.session_factory() as session:
        before = {
            (e.head_key, e.tail_key, e.type, tuple(sorted(e.chunk_ids)))
            for e in await age.load_space_edges(session, str(space_id))
        }
    await PipelineRunner(deps).run(asset_id, from_step="graph")
    async with deps.session_factory() as session:
        after = {
            (e.head_key, e.tail_key, e.type, tuple(sorted(e.chunk_ids)))
            for e in await age.load_space_edges(session, str(space_id))
        }
    assert before == after, f"重跑图写不幂等：新增 {after - before}，丢失 {before - after}"

    # ---- 删除级联：资产边清理 + 悬空节点移除 ----
    async with deps.session_factory() as session, session.begin():
        touched = await age.delete_asset_edges(session, str(space_id), asset_id)
        await age.delete_orphan_nodes(session, str(space_id), touched)
    async with deps.session_factory() as session:
        edges_after = await age.load_space_edges(session, str(space_id))
        assert all(str(asset_id) not in (e.asset_ids or []) for e in edges_after)
