"""P1-CORE-03 检索层包：Milvus 集合管理 + 混合检索器 + 高亮。

模块划分：
- store.py      MilvusStore（建集合/写入/删除/混合检索，pymilvus 同步 SDK 经线程池暴露异步）
- retriever.py  Retriever（dense+BM25 → RRF → 可选 rerank → SemanticHit）
- highlight.py  查询词高亮（空白分词 + CJK 2-gram 回退）

集合设计与 scope 语义见 02/03 文档；P2 多空间时 scope 过滤在 Retriever 内扩展。
"""

from loomvec.core.retrieval.highlight import make_highlight
from loomvec.core.retrieval.retriever import Retriever, SemanticHit
from loomvec.core.retrieval.store import COLLECTION_SEMANTIC_UNITS, MilvusStore

__all__ = [
    "COLLECTION_SEMANTIC_UNITS",
    "MilvusStore",
    "Retriever",
    "SemanticHit",
    "make_highlight",
]
