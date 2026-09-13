"""P1-CORE-03 Milvus 集合管理：建集合（schema/索引）、写入、删除、混合检索。

pymilvus 为同步 SDK：统一经 asyncio.to_thread 暴露异步接口（async_* 门面）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker

from loomvec.core.config import AiSettings, MilvusSettings
from loomvec.core.errors import UpstreamUnavailableError

logger = structlog.get_logger("loomvec.retrieval")

COLLECTION_SEMANTIC_UNITS = "semantic_units"
COLLECTION_ENTITIES = "entities"  # P3 实体链接向量（可由 PG entity 主表全量重建）


def _in_expr(field: str, values: list[str]) -> str:
    return f"{field} in [{', '.join(f'"{v}"' for v in values)}]"


class MilvusStore:
    """semantic_units 集合的写入/删除/建集合/检索（worker、API、删除级联共用）。"""

    def __init__(self, settings: MilvusSettings, embedding: AiSettings) -> None:
        self._settings = settings
        self._dim = embedding.embedding.dim
        self._clip_dim = embedding.clip.dim
        self._client: MilvusClient | None = None

    @property
    def text_dim(self) -> int:
        return self._dim

    @property
    def clip_dim(self) -> int:
        return self._clip_dim

    def _ensure_client(self) -> MilvusClient:
        if self._client is None:
            kwargs: dict[str, Any] = {"uri": self._settings.uri}
            if self._settings.token:
                kwargs["token"] = self._settings.token
            self._client = MilvusClient(**kwargs)
        return self._client

    # ---------- 集合管理 ----------

    def collection_exists(self) -> bool:
        return self._ensure_client().has_collection(COLLECTION_SEMANTIC_UNITS)

    def ensure_collection(self) -> None:
        client = self._ensure_client()
        if client.has_collection(COLLECTION_SEMANTIC_UNITS):
            desc = client.describe_collection(COLLECTION_SEMANTIC_UNITS)
            fields = {f["name"]: f for f in desc["fields"]}
            dense = fields.get("text_dense")
            if dense and dense["params"].get("dim") != self._dim:
                raise UpstreamUnavailableError(
                    upstream="milvus",
                    reason=(
                        f"集合 {COLLECTION_SEMANTIC_UNITS} 的向量维度为 "
                        f"{dense['params'].get('dim')}，与配置 embedding.dim={self._dim} 不一致；"
                        "更换嵌入模型需删除集合重建（语义单元可由管线重跑再生成）"
                    ),
                )
            # P2 schema 漂移：P1 集合缺 tenant_id / clip_dense 字段时拒绝写入/检索
            missing = [name for name in ("tenant_id", "clip_dense") if name not in fields]
            if missing:
                raise UpstreamUnavailableError(
                    upstream="milvus",
                    reason=(
                        f"集合 {COLLECTION_SEMANTIC_UNITS} 缺少 P2 字段 {missing}"
                        "（P1 旧集合）；请删除集合并重跑管线重建："
                        "pymilvus drop_collection('semantic_units')"
                    ),
                )
            client.load_collection(COLLECTION_SEMANTIC_UNITS)
            return

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("space_id", DataType.VARCHAR, max_length=64)
        # P2 租户隔离：检索 expr 叠加 tenant 过滤（租户 × 空间 × 角色）
        schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
        schema.add_field("asset_id", DataType.VARCHAR, max_length=64)
        schema.add_field("unit_type", DataType.VARCHAR, max_length=16)
        schema.add_field("model_version", DataType.VARCHAR, max_length=128)
        # BM25 输入字段：中文分析器（jieba）兼顾中英混合文本
        schema.add_field(
            "text",
            DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        )
        schema.add_field("text_dense", DataType.FLOAT_VECTOR, dim=self._dim)
        # P2-CORE-05 图文向量通道（clip_dense，BGE-VL 类；以文搜图）
        schema.add_field("clip_dense", DataType.FLOAT_VECTOR, dim=self._clip_dim)
        schema.add_field("sparse_bm25", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="text_bm25",
                function_type=FunctionType.BM25,
                input_field_names=["text"],
                output_field_names=["sparse_bm25"],
            )
        )
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="text_dense",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name="clip_dense",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name="sparse_bm25", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25"
        )
        client.create_collection(
            COLLECTION_SEMANTIC_UNITS, schema=schema, index_params=index_params
        )
        # 新建集合必须显式加载，否则紧随其后的写入/检索报 "collection not loaded"
        client.load_collection(COLLECTION_SEMANTIC_UNITS)
        logger.info(
            "milvus_collection_created", collection=COLLECTION_SEMANTIC_UNITS, dim=self._dim
        )

    # ---------- 写入 / 删除 ----------

    def upsert_units(self, rows: list[dict[str, Any]]) -> None:
        """行结构：id/space_id/tenant_id/asset_id/unit_type/model_version/text/
        text_dense（文档）+ 可选 clip_dense（图片）。"""
        if not rows:
            return
        client = self._ensure_client()
        client.upsert(COLLECTION_SEMANTIC_UNITS, rows)
        client.flush(COLLECTION_SEMANTIC_UNITS)

    def delete_asset_units(self, asset_id: uuid.UUID) -> None:
        client = self._ensure_client()
        if not client.has_collection(COLLECTION_SEMANTIC_UNITS):
            return
        client.delete(COLLECTION_SEMANTIC_UNITS, filter=f'asset_id == "{asset_id}"')
        client.flush(COLLECTION_SEMANTIC_UNITS)

    def delete_space_units(self, space_id: uuid.UUID) -> None:
        """空间删除级联：清理该空间全部向量。"""
        client = self._ensure_client()
        if not client.has_collection(COLLECTION_SEMANTIC_UNITS):
            return
        client.delete(COLLECTION_SEMANTIC_UNITS, filter=f'space_id == "{space_id}"')
        client.flush(COLLECTION_SEMANTIC_UNITS)

    # ---------- 检索（供 Retriever 调用） ----------

    def hybrid_search(
        self,
        *,
        query_vector: list[float],
        query_text: str,
        expr: str,
        limit: int,
        dense_top_k: int,
        sparse_top_k: int,
        rrf_k: int,
    ) -> list[dict[str, Any]]:
        """dense + BM25 两路召回，RRF 融合；返回按 RRF 序的 Milvus 行。"""
        client = self._ensure_client()
        reqs = [
            AnnSearchRequest(
                data=[query_vector],
                anns_field="text_dense",
                param={"metric_type": "IP", "params": {}},
                limit=dense_top_k,
                expr=expr,
            ),
            AnnSearchRequest(
                data=[query_text],
                anns_field="sparse_bm25",
                param={"metric_type": "BM25"},
                limit=sparse_top_k,
                expr=expr,
            ),
        ]
        rows = client.hybrid_search(
            COLLECTION_SEMANTIC_UNITS,
            reqs=reqs,
            ranker=RRFRanker(k=rrf_k),
            limit=limit,
            output_fields=[
                "space_id",
                "tenant_id",
                "asset_id",
                "unit_type",
                "model_version",
                "text",
            ],
            filter=expr,
        )
        # pymilvus 返回 List[List[dict]]：单查询取第一组
        return rows[0] if rows and isinstance(rows[0], list) else list(rows)

    def clip_search(
        self,
        *,
        query_vector: list[float],
        expr: str,
        limit: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """以文搜图：clip_dense 单路召回（P2-CORE-05/WK-01）。"""
        client = self._ensure_client()
        req = AnnSearchRequest(
            data=[query_vector],
            anns_field="clip_dense",
            param={"metric_type": "IP", "params": {}},
            limit=top_k,
            expr=expr,
        )
        rows = client.hybrid_search(
            COLLECTION_SEMANTIC_UNITS,
            reqs=[req],
            ranker=RRFRanker(k=60),
            limit=limit,
            output_fields=["space_id", "tenant_id", "asset_id", "unit_type", "text"],
            filter=expr,
        )
        return rows[0] if rows and isinstance(rows[0], list) else list(rows)

    def query_units(self, *, expr: str, limit: int) -> list[dict[str, Any]]:
        """标量查询（P3 L3 chunk 回收：按 id 清单取回候选行，非 ANN）。"""
        client = self._ensure_client()
        rows = client.query(
            COLLECTION_SEMANTIC_UNITS,
            filter=expr,
            output_fields=[
                "space_id",
                "tenant_id",
                "asset_id",
                "unit_type",
                "model_version",
                "text",
            ],
            limit=limit,
        )
        return list(rows)

    def community_search(
        self,
        *,
        query_vector: list[float],
        expr: str,
        limit: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        """L4 全局总结通道：community_summary 单元 dense 单路召回（P3-WRK-02）。"""
        client = self._ensure_client()
        if not client.has_collection(COLLECTION_SEMANTIC_UNITS):
            return []
        comm_expr = 'unit_type == "community"' + (f" and ({expr})" if expr else "")
        req = AnnSearchRequest(
            data=[query_vector],
            anns_field="text_dense",
            param={"metric_type": "IP", "params": {}},
            limit=top_k,
            expr=comm_expr,
        )
        rows = client.hybrid_search(
            COLLECTION_SEMANTIC_UNITS,
            reqs=[req],
            ranker=RRFRanker(k=60),
            limit=limit,
            output_fields=["space_id", "tenant_id", "asset_id", "unit_type", "text"],
            filter=comm_expr,
        )
        return rows[0] if rows and isinstance(rows[0], list) else list(rows)

    # ---------- P3 实体链接向量（entities 集合） ----------

    def entities_collection_exists(self) -> bool:
        return self._ensure_client().has_collection(COLLECTION_ENTITIES)

    def ensure_entities_collection(self) -> None:
        """entities 集合：id=entity_key，向量=实体链接向量（名称+类型+描述）。"""
        client = self._ensure_client()
        if client.has_collection(COLLECTION_ENTITIES):
            client.load_collection(COLLECTION_ENTITIES)
            return
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("space_id", DataType.VARCHAR, max_length=64)
        schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
        schema.add_field("name", DataType.VARCHAR, max_length=512)
        schema.add_field("entity_type", DataType.VARCHAR, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 200},
        )
        client.create_collection(COLLECTION_ENTITIES, schema=schema, index_params=index_params)
        client.load_collection(COLLECTION_ENTITIES)
        logger.info("milvus_collection_created", collection=COLLECTION_ENTITIES, dim=self._dim)

    def upsert_entities(self, rows: list[dict[str, Any]]) -> None:
        """行结构：id/space_id/tenant_id/name/entity_type/vector。"""
        if not rows:
            return
        client = self._ensure_client()
        client.upsert(COLLECTION_ENTITIES, rows)
        client.flush(COLLECTION_ENTITIES)

    def delete_space_entities(self, space_id: uuid.UUID) -> None:
        client = self._ensure_client()
        if not client.has_collection(COLLECTION_ENTITIES):
            return
        client.delete(COLLECTION_ENTITIES, filter=f'space_id == "{space_id}"')
        client.flush(COLLECTION_ENTITIES)

    def search_entities(
        self, *, query_vector: list[float], space_ids: list[uuid.UUID], top_k: int
    ) -> list[tuple[str, float]]:
        """实体链接检索：返回 [(entity_key, score)]（按相似度降序）。"""
        client = self._ensure_client()
        if not client.has_collection(COLLECTION_ENTITIES):
            return []
        req = AnnSearchRequest(
            data=[query_vector],
            anns_field="vector",
            param={"metric_type": "IP", "params": {}},
            limit=top_k,
            expr=_in_expr("space_id", [str(s) for s in space_ids]),
        )
        rows = client.hybrid_search(
            COLLECTION_ENTITIES,
            reqs=[req],
            ranker=RRFRanker(k=60),
            limit=top_k,
            output_fields=["name", "entity_type"],
        )
        flat = rows[0] if rows and isinstance(rows[0], list) else list(rows)
        return [(r["id"], float(r.get("distance", 0.0))) for r in flat]

    def fetch_entity_vectors(self, space_id: uuid.UUID) -> dict[str, list[float]]:
        """空间内实体向量全量拉取（合并候选的向量路；缺集合返回空）。"""
        client = self._ensure_client()
        if not client.has_collection(COLLECTION_ENTITIES):
            return {}
        rows = client.query(
            COLLECTION_ENTITIES,
            filter=f'space_id == "{space_id}"',
            output_fields=["vector"],
            limit=16384,
        )
        return {r["id"]: list(r["vector"]) for r in rows}

    def delete_community_units(self, space_id: uuid.UUID) -> None:
        """清空空间 community_summary 单元（社区任务重写前调用）。"""
        client = self._ensure_client()
        if not client.has_collection(COLLECTION_SEMANTIC_UNITS):
            return
        client.delete(
            COLLECTION_SEMANTIC_UNITS,
            filter=f'space_id == "{space_id}" and unit_type == "community"',
        )
        client.flush(COLLECTION_SEMANTIC_UNITS)

    # ---------- 异步门面 ----------

    async def async_ensure_collection(self) -> None:
        await asyncio.to_thread(self._guarded, self.ensure_collection)

    async def async_collection_exists(self) -> bool:
        return await asyncio.to_thread(self._guarded, self.collection_exists)

    async def async_upsert_units(self, rows: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._guarded, lambda: self.upsert_units(rows))

    async def async_delete_asset_units(self, asset_id: uuid.UUID) -> None:
        await asyncio.to_thread(self._guarded, lambda: self.delete_asset_units(asset_id))

    async def async_delete_space_units(self, space_id: uuid.UUID) -> None:
        await asyncio.to_thread(self._guarded, lambda: self.delete_space_units(space_id))

    async def async_clip_search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._guarded, lambda: self.clip_search(**kwargs))

    async def async_query_units(self, *, expr: str, ids: list[str]) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._guarded, lambda: self.query_units(expr=expr, limit=max(len(ids), 1))
        )

    async def async_community_search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._guarded, lambda: self.community_search(**kwargs))

    async def async_ensure_entities_collection(self) -> None:
        await asyncio.to_thread(self._guarded, self.ensure_entities_collection)

    async def async_upsert_entities(self, rows: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._guarded, lambda: self.upsert_entities(rows))

    async def async_delete_space_entities(self, space_id: uuid.UUID) -> None:
        await asyncio.to_thread(self._guarded, lambda: self.delete_space_entities(space_id))

    async def async_fetch_entity_vectors(self, space_id: uuid.UUID) -> dict[str, list[float]]:
        return await asyncio.to_thread(self._guarded, lambda: self.fetch_entity_vectors(space_id))

    async def async_delete_community_units(self, space_id: uuid.UUID) -> None:
        await asyncio.to_thread(self._guarded, lambda: self.delete_community_units(space_id))

    async def async_search_entities(
        self, *, query_vector: list[float], space_ids: list[uuid.UUID], top_k: int
    ) -> list[tuple[str, float]]:
        return await asyncio.to_thread(
            self._guarded,
            lambda: self.search_entities(
                query_vector=query_vector, space_ids=space_ids, top_k=top_k
            ),
        )

    def guarded(self, fn):
        """把底层异常收敛为 UpstreamUnavailableError（Retriever/健康检查共用）。"""
        return self._guarded(fn)

    def _guarded(self, fn):
        try:
            return fn()
        except UpstreamUnavailableError:
            raise
        except Exception as e:
            raise UpstreamUnavailableError(upstream="milvus", reason=str(e)) from e
