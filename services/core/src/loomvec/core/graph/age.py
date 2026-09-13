"""P3-CORE-01 AGE 访问层：openCypher（经 ag_catalog.cypher()）封装。

关键决策（03 文档 §三）：
- 图谱与业务数据**同库**：AgeStore 持有 SQLAlchemy AsyncSession，在**同一 PG 事务**
  内执行 Cypher——实体主表写入与图写入原子生效，资产删除同事务级联清理；
- 按 space 隔离：单图（constants.GRAPH_NAME），所有节点/边携带 space_id 属性，
  读写一律按 space_id 过滤；
- 确定性 ID：节点主键 entity_key = hash(space_id, 归一化名, type)（ontology.py），
  写入幂等（先查后建），重跑不留半图；
- 查询面刻意收窄为简单 Cypher（UNWIND/MATCH/CREATE/DELETE/RETURN map），
  图遍历（1~2 跳扩展、邻接补全、社区检测）在 Python 侧基于 load_space_edges()
  的边表执行——空间规模（万级边）内往返成本低于复杂 Cypher，且可单测。

agtype 返回值为 JSON 文本，统一 json.loads 反序列化。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from loomvec.core.constants import GRAPH_NAME

logger = structlog.get_logger("loomvec.graph")

# 预备语句：每条后端连接首个图操作前执行一次（LOAD 'age' 为会话级；
# 失败容忍——search_path 缺失时所有引用已全限定 ag_catalog）
_PREP_SQL = (
    "CREATE EXTENSION IF NOT EXISTS age",
    "LOAD 'age'",
    'SET search_path = ag_catalog, "$user", public',
)


@dataclass
class GraphNode:
    """AGE 节点（与 PG entity 主表行对应）。"""

    key: str
    space_id: str
    tenant_id: str | None
    name: str
    type: str
    description: str | None = None


@dataclass
class GraphEdge:
    """AGE 边：chunk_ids/asset_ids 为溯源（命中边反查语义单元；删资产按 asset_ids 清理）。"""

    head_key: str
    tail_key: str
    type: str
    weight: float = 1.0
    chunk_ids: list[str] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)


class AgeStore:
    """openCypher 访问器：绑定调用方事务（session），无独立连接生命周期。"""

    def __init__(self, graph: str = GRAPH_NAME) -> None:
        self.graph = graph
        self._prepped: set[int] = set()

    # ---------- 基础执行 ----------

    async def _prep(self, session: AsyncSession) -> None:
        conn = await session.connection()
        raw = await conn.get_raw_connection()
        key = id(raw)
        if key in self._prepped:
            return
        for sql in _PREP_SQL:
            try:
                await conn.exec_driver_sql(sql)
            except Exception as e:  # 非 superuser LOAD 失败等：继续用全限定名
                logger.debug("age_prep_statement_failed", sql=sql.split()[0], error=str(e))
        self._prepped.add(key)

    async def _run_cypher(
        self, session: AsyncSession, cypher: str, params: str | None = None
    ) -> list[Any]:
        """执行 Cypher；返回单列 agtype 反序列化结果（RETURN map 或标量）。"""
        await self._prep(session)
        conn = await session.connection()
        tag = f"$cypher{uuid4().hex}$"
        sql = f"SELECT * FROM ag_catalog.cypher('{self.graph}', {tag}{cypher}{tag}"
        args: list[Any] = []
        if params is not None:
            # AGE 参数形参为 ag_catalog.agtype（非 jsonb），显式 cast 才能命中签名
            sql += ", $1::ag_catalog.agtype"
            args.append(params)
        sql += ") AS (a ag_catalog.agtype)"
        result = await conn.exec_driver_sql(sql, tuple(args) if args else None)
        rows = result.fetchall()
        return [json.loads(r[0]) for r in rows if r[0] is not None]

    # ---------- 拓扑管理 ----------

    async def ensure_graph(self, session: AsyncSession) -> None:
        """幂等建图（compose init 与迁移 0005 已建；运行时兜底）。"""
        await self._prep(session)
        if not await self._graph_exists(session):
            await self._create_graph(session)

    async def _graph_exists(self, session: AsyncSession) -> bool:
        conn = await session.connection()
        result = await conn.exec_driver_sql(
            "SELECT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1)", (self.graph,)
        )
        return bool(result.scalar())

    async def _create_graph(self, session: AsyncSession) -> None:
        conn = await session.connection()
        await conn.exec_driver_sql(
            f"DO $$ BEGIN PERFORM ag_catalog.create_graph('{self.graph}'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
        )

    # ---------- 写入（幂等 upsert，P3-WRK-01）----------

    async def upsert_nodes(self, session: AsyncSession, nodes: list[GraphNode]) -> int:
        """节点幂等写入：先查已存在 key，仅 CREATE 缺失；全部节点刷新 description。

        并发安全：调用方（EntityGraphService）持有 per-space advisory lock，
        同空间图写不会并行。
        """
        if not nodes:
            return 0
        unique: dict[str, GraphNode] = {n.key: n for n in nodes}
        rows = [
            {
                "key": n.key,
                "space_id": n.space_id,
                "tenant_id": n.tenant_id,
                "name": n.name,
                "type": n.type,
                "description": n.description,
            }
            for n in unique.values()
        ]
        existing = {
            r if isinstance(r, str) else r.get("key")
            for r in await self._run_cypher(
                session,
                "UNWIND $rows AS k MATCH (n:Entity {key: k}) RETURN n.key",
                json.dumps({"rows": list(unique)}),
            )
        }
        created = [r for r in rows if r["key"] not in existing]
        if created:
            await self._run_cypher(
                session,
                (
                    "UNWIND $rows AS row CREATE (n:Entity {"
                    "key: row.key, space_id: row.space_id, tenant_id: row.tenant_id, "
                    "name: row.name, type: row.type, description: row.description})"
                ),
                json.dumps({"rows": created}),
            )
        # 描述以最新非空为准（合并抽取结果演进）
        with_desc = [r for r in rows if r.get("description")]
        if with_desc:
            await self._run_cypher(
                session,
                "UNWIND $rows AS row MATCH (n:Entity {key: row.key}) "
                "SET n.description = row.description",
                json.dumps({"rows": with_desc}),
            )
        return len(created)

    async def write_edges(self, session: AsyncSession, edges: list[GraphEdge]) -> int:
        """按 (head, tail, type) 已归并的边一次性 CREATE（溯源清单由调用方归并）。

        端点 key 同时写入边属性 h/t：读取走无向匹配，方向以属性为准
        （快照/回滚需要方向忠实，仅靠匹配位置会随查询视角翻转）。
        """
        if not edges:
            return 0
        rows = [
            {
                "h": e.head_key,
                "t": e.tail_key,
                "type": e.type,
                "weight": e.weight,
                "chunk_ids": e.chunk_ids,
                "asset_ids": e.asset_ids,
            }
            for e in edges
        ]
        await self._run_cypher(
            session,
            (
                "UNWIND $rows AS row MATCH (a:Entity {key: row.h}), (b:Entity {key: row.t}) "
                "CREATE (a)-[:REL {h: row.h, t: row.t, type: row.type, weight: row.weight, "
                "chunk_ids: row.chunk_ids, asset_ids: row.asset_ids}]->(b)"
            ),
            json.dumps({"rows": rows}),
        )
        return len(rows)

    async def delete_asset_edges(
        self, session: AsyncSession, space_id: str, asset_id: UUID
    ) -> list[str]:
        """删除某资产的边（资产删除/重跑级联），返回被删边触及的节点 key。"""
        rows = await self._run_cypher(
            session,
            (
                "MATCH (a:Entity)-[r:REL]-(b:Entity) "
                "WHERE a.space_id = $space AND $aid IN r.asset_ids "
                "RETURN {h: a.key, t: b.key} AS e"
            ),
            json.dumps({"space": space_id, "aid": str(asset_id)}),
        )
        if not rows:
            return []
        touched: list[str] = []
        for r in rows:
            for k in (r.get("h"), r.get("t")):
                if k and k not in touched:
                    touched.append(k)
        await self._run_cypher(
            session,
            (
                "MATCH (a:Entity)-[r:REL]-(b:Entity) "
                "WHERE a.space_id = $space AND $aid IN r.asset_ids DELETE r"
            ),
            json.dumps({"space": space_id, "aid": str(asset_id)}),
        )
        return touched

    async def delete_orphan_nodes(
        self, session: AsyncSession, space_id: str, keys: list[str]
    ) -> int:
        """删除指定 key 中已无任何边的节点（删资产后不留悬空节点）。

        写法说明：WHERE 内 pattern 谓词直接接 DELETE 会被 AGE 解析器判歧义，
        规范形式为 OPTIONAL MATCH 聚合 → WITH → WHERE → DELETE。
        """
        if not keys:
            return 0
        await self._run_cypher(
            session,
            (
                "UNWIND $rows AS k MATCH (n:Entity {key: k}) "
                "WHERE n.space_id = $space "
                "OPTIONAL MATCH (n)-[r:REL]-() "
                "WITH n, count(r) AS cnt WHERE cnt = 0 DELETE n"
            ),
            json.dumps({"rows": keys, "space": space_id}),
        )
        return len(keys)

    async def clear_space(self, session: AsyncSession, space_id: str) -> int:
        """清空空间子图（重建触发用）：DETACH DELETE 空间内全部节点。"""
        await self._run_cypher(
            session,
            "MATCH (n:Entity) WHERE n.space_id = $space DETACH DELETE n",
            json.dumps({"space": space_id}),
        )
        return 0

    async def load_node_edges(
        self, session: AsyncSession, space_id: str, key: str
    ) -> list[GraphEdge]:
        """某节点的全部边（合并重写/回滚的定点操作）；方向以边属性为准。"""
        rows = await self._run_cypher(
            session,
            (
                "MATCH (a:Entity {key: $key})-[r:REL]-(b:Entity) WHERE a.space_id = $space "
                "RETURN {h: coalesce(r.h, a.key), t: coalesce(r.t, b.key), type: r.type, "
                "weight: r.weight, chunk_ids: r.chunk_ids, asset_ids: r.asset_ids} AS e"
            ),
            json.dumps({"space": space_id, "key": key}),
        )
        edges: list[GraphEdge] = []
        for r in rows:
            edges.append(
                GraphEdge(
                    head_key=r["h"],
                    tail_key=r["t"],
                    type=r.get("type") or "REL",
                    weight=float(r.get("weight") or 1.0),
                    chunk_ids=[str(c) for c in (r.get("chunk_ids") or [])],
                    asset_ids=[str(a) for a in (r.get("asset_ids") or [])],
                )
            )
        return edges

    async def delete_node_edges(self, session: AsyncSession, space_id: str, key: str) -> int:
        """删除某节点的全部边（合并时先摘边再重写）。"""
        await self._run_cypher(
            session,
            "MATCH (a:Entity {key: $key})-[r:REL]-() WHERE a.space_id = $space DELETE r",
            json.dumps({"space": space_id, "key": key}),
        )
        return 0

    async def update_edge_chunks(
        self,
        session: AsyncSession,
        space_id: str,
        head_key: str,
        tail_key: str,
        edge_type: str,
        chunk_ids: list[str],
        asset_ids: list[str],
    ) -> None:
        """合并时同三元组边的溯源清单 union 回写。"""
        await self._run_cypher(
            session,
            (
                "MATCH (a:Entity {key: $h})-[r:REL {type: $t}]-(b:Entity {key: $k}) "
                "WHERE a.space_id = $space SET r.chunk_ids = $c, r.asset_ids = $s"
            ),
            json.dumps(
                {
                    "space": space_id,
                    "h": head_key,
                    "k": tail_key,
                    "t": edge_type,
                    "c": chunk_ids,
                    "s": asset_ids,
                }
            ),
        )

    async def delete_edge(
        self, session: AsyncSession, space_id: str, head_key: str, tail_key: str, edge_type: str
    ) -> None:
        """定点删除一条（无向匹配的）边（回滚恢复前清场）。"""
        await self._run_cypher(
            session,
            (
                "MATCH (a:Entity {key: $h})-[r:REL {type: $t}]-(b:Entity {key: $k}) "
                "WHERE a.space_id = $space DELETE r"
            ),
            json.dumps({"space": space_id, "h": head_key, "k": tail_key, "t": edge_type}),
        )

    async def delete_nodes(self, session: AsyncSession, space_id: str, keys: list[str]) -> int:
        """定点删除节点（含其边；合并后移除败者节点）。"""
        if not keys:
            return 0
        await self._run_cypher(
            session,
            "UNWIND $rows AS k MATCH (n:Entity {key: k}) WHERE n.space_id = $space DETACH DELETE n",
            json.dumps({"rows": keys, "space": space_id}),
        )
        return len(keys)

    # ---------- 读取 ----------

    async def load_space_edges(self, session: AsyncSession, space_id: str) -> list[GraphEdge]:
        """全量边表（Python 侧遍历用：L3 扩展/邻接补全/社区检测）。

        方向以边属性 h/t 为准（缺属性时回退匹配位置——兼容旧数据）。
        """
        rows = await self._run_cypher(
            session,
            (
                "MATCH (a:Entity)-[r:REL]-(b:Entity) WHERE a.space_id = $space "
                "RETURN {h: coalesce(r.h, a.key), t: coalesce(r.t, b.key), type: r.type, "
                "weight: r.weight, chunk_ids: r.chunk_ids, asset_ids: r.asset_ids} AS e"
            ),
            json.dumps({"space": space_id}),
        )
        edges: list[GraphEdge] = []
        for r in rows:
            try:
                edges.append(
                    GraphEdge(
                        head_key=r["h"],
                        tail_key=r["t"],
                        type=r.get("type") or "REL",
                        weight=float(r.get("weight") or 1.0),
                        chunk_ids=[str(c) for c in (r.get("chunk_ids") or [])],
                        asset_ids=[str(a) for a in (r.get("asset_ids") or [])],
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return edges

    async def load_space_nodes(self, session: AsyncSession, space_id: str) -> dict[str, GraphNode]:
        """全量节点表（key → GraphNode；社区检测/证据链名称解析用）。"""
        rows = await self._run_cypher(
            session,
            (
                "MATCH (n:Entity) WHERE n.space_id = $space "
                "RETURN {key: n.key, name: n.name, type: n.type, "
                "description: n.description, tenant_id: n.tenant_id} AS n"
            ),
            json.dumps({"space": space_id}),
        )
        nodes: dict[str, GraphNode] = {}
        for r in rows:
            nodes[r["key"]] = GraphNode(
                key=r["key"],
                space_id=space_id,
                tenant_id=r.get("tenant_id"),
                name=r.get("name") or "",
                type=r.get("type") or "other",
                description=r.get("description"),
            )
        return nodes

    async def space_edge_count(self, session: AsyncSession, space_id: str) -> int:
        rows = await self._run_cypher(
            session,
            "MATCH (n:Entity) WHERE n.space_id = $space RETURN count(n) AS c",
            json.dumps({"space": space_id}),
        )
        return int(rows[0]) if rows else 0
