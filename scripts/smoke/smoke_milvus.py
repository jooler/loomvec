"""P0-INF-06 冒烟：Milvus 可建集合（dense + BM25 稀疏 Function，2.6 混合检索前置）。

用法：uv run python scripts/smoke/smoke_milvus.py [--uri http://localhost:39530]
（建临时集合 → 插入 → load → search → drop）
"""

from __future__ import annotations

import argparse
import sys
import uuid

URI = "http://localhost:39530"
COLLECTION = "smoke_loomvec_collection"


def main(uri: str) -> int:
    from pymilvus import (
        DataType,
        Function,
        FunctionType,
        MilvusClient,
    )

    client = MilvusClient(uri=uri)

    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)

    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
    schema.add_field("text", DataType.VARCHAR, max_length=2048, enable_analyzer=True)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=8)
    schema.add_field("sparse_bm25", DataType.SPARSE_FLOAT_VECTOR)
    # BM25 Function：Milvus 2.6 内置分词生成稀疏向量（02 文档集合设计）
    schema.add_function(
        Function(
            name="text_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["text"],
            output_field_names=["sparse_bm25"],
        )
    )
    index_params = client.prepare_index_params()
    index_params.add_index(field_name="dense", index_type="FLAT", metric_type="IP")
    index_params.add_index(
        field_name="sparse_bm25", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25"
    )

    client.create_collection(COLLECTION, schema=schema, index_params=index_params)
    assert client.has_collection(COLLECTION), "集合创建失败"

    try:
        doc_id = str(uuid.uuid4())
        client.insert(
            COLLECTION,
            [
                {
                    "id": doc_id,
                    "text": "LoomVec 冒烟测试：语义检索与 BM25 混合召回。",
                    "dense": [0.1] * 8,
                }
            ],
        )
        client.flush(COLLECTION)
        client.load_collection(COLLECTION)
        results = client.search(
            COLLECTION,
            data=[[0.1] * 8],
            anns_field="dense",  # 多向量字段（dense+BM25）必须显式指定
            limit=1,
            output_fields=["text"],
            search_params={"metric_type": "IP", "params": {}},
        )
        assert results and len(results[0]) >= 1, "dense 检索无结果"
        print(f"[smoke:milvus] OK — 集合创建/插入/检索成功（{COLLECTION}）")
    finally:
        client.drop_collection(COLLECTION)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=URI)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.uri))
    except Exception as e:
        print(f"[smoke:milvus] FAIL — {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
