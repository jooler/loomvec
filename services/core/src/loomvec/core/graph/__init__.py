"""P3-CORE-01 图谱域：AGE 访问层、实体规范化与确定性 ID、实体合并、社区。

- `age.AgeStore`：openCypher 经 ag_catalog.cypher() 在 PG 事务内执行——
  图数据与资产/语义单元同库，资产删除可同事务级联清理（03 文档 §3.4）；
- `ontology`：实体名规范化与确定性 entity_key（hash(space, canonical_name, type)）；
- `merge.EntityMergeService`：阶段二空间级异步合并与 merge_log 回滚
  （阶段一内联链接 + 资产图写入在 `pipeline/graph_flow.py` 的 EntityGraphService）；
- `communities`：Leiden 社区检测 + LLM 摘要（P3-WRK-02）。
"""

from __future__ import annotations

from loomvec.core.graph.age import AgeStore
from loomvec.core.graph.ontology import entity_embed_text, entity_key, normalize_entity_name

__all__ = [
    "AgeStore",
    "entity_embed_text",
    "entity_key",
    "normalize_entity_name",
]
