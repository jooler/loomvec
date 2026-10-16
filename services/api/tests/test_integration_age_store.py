"""AgeStore 全空间读/删的集成测试（testcontainers PG18+AGE，不连 compose 栈）。

回归点：全空间边查询曾用无向匹配 `(a)-[r]-(b) WHERE a.space_id = ...`，
AGE 把它翻译成带 OR 的连接条件 → PG 只能嵌套循环（全图边 × 空间实体）：
万级实体空间单次 100s+，且每条边返回两次。改为按存储方向的有向匹配后
每条边恰好一次、走 start_id/end_id 索引。
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("testcontainers", reason="testcontainers 未安装")

pytestmark = pytest.mark.integration

ASSET_1 = str(uuid.uuid4())
ASSET_2 = str(uuid.uuid4())


@pytest.fixture()
async def graph(pg_url):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.graph.age import AgeStore, GraphEdge, GraphNode

    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    age = AgeStore()
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())

    def node(space: str, key: str) -> GraphNode:
        return GraphNode(key=key, space_id=space, tenant_id=None, name=key, type="concept")

    async with factory() as session, session.begin():
        await age.ensure_graph(session)
        await age.upsert_nodes(session, [node(s1, k) for k in ("A", "B", "C")])
        await age.upsert_nodes(session, [node(s2, k) for k in ("X", "Y")])
        await age.write_edges(
            session,
            [
                GraphEdge("A", "B", "USES", chunk_ids=["c1"], asset_ids=[ASSET_1]),
                GraphEdge("C", "B", "PART_OF", chunk_ids=["c2"], asset_ids=[ASSET_2]),
                GraphEdge("X", "Y", "USES", chunk_ids=["c3"], asset_ids=[ASSET_1]),
            ],
        )
    yield age, factory, s1, s2
    await engine.dispose()


def _triples(edges) -> list[tuple[str, str, str]]:
    return sorted((e.head_key, e.tail_key, e.type) for e in edges)


async def test_load_space_edges_returns_each_edge_once_with_stored_direction(graph):
    age, factory, s1, _ = graph
    async with factory() as session, session.begin():
        edges = await age.load_space_edges(session, s1)
    assert _triples(edges) == [("A", "B", "USES"), ("C", "B", "PART_OF")]
    by_type = {e.type: e for e in edges}
    assert by_type["USES"].chunk_ids == ["c1"]
    assert by_type["USES"].asset_ids == [ASSET_1]


async def test_load_space_edges_is_space_scoped(graph):
    age, factory, _, s2 = graph
    async with factory() as session, session.begin():
        assert _triples(await age.load_space_edges(session, s2)) == [("X", "Y", "USES")]
        assert await age.load_space_edges(session, str(uuid.uuid4())) == []


async def test_delete_asset_edges_only_touches_that_asset_in_space(graph):
    age, factory, s1, s2 = graph
    async with factory() as session, session.begin():
        touched = await age.delete_asset_edges(session, s1, uuid.UUID(ASSET_1))
    assert sorted(touched) == ["A", "B"]
    async with factory() as session, session.begin():
        assert _triples(await age.load_space_edges(session, s1)) == [("C", "B", "PART_OF")]
        # 同资产在别的空间的边不受影响
        assert _triples(await age.load_space_edges(session, s2)) == [("X", "Y", "USES")]


async def test_delete_asset_edges_without_match_is_noop(graph):
    age, factory, s1, _ = graph
    async with factory() as session, session.begin():
        assert await age.delete_asset_edges(session, s1, uuid.uuid4()) == []
        assert len(await age.load_space_edges(session, s1)) == 2


async def test_cypher_survives_rolled_back_transactions_on_a_pooled_connection(pg_url, graph):
    """回归：search_path 曾按连接只设一次，只读请求回滚时被撤销，同一条池连接上的下一次
    参数化 Cypher 报「cannot cast agtype string to type boolean」。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from loomvec.core.graph.age import AgeStore

    _, _, s1, _ = graph
    engine = create_async_engine(pg_url, pool_size=1, max_overflow=0)  # 强制复用同一条连接
    factory = async_sessionmaker(engine, expire_on_commit=False)
    age = AgeStore()
    try:
        for _ in range(3):
            async with factory() as session:  # 不提交：退出即回滚
                assert len(await age.load_space_edges(session, s1)) == 2
    finally:
        await engine.dispose()


async def test_graph_read_failure_degrades_without_poisoning_the_transaction(graph, monkeypatch):
    """回归：图读取失败被降级吞掉后，外层事务不能处于 aborted 状态（否则后续 PG 查询 500）。"""
    from sqlalchemy import text

    from loomvec.core.config import GraphSettings, SearchSettings
    from loomvec.core.retrieval import graph_retrieval
    from loomvec.core.retrieval.graph_retrieval import GraphRetriever

    _, factory, s1, _ = graph

    async def broken(session, space_id):
        await session.execute(text("SELECT 1/0"))

    monkeypatch.setattr(graph_retrieval.AGE, "load_space_edges", broken)
    retriever = GraphRetriever(None, GraphSettings(), SearchSettings())  # type: ignore[arg-type]
    async with factory() as session, session.begin():
        out = await retriever.adjacency_neighbors(
            session, space_ids=[uuid.UUID(s1)], seed_unit_ids=["c1"]
        )
        assert out == {}
        assert (await session.execute(text("SELECT 1"))).scalar() == 1


async def test_cypher_after_a_rolled_back_savepoint_that_did_the_first_prep(graph):
    """回归：SET LOCAL 在保存点内执行、保存点回滚后被撤销；标记若仍指向外层事务，
    同一事务里下一次 Cypher 会因缺 search_path 报「cannot cast agtype string to type boolean」。"""
    from sqlalchemy import text

    age, factory, s1, _ = graph
    async with factory() as session, session.begin():
        try:
            async with session.begin_nested():
                await age._prep(session)  # 首次预备发生在保存点内
                await session.execute(text("SELECT 1/0"))
        except Exception:
            pass
        assert len(await age.load_space_edges(session, s1)) == 2
