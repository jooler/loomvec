"""图谱实体噪音审计（只读，不改库）。

用 core/graph/entity_filter 的确定性规则扫描存量 entity / 关系，按规则分桶统计，
评估「入库前过滤」或「存量清洗」能去掉多少，不做任何写操作。规则按实体所在空间
当前生效的抽取领域配置（graph/profiles）选取：通用层 + 该配置是否启用论文元数据词表、
该配置的关系拦截集合。

用法：
    .venv/bin/python scripts/audit_entities.py              # 汇总统计
    .venv/bin/python scripts/audit_entities.py --samples 5  # 每个规则附 5 个样例
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict

from sqlalchemy import select, text


async def main(samples: int) -> None:
    from loomvec.core.config import get_settings
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.db.models import Space
    from loomvec.core.db.system_config import load_extraction_profiles
    from loomvec.core.graph.entity_filter import is_blocked_relation, noise_reason
    from loomvec.core.pipeline.base import resolve_extraction_profile

    settings = get_settings()
    engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
    try:
        async with session_factory() as db:
            profiles = await load_extraction_profiles(db)
            spaces = (
                (await db.execute(select(Space).where(Space.deleted_at.is_(None)))).scalars().all()
            )
            profile_of = {s.id: resolve_extraction_profile(settings, s, profiles) for s in spaces}
            rows = (
                await db.execute(
                    text(
                        "select space_id, name, type, description from entity "
                        "where merged_into is null and deleted_at is null"
                    )
                )
            ).all()
            rel_rows = (
                await db.execute(
                    text(
                        "select a.space_id, r->>'type', count(*) "
                        "from asset_version v join asset a on a.id = v.asset_id, "
                        "jsonb_array_elements("
                        "v.version_meta->'graph'->'extraction'->'relations') r "
                        "group by 1, 2"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    # 软删空间的实体 / 暂存关系不审计（它们不会再被查询或重抽）
    rows = [r for r in rows if r[0] in profile_of]
    rel_rows = [r for r in rel_rows if r[0] in profile_of]
    print("空间生效的抽取领域配置：", dict(Counter(p.key for p in profile_of.values())))

    buckets: Counter[str] = Counter()
    by_type: Counter[str] = Counter(t for _, _, t, _ in rows)
    examples: dict[str, list[str]] = defaultdict(list)
    for space_id, name, etype, desc in rows:
        profile = profile_of[space_id]
        reason = noise_reason(name, etype, desc, profile=profile)
        if reason:
            buckets[reason] += 1
            if len(examples[reason]) < samples:
                examples[reason].append(f"[{profile.key}/{etype}] {name} | {(desc or '')[:50]}")

    total = len(rows)
    noisy = sum(buckets.values())
    print(f"有效实体 {total}；命中噪音规则 {noisy}（{noisy / max(total, 1):.1%}）")
    print("\n按类型：", dict(by_type.most_common()))
    print("\n按规则：")
    for reason, n in buckets.most_common():
        print(f"  {reason:34s} {n}")
        for ex in examples[reason]:
            print(f"      {ex}")

    rel_total = sum(n for _, _, n in rel_rows)
    blocked: Counter[str] = Counter()
    for space_id, t, n in rel_rows:
        if is_blocked_relation(t, profile=profile_of[space_id]):
            blocked[t] += n
    print(f"\n关系（asset_version 暂存抽取）{rel_total} 条，被拦截类型 {sum(blocked.values())} 条")
    for t, n in blocked.most_common():
        print(f"  {t:20s} {n}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=0, help="每个规则附带的样例数")
    asyncio.run(main(ap.parse_args().samples))
