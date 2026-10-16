"""清理图谱里的陈旧实体（重抽完成后用）。

重抽只会按资产覆盖 AGE 里的边；旧实体主表行（PG entity）、无边的 AGE 孤立节点、
Milvus entities 向量会残留，并占用查询侧实体链接的 top-k 名额（graph.link_top_k）。
本脚本按空间收敛三处：保留集 = 当前 AGE 里**至少有一条边**的节点 key，其余视为陈旧：

- AGE：删除无边的孤立节点；
- PG entity：软删（deleted_at）未被合并的陈旧实体行（合并墓碑 merged_into 不动）；
- Milvus entities：删除这些实体的向量。

默认 dry-run 只统计；`--apply` 才执行。**务必等该空间的资产全部重抽完再 apply**，
否则尚未重抽的资产的实体会被当成陈旧误删。

用法：
    .venv/bin/python scripts/gc_entities.py --space <uuid>            # dry-run
    .venv/bin/python scripts/gc_entities.py --space <uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Iterable

from sqlalchemy import text


def plan_gc(
    node_keys: Iterable[str], edges: Iterable, active_entity_keys: Iterable[str]
) -> tuple[set[str], list[str], list[str]]:
    """保留集 = 至少有一条边的节点 key；返回 (保留集, AGE 孤立节点, PG 陈旧实体)。

    edges 的元素带 head_key / tail_key（`AgeStore.load_space_edges` 的返回）；
    active_entity_keys 为 PG 里未软删、未被合并的实体 key。
    """
    with_edge = {e.head_key for e in edges} | {e.tail_key for e in edges}
    isolated = [k for k in node_keys if k not in with_edge]
    stale = [k for k in active_entity_keys if k not in with_edge]
    return with_edge, isolated, stale


async def main(args: argparse.Namespace) -> None:
    from loomvec.core.config import get_settings
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.graph.age import AgeStore
    from loomvec.core.retrieval.store import MilvusStore

    settings = get_settings()
    engine, sf = create_engine_and_sessionmaker(settings.postgres)
    age = AgeStore()
    milvus = MilvusStore(settings.milvus, settings.ai)
    try:
        for sid in args.space:
            async with sf() as db:
                nodes = await age.load_space_nodes(db, sid)
                edges = await age.load_space_edges(db, sid)
                rows = (
                    (
                        await db.execute(
                            text(
                                "select entity_key from entity where space_id = :sid "
                                "and deleted_at is null and merged_into is null"
                            ),
                            {"sid": sid},
                        )
                    )
                    .scalars()
                    .all()
                )
                with_edge, isolated, stale = plan_gc(nodes, edges, rows)
                print(
                    f"空间 {sid[:8]}: PG 活跃实体 {len(rows)}，AGE 节点 {len(nodes)}"
                    f"（有边 {len(with_edge)}，孤立 {len(isolated)}），边 {len(edges)}"
                    f" → 陈旧实体 {len(stale)}"
                )
                if not args.apply:
                    continue
                if isolated:
                    await age.delete_orphan_nodes(db, sid, isolated)
                if stale:
                    for i in range(0, len(stale), 1000):
                        await db.execute(
                            text(
                                "update entity set deleted_at = now() "
                                "where space_id = :sid and entity_key = any(:keys) "
                                "and merged_into is null"
                            ),
                            {"sid": sid, "keys": stale[i : i + 1000]},
                        )
                await db.commit()
            await milvus.async_delete_entities(list(set(stale) | set(isolated)))
            print(f"  已清理：AGE 孤立 {len(isolated)}，PG 软删 {len(stale)}，Milvus 向量同步删除")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--space", action="append", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    for s in a.space:
        uuid.UUID(s)
    asyncio.run(main(a))
