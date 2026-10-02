"""修复解析产物中的内嵌图片 base64（MinerU 4.0.10 单独 markdown 产物遗留）。

此前入库解析取 V1 单独 markdown 产物，图片以 data URI 内嵌（体积可达正文数十倍），
派生的语义单元/向量/图谱随之污染，分片抽取按输入 token 计费。现解析改取 zip 产物
（docs/03 §一），本脚本处理已落库的存量：

1. 扫描各资产最新版本的解析 Markdown（version_meta.parse.md_key），找出含 data:image 的；
2. `--apply`：原文备份到同桶 `<md_key>.inline-images.bak`（已有备份不覆盖），写回
   `sanitize_text` 清洗后的 Markdown（行数不变，行号/页码映射稳定）；
3. 对状态为 ready / processing / failed 的命中资产以 from_step=chunk 重新入队
   （低优队列、按 --stagger-seconds 错峰），重建语义单元、图谱、向量与索引。

用法：
    uv run python scripts/repair_inline_images.py              # 预览（不改任何东西）
    uv run python scripts/repair_inline_images.py --apply      # 执行（会产生 LLM 调用费用）
"""

from __future__ import annotations

import argparse
import asyncio
import sys

BACKUP_SUFFIX = ".inline-images.bak"


async def _scan_and_repair(apply: bool, stagger_seconds: float) -> int:
    from celery import Celery
    from sqlalchemy import select

    from loomvec.core.config import get_settings
    from loomvec.core.constants import QUEUE_PIPELINE_LOW
    from loomvec.core.db.base import create_engine_and_sessionmaker
    from loomvec.core.db.models import Asset, AssetStatus, AssetVersion
    from loomvec.core.pipeline.chunking import split_batches
    from loomvec.core.pipeline.pages import sanitize_text
    from loomvec.core.storage import ObjectStorage

    settings = get_settings()
    storage = ObjectStorage(settings.storage)
    bucket = settings.storage.bucket_derived
    engine, session_factory = create_engine_and_sessionmaker(settings.postgres)
    try:
        async with session_factory() as session:
            latest = (
                select(AssetVersion.asset_id, AssetVersion.version_meta)
                .distinct(AssetVersion.asset_id)
                .order_by(AssetVersion.asset_id, AssetVersion.version.desc())
                .subquery()
            )
            rows = (
                await session.execute(
                    select(Asset.id, Asset.name, Asset.status, latest.c.version_meta)
                    .join(latest, latest.c.asset_id == Asset.id)
                    .where(Asset.deleted_at.is_(None))
                )
            ).all()
    finally:
        await engine.dispose()

    hits: list[tuple] = []
    for asset_id, name, status, meta in rows:
        md_key = ((meta or {}).get("parse") or {}).get("md_key")
        if not md_key:
            continue
        raw = await storage.get_object(bucket, md_key)
        if b"data:image/" not in raw:
            continue
        original = raw.decode("utf-8", errors="replace")
        cleaned = sanitize_text(original)
        hits.append((asset_id, name, status, md_key, raw, original, cleaned))

    batch_chars = settings.pipeline.llm_batch_chars
    total_before = total_after = 0
    for _aid, name, status, _key, _raw, original, cleaned in hits:
        total_before += len(original)
        total_after += len(cleaned)
        before = len(split_batches(original.split("\n"), batch_chars))
        after = len(split_batches(cleaned.split("\n"), batch_chars))
        print(
            f"[{status}] {name[:60]} | {len(original):,} → {len(cleaned):,} 字"
            f" | 分片批 {before} → {after}"
        )
    print(f"共 {len(hits)} 个资产；Markdown 合计 {total_before:,} → {total_after:,} 字")

    rerun = {AssetStatus.READY, AssetStatus.PROCESSING, AssetStatus.FAILED}
    if not apply:
        n = sum(1 for h in hits if h[2] in rerun)
        print(f"预览模式：未改动。--apply 将清洗以上 Markdown 并重跑其中 {n} 个资产（from chunk）")
        return 0

    celery = Celery("loomvec", broker=settings.redis.url, backend=settings.redis.url)
    queued = 0
    for asset_id, name, status, md_key, raw, _original, cleaned in hits:
        backup_key = md_key + BACKUP_SUFFIX
        if await storage.head_object(bucket, backup_key) is None:  # 已有备份不覆盖（保留最初原文）
            await storage.put_object(bucket, backup_key, raw, content_type="text/markdown")
        await storage.put_object(bucket, md_key, cleaned.encode(), content_type="text/markdown")
        if status in rerun:
            celery.send_task(
                "pipeline.process_asset",
                args=[str(asset_id), "chunk"],
                queue=QUEUE_PIPELINE_LOW,
                countdown=queued * stagger_seconds,
            )
            queued += 1
        print(f"已清洗{'并入队' if status in rerun else ''}：{name[:60]}")
    print(f"完成：清洗 {len(hits)} 个，重跑入队 {queued} 个（低优队列，间隔 {stagger_seconds}s）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="执行清洗与重跑（默认仅预览）")
    parser.add_argument(
        "--stagger-seconds", type=float, default=60.0, help="重跑任务入队错峰间隔（秒）"
    )
    args = parser.parse_args()
    return asyncio.run(_scan_and_repair(args.apply, args.stagger_seconds))


if __name__ == "__main__":
    sys.exit(main())
