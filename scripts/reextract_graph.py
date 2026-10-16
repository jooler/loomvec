"""按当前 prompt 版本与空间生效的抽取领域配置，分批重抽存量资产的分片 + 图谱（可续跑）。

做什么：对指定空间里的资产，从 chunk 步起重跑（`chunk_strategy=llm, graph_extraction=on`），
chunk 之后的 graph/embed/index 随管线自动重建。走低优先级队列，且**限制同时在途的资产数**——
每篇文档内部还有 `pipeline.llm_concurrency` 个并发 LLM 调用，一次性全放出会打满 LLM 网关。

**默认 dry-run**（只读库、只列出将处理的资产）；`--apply` 才会改状态并派发任务调 LLM。

待重抽集合（`classify_asset`）：
- 已完成而跳过：status=ready，且按当前 prompt 版本、按该空间当前生效的抽取领域配置
  （extraction_profile 与内容指纹 extraction_profile_fingerprint 都相同）做过真实 LLM 抽取
  （非 mock、非规则分片），图谱没有降级，且**没有失败批次**（version_meta.chunk 的
  extraction_failed_batches>0 表示有批次 LLM 调用失败/抽取无效而无实体；旧记录没有该字段时看
  fallback_batches，见 pipeline.base.failed_extraction_batches；有失败批次的资产 chunk 步不会命中
  分片缓存，重抽真的会补抽）；中断后重新执行同一命令即可接着跑；
- 不碰在途：status 为 pending/processing 的资产（正在入库或被别处重跑）只在输出里报数量，
  派发前还会再核一次当前状态——对在途资产再派发会让两条管线并发写同一资产；
- 不碰未解析：version_meta 里没有 parse 的资产（从未解析成功）从 chunk 起跑没有输入，
  需先走正常入库/重试，不在本脚本范围；
- 空间有效分片策略为 structural 或图谱抽取关闭（含全局默认）时整空间默认跳过，
  `--force-llm` 才强制按 llm + 抽取重抽。

注意：重跑会**替换**该资产的语义单元（分片）并重做嵌入/索引；旧图谱边在 graph 步按资产覆盖，
但旧实体主表行 / Milvus 向量要等全部重抽完后用 scripts/gc_entities.py 清理。

并发上限：同时在途的 LLM 调用数 ≈ `--inflight` × `pipeline.llm_concurrency`（默认 8）。
LLM 网关（acp-gateway）同时活跃的运行时 `runtime.max_active_runtimes` 默认 32，且线上入库也在
用，超出会 503、让该批退化为规则分片。故 `--inflight` 默认 2（≈16 个调用，给在线入库留一半），
并以 `--max-llm-calls`（默认 16）为硬上限：`--inflight × llm_concurrency` 超过它直接拒绝运行。

用法：
    .venv/bin/python scripts/reextract_graph.py --space <uuid>                  # dry-run 列出
    .venv/bin/python scripts/reextract_graph.py --space <uuid> --apply --limit 5   # 先跑 5 篇
    .venv/bin/python scripts/reextract_graph.py --space <uuid> --space <uuid2> --apply --inflight 2
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from loomvec.core.constants import QUEUE_PIPELINE_LOW
from loomvec.core.pipeline.base import failed_extraction_batches

# 管线正在处理或排队中的资产状态：不能再派发重抽
IN_FLIGHT_STATUSES = ("pending", "processing")
# 图谱写入降级/失败：该资产的图谱不完整，需要重抽
GRAPH_INCOMPLETE_STATUSES = ("pending_retry", "failed")

_SELECT = text(
    """
    select a.id, a.name, a.status::text status, a.graph_status::text graph_status,
           (v.version_meta->'parse') is not null has_parse,
           v.version_meta->'chunk'->>'prompt_version' prompt_version,
           v.version_meta->'chunk'->>'extraction_profile' profile_key,
           v.version_meta->'chunk'->>'extraction_profile_fingerprint' profile_fp,
           v.version_meta->'chunk'->>'llm_model' llm_model,
           v.version_meta->'graph'->>'reason' graph_reason,
           v.version_meta->'chunk' chunk_meta,
           (v.version_meta->'graph'->'extraction') is not null has_extraction
    from asset a
    left join lateral (select * from asset_version where asset_id = a.id
                       order by created_at desc limit 1) v on true
    where a.space_id = :sid and a.deleted_at is null
      and a.mime_type not like 'image/%' and a.mime_type not like 'video/%'
      and a.mime_type not like 'audio/%'
    order by a.created_at
    """
)


_POLL = text(
    """
    select a.id, a.status::text, a.graph_status::text, a.status_reason,
           v.version_meta->'chunk' chunk_meta,
           (v.version_meta->'graph'->'extraction') is not null has_extraction
    from asset a
    left join lateral (select version_meta from asset_version where asset_id = a.id
                       order by created_at desc limit 1) v on true
    where a.id = any(:ids)
    """
)


@dataclass(frozen=True)
class AssetState:
    """判定重抽所需的资产状态（来自资产行与最新版本的 version_meta）。"""

    status: str
    graph_status: str | None = None
    has_parse: bool = False
    prompt_version: str | None = None
    profile_key: str | None = None
    profile_fp: str | None = None
    llm_model: str | None = None
    graph_reason: str | None = None
    chunk_meta: dict | None = None  # version_meta.chunk（含 extraction_failed_batches 等）
    has_extraction: bool = False  # version_meta.graph.extraction 是否存在


def classify_asset(
    a: AssetState, *, prompt_version: str, profile_key: str, profile_fingerprint: str
) -> str:
    """返回 "in_flight"（在途，不碰）/ "unparsed"（从未解析，不碰）/ "done"（已完成，跳过）/
    "todo"（待重抽）。"""
    if a.status in IN_FLIGHT_STATUSES:
        return "in_flight"
    if not a.has_parse:
        return "unparsed"
    failed = failed_extraction_batches(a.chunk_meta, has_extraction=a.has_extraction)
    done = (
        a.status == "ready"
        and a.prompt_version == prompt_version
        and (a.profile_key, a.profile_fp) == (profile_key, profile_fingerprint)
        and a.llm_model != "mock"
        and a.graph_reason is None
        and a.graph_status not in GRAPH_INCOMPLETE_STATUSES
        and failed == 0
    )
    return "done" if done else "todo"


def concurrency_error(inflight: int, llm_concurrency: int, max_llm_calls: int) -> str | None:
    """同时在途 LLM 调用数（inflight × 每篇内部并发）超过上限时返回中文错误信息。"""
    calls = inflight * llm_concurrency
    if calls > max_llm_calls:
        return (
            f"同时在途的 LLM 调用数 {inflight} × {llm_concurrency} = {calls} 超过上限 "
            f"--max-llm-calls={max_llm_calls}（网关 runtime.max_active_runtimes 默认 32，"
            "且线上入库也在占用）；请调小 --inflight"
        )
    return None


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("须 >= 1")
    return n


class _LowQueue:
    """把 retry_asset 的 send_task 改投低优先级队列（默认是交互优先队列）。"""

    def __init__(self, celery) -> None:
        self._celery = celery

    def send_task(self, name, *, args=None, kwargs=None, queue=None, **kw):
        return self._celery.send_task(
            name, args=args, kwargs=kwargs, queue=QUEUE_PIPELINE_LOW, **kw
        )


async def main(args: argparse.Namespace) -> None:
    from loomvec.api.services.assets import retry_asset
    from loomvec.core.config import get_settings
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.db.models import Space
    from loomvec.core.db.system_config import load_extraction_profiles
    from loomvec.core.pipeline.base import (
        resolve_chunk_strategy,
        resolve_extraction_profile,
        resolve_graph_extraction,
    )

    settings = get_settings()
    err = concurrency_error(args.inflight, settings.pipeline.llm_concurrency, args.max_llm_calls)
    if err:
        raise SystemExit(err)
    current_pv = settings.pipeline.prompt_version
    engine, sf = create_engine_and_sessionmaker(settings.postgres)

    todo: list[tuple[uuid.UUID, str]] = []
    counts = {"done": 0, "in_flight": 0, "unparsed": 0}
    async with sf() as db:
        profiles = await load_extraction_profiles(db)
        for sid in args.space:
            space = await db.get(Space, uuid.UUID(sid))
            if space is None or space.deleted_at is not None:
                print(f"空间 {sid} 不存在或已删除，跳过")
                continue
            strategy = resolve_chunk_strategy(settings, space)
            graph_on = resolve_graph_extraction(settings, space)
            if (strategy == "structural" or not graph_on) and not args.force_llm:
                print(
                    f"空间 {sid} 生效配置为 chunk_strategy={strategy}、graph_extraction="
                    f"{'on' if graph_on else 'off'}，不做 LLM 抽取，跳过（--force-llm 可强制重抽）"
                )
                continue
            profile = resolve_extraction_profile(settings, space, profiles)
            print(f"空间 {sid}  抽取领域配置 {profile.key}@{profile.fingerprint}")
            rows = (await db.execute(_SELECT, {"sid": sid})).mappings().all()
            for r in rows:
                state = AssetState(**{k: v for k, v in r.items() if k not in ("id", "name")})
                kind = classify_asset(
                    state,
                    prompt_version=current_pv,
                    profile_key=profile.key,
                    profile_fingerprint=profile.fingerprint,
                )
                if kind == "todo":
                    todo.append((r["id"], r["name"]))
                else:
                    counts[kind] += 1
    if args.limit:
        todo = todo[: args.limit]
    print(
        f"prompt={current_pv}  待处理 {len(todo)}  已完成跳过 {counts['done']}"
        f"  在途（pending/processing，未碰）{counts['in_flight']}"
        f"  未解析（未碰）{counts['unparsed']}"
    )
    if not args.apply:
        for aid, name in todo:
            print(f"  {aid}  {name[:80]}")
        print("dry-run：未做任何改动；确认无误后加 --apply 执行")
        await engine.dispose()
        return

    from loomvec.worker.celery_app import celery_app

    celery = _LowQueue(celery_app)
    pending = list(todo)
    inflight: dict[uuid.UUID, tuple[str, float]] = {}
    ok = failed = skipped = incomplete = 0
    try:
        while pending or inflight:
            while pending and len(inflight) < args.inflight:
                aid, name = pending.pop(0)
                async with sf() as db:
                    # 列出之后资产可能进入在途（目录监听入库、别处重跑），派发前再核一次
                    status = (
                        await db.execute(
                            text("select status::text from asset where id = :id"), {"id": aid}
                        )
                    ).scalar_one_or_none()
                    if status is None or status in IN_FLIGHT_STATUSES:
                        skipped += 1
                        print(f"- 跳过 {name[:70]}  当前状态 {status}", flush=True)
                        continue
                    await retry_asset(
                        db,
                        celery=celery,
                        asset_id=aid,
                        step="chunk",
                        chunk_strategy="llm",
                        graph_extraction="on",
                    )
                inflight[aid] = (name, time.time())
                print(
                    f"→ 派发 {name[:70]}  (在途 {len(inflight)}, 剩余 {len(pending)})", flush=True
                )
            if not inflight:
                continue
            await asyncio.sleep(args.poll)
            async with sf() as db:
                rows = (await db.execute(_POLL, {"ids": list(inflight)})).all()
            for aid, status, gstatus, reason, chunk_meta, has_extraction in rows:
                if status not in ("ready", "failed"):
                    continue
                failed_batches = failed_extraction_batches(
                    chunk_meta, has_extraction=has_extraction
                )
                name, t0 = inflight.pop(aid)
                if status == "ready":
                    ok += 1
                else:
                    failed += 1
                note = ""
                if status == "ready" and failed_batches:
                    incomplete += 1
                    note = f" ⚠ {failed_batches} 批抽取失败，需再跑一轮"
                print(
                    f"{'✓' if status == 'ready' else '✗'} {name[:70]}  {status}/graph={gstatus}"
                    f"  {time.time() - t0:.0f}s {reason or ''}{note}",
                    flush=True,
                )
    finally:
        await engine.dispose()
    print(
        f"完成：成功 {ok}（其中有失败批次 {incomplete}，重跑同一命令会再处理），"
        f"失败 {failed}，派发前跳过 {skipped}"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--space", action="append", required=True)
    ap.add_argument("--apply", action="store_true", help="真正派发重抽（默认只 dry-run 列出）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理的资产数（0=不限）")
    ap.add_argument(
        "--inflight",
        type=_positive_int,
        default=2,
        help="同时在途的资产数（默认 2；每篇内部还有 pipeline.llm_concurrency 个并发 LLM 调用）",
    )
    ap.add_argument(
        "--max-llm-calls",
        type=_positive_int,
        default=16,
        help="同时在途 LLM 调用数硬上限（默认 16，约为网关 max_active_runtimes=32 的一半，"
        "另一半留给在线入库）；--inflight × llm_concurrency 超过则拒绝运行",
    )
    ap.add_argument("--poll", type=float, default=15.0, help="轮询间隔秒数")
    ap.add_argument(
        "--force-llm",
        action="store_true",
        help="对生效分片策略为 structural 或图谱抽取关闭的空间也强制按 llm + 抽取重抽",
    )
    a = ap.parse_args()
    if a.poll <= 0:
        ap.error("--poll 须 > 0")
    for s in a.space:
        uuid.UUID(s)
    asyncio.run(main(a))
