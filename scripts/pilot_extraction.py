"""图谱抽取试点（只读 DB、只读对象存储，不写任何数据）。

对若干篇已入库的文档，用**当前代码里的抽取 prompt**（build_extraction_messages）重新跑一遍
LLM 抽取，与库里已存的旧抽取（version_meta.graph.extraction）并排比较，评估新 prompt 是否
值得全量重抽。指标：每篇实体/关系数、孤立实体率、噪音率（entity_filter）、
跨文档复现率（实体名/别名在试点集内另一篇也出现）、类型分布。

会真实调用 LLM（走 admin/文件配置里的 ai.llm），文档越多越贵；默认 8 篇。
抽取领域配置默认取各资产所在空间当前生效的配置（graph/profiles），--profile 可统一指定。

用法：
    .venv/bin/python scripts/pilot_extraction.py                    # 默认挑 8 篇（长短混合）
    .venv/bin/python scripts/pilot_extraction.py --n 4
    .venv/bin/python scripts/pilot_extraction.py --asset <uuid> --asset <uuid>
    .venv/bin/python scripts/pilot_extraction.py --profile academic     # 统一用某个领域配置
    .venv/bin/python scripts/pilot_extraction.py --out /path/to/dir # 逐篇结果 JSON 落盘供人工抽查
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import Counter
from pathlib import Path

from sqlalchemy import select, text


def _norms(entities: list[dict]) -> list[set[str]]:
    from loomvec.core.graph.ontology import normalize_entity_name

    out = []
    for e in entities:
        s = {normalize_entity_name(e["name"])}
        s.update(normalize_entity_name(a) for a in e.get("aliases") or [])
        out.append({x for x in s if x})
    return out


def _metrics(per_doc: dict[str, dict], profile_of: dict) -> dict:
    """per_doc[aid] = {"entities": [...], "relations": [...]}；profile_of[aid] = 该篇的领域配置"""
    from loomvec.core.graph.entity_filter import is_blocked_relation, noise_reason
    from loomvec.core.graph.ontology import normalize_entity_name

    docs = list(per_doc)
    n_ent = sum(len(per_doc[a]["entities"]) for a in docs)
    n_rel = sum(len(per_doc[a]["relations"]) for a in docs)
    isolated = noisy = blocked = 0
    types: Counter[str] = Counter()
    name_docs: dict[str, set[str]] = {}
    for a in docs:
        used = set()
        for r in per_doc[a]["relations"]:
            used.add(normalize_entity_name(r["head"]))
            used.add(normalize_entity_name(r["tail"]))
            blocked += is_blocked_relation(r["type"], profile=profile_of[a])
        ents = per_doc[a]["entities"]
        for e, ns in zip(ents, _norms(ents), strict=True):
            types[e["type"]] += 1
            isolated += normalize_entity_name(e["name"]) not in used
            noisy += bool(
                noise_reason(e["name"], e["type"], e.get("description"), profile=profile_of[a])
            )
            for n in ns:
                name_docs.setdefault(n, set()).add(a)
    shared = 0
    for a in docs:
        ents = per_doc[a]["entities"]
        shared += sum(1 for ns in _norms(ents) if any(len(name_docs.get(n, ())) >= 2 for n in ns))
    k = max(len(docs), 1)
    return {
        "docs": len(docs),
        "entities_per_doc": round(n_ent / k, 1),
        "relations_per_doc": round(n_rel / k, 1),
        "isolated_rate": round(isolated / max(n_ent, 1), 3),
        "noise_rate": round(noisy / max(n_ent, 1), 3),
        "blocked_relation_rate": round(blocked / max(n_rel, 1), 3),
        "cross_doc_rate": round(shared / max(n_ent, 1), 3),
        "types": dict(types.most_common()),
    }


async def main(args: argparse.Namespace) -> None:
    from loomvec.core.ai import AiGateway
    from loomvec.core.config import apply_ai_overrides, get_settings
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.db.models import Space
    from loomvec.core.db.system_config import load_ai_overrides, load_extraction_profiles
    from loomvec.core.pipeline.base import resolve_extraction_profile
    from loomvec.core.pipeline.chunking import split_batches
    from loomvec.core.pipeline.extraction import (
        build_extraction_messages,
        parse_merged,
        sanitize_extraction,
    )
    from loomvec.core.storage import ObjectStorage

    settings = get_settings()
    engine, sf = create_engine_and_sessionmaker(settings.postgres)
    async with sf() as db:
        ai_settings = apply_ai_overrides(settings.ai, await load_ai_overrides(db))
        profiles = await load_extraction_profiles(db)
        if args.profile and args.profile not in profiles:
            raise SystemExit(f"未知抽取领域配置 {args.profile}，可选：{sorted(profiles)}")
        space_profile = {
            str(s.id): resolve_extraction_profile(settings, s, profiles, args.profile)
            for s in (await db.execute(select(Space))).scalars().all()
        }
        rows = (
            await db.execute(
                text(
                    """
            select distinct on (a.id) a.id::text, a.name, a.space_id::text,
                   v.version_meta->'parse'->>'md_key', v.version_meta->'graph'->'extraction'
            from asset a join asset_version v on v.asset_id = a.id
            where a.deleted_at is null and v.version_meta->'parse'->>'md_key' is not null
              and v.version_meta->'chunk'->>'llm_model' <> 'mock'
              and v.version_meta->'graph'->'extraction' is not null
            order by a.id, v.created_at desc"""
                )
            )
        ).all()
    await engine.dispose()
    by_id = {r[0]: r for r in rows}
    if args.asset:
        missing = [a for a in args.asset if a not in by_id]
        if missing:
            raise SystemExit(
                "以下资产不可试抽（须存在且未删除、已解析、做过真实 LLM 抽取且库里有暂存抽取）："
                + "、".join(missing)
            )
        picked = [by_id[a] for a in args.asset]
    else:  # 按旧抽取实体数分位均匀挑，覆盖长短文章；固定种子可复现
        ordered = sorted(rows, key=lambda r: len(r[4]["entities"]))
        step = max(len(ordered) // args.n, 1)
        picked = ordered[step // 2 :: step][: args.n]
        random.Random(args.seed).shuffle(picked)

    storage = ObjectStorage(settings.storage)
    ai = AiGateway(ai_settings)
    sem = asyncio.Semaphore(settings.pipeline.llm_concurrency)
    old: dict[str, dict] = {}
    new: dict[str, dict] = {}
    names: dict[str, str] = {}
    profile_of: dict = {}

    async def one_batch(lines, s, e, profile):
        async with sem:
            msgs = build_extraction_messages(
                lines,
                s,
                e,
                min_chars=settings.pipeline.chunk_min_chars,
                max_chars=settings.pipeline.chunk_max_chars,
                max_line_chars=settings.pipeline.llm_batch_chars,
                profile=profile,
            )
            try:
                return parse_merged(
                    await ai.complete_json(msgs), batch_start=s, batch_end=e, profile=profile
                )
            except Exception as ex:  # 单批失败不拖垮整篇试点
                print(f"  batch {s}-{e} failed: {ex}")
                return None

    for aid, name, space_id, md_key, ex in picked:
        profile = space_profile[space_id]
        print(
            f"→ {name[:70]}  [{profile.key}]"
            f"  (旧: {len(ex['entities'])} 实体 / {len(ex['relations'])} 关系)"
        )
        names[aid] = name
        profile_of[aid] = profile
        old[aid] = {"entities": ex["entities"], "relations": ex["relations"]}
        md = (await storage.get_object(settings.storage.bucket_derived, md_key)).decode()
        lines = md.split("\n")
        batches = split_batches(lines, settings.pipeline.llm_batch_chars)
        results = await asyncio.gather(*(one_batch(lines, s, e, profile) for s, e in batches))
        ents, rels = [], []
        for m in results:
            if m is None or m.entities is None:
                continue
            ents.extend(m.entities)
            rels.extend(m.relations or [])
        ents, rels = sanitize_extraction(ents, rels, drop_isolated=True)
        new[aid] = {
            "entities": [
                {
                    "name": e.name,
                    "type": e.type,
                    "description": e.description,
                    "aliases": e.aliases,
                }
                for e in ents
            ],
            "relations": [{"head": r.head, "tail": r.tail, "type": r.type} for r in rels],
        }
        print(f"  新: {len(ents)} 实体 / {len(rels)} 关系（{len(batches)} 批）")

    m_old, m_new = _metrics(old, profile_of), _metrics(new, profile_of)
    print("\n指标对比（库内旧抽取 → 当前 prompt 新抽取）：")
    for k in m_old:
        if k != "types":
            print(f"  {k:24s} {m_old[k]!s:>8} → {m_new[k]!s}")
    print("  旧类型:", m_old["types"])
    print("  新类型:", m_new["types"])

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for aid in new:
            (out / f"{aid}.json").write_text(
                json.dumps(
                    {"name": names[aid], "old": old[aid], "new": new[aid]},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        (out / "metrics.json").write_text(
            json.dumps({"old": m_old, "new": m_new}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n逐篇结果已写入 {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--asset", action="append", default=[])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="")
    ap.add_argument("--profile", default="", help="统一使用的抽取领域配置 key（默认按空间）")
    asyncio.run(main(ap.parse_args()))
