"""管线共享定义：步骤表、运行依赖、幂等缓存键、图片单元文本。

拆分说明（P2 重构）：steps.py 保留四步的文档流实现，图片分支在 image_flow.py；
本模块承载两者共用的常量与数据结构，避免相互导入成环。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from loomvec.core.constants import (
    CHUNK_STRATEGIES,
    DEFAULT_EXTRACTION_PROFILE,
    GRAPH_EXTRACTION_MODES,
)
from loomvec.core.db.models import Asset, Space
from loomvec.core.graph.profiles import ExtractionProfile
from loomvec.core.mineru_client import MineruClient
from loomvec.core.retrieval import MilvusStore
from loomvec.core.storage import ObjectStorage

if TYPE_CHECKING:
    from loomvec.core.ai import AiGateway
    from loomvec.core.config import Settings

logger = structlog.get_logger("loomvec.pipeline")

# P3 起插入 graph 步骤（chunk 的合并抽取结果 → 图谱写入），位于 embed 之前：
# 图失败降级不阻断 embed/index；from_step 单步重跑语义不变。
PIPELINE_STEPS = ["parse", "chunk", "graph", "embed", "index"]
LAST_STEP = PIPELINE_STEPS[-1]


@dataclass
class PipelineDeps:
    """管线运行依赖（worker / 集成测试各自装配）。

    注意：session_factory/redis/httpx 客户端均绑定创建时的事件循环——
    Celery 任务每次 asyncio.run 都是独立循环，必须逐任务装配并在结束时释放。
    """

    settings: Settings
    session_factory: Any
    storage: ObjectStorage
    ai: AiGateway
    mineru: MineruClient
    milvus: MilvusStore
    redis: Any | None = None  # 可选：事件发布（redis.asyncio 客户端）
    engine: Any | None = None  # 任务结束时 dispose


# 图片单元 embed_model_version 前缀（区别于文档通道的裸模型名）；媒体流复用
IMAGE_UNIT_MODEL_PREFIX = "clip:"


def resolve_chunk_strategy(
    settings: Settings, space: Space | None, override: str | None = None
) -> str:
    """分片策略：本次运行覆盖 > 空间覆盖 > 全局默认；白名单外的值按未设置处理
    （防御，不中断管线）。

    override 为单次运行参数（/retry 请求带入，见 PipelineRunner.run），不落库。
    ChunkStep 与 GraphStep 共用：后者需要用它判断某资产的 graph_status=skipped
    是否仍与当前生效配置一致，而不是盲目信任写入时的旧判定。
    """
    if override in CHUNK_STRATEGIES:
        return override
    space_value = space.chunk_strategy if space else None
    if space_value in CHUNK_STRATEGIES:
        return space_value
    return settings.pipeline.chunk_strategy


def resolve_graph_extraction(
    settings: Settings, space: Space | None, override: str | None = None
) -> bool:
    """图谱抽取开关：本次运行覆盖 > 空间覆盖（on/off）> 全局 extraction_enabled。"""
    if override in GRAPH_EXTRACTION_MODES:
        return override == "on"
    space_value = space.graph_extraction if space else None
    if space_value in GRAPH_EXTRACTION_MODES:
        return space_value == "on"
    return settings.graph.extraction_enabled


def resolve_extraction_profile(
    settings: Settings,
    space: Space | None,
    profiles: Mapping[str, ExtractionProfile],
    override: str | None = None,
) -> ExtractionProfile:
    """抽取领域配置：本次运行覆盖 > 空间覆盖 > 全局 pipeline.extraction_profile。

    profiles 为可用集合（内置 + 自定义，见 db.system_config.load_extraction_profiles）；
    不在集合内的值按未设置处理（如空间引用的自定义配置已被删除）。全局值也不可用时
    落内置 general 并告警，不中断管线。
    """
    space_value = space.extraction_profile if space else None
    for value in (override, space_value, settings.pipeline.extraction_profile):
        if value and value in profiles:
            return profiles[value]
    logger.warning(
        "extraction_profile_fallback",
        override=override,
        space_value=space_value,
        global_value=settings.pipeline.extraction_profile,
    )
    return profiles[DEFAULT_EXTRACTION_PROFILE]


def chunk_cache_key(
    settings: Settings,
    checksum: str | None,
    llm_model: str | None,
    *,
    chunk_strategy: str = "llm",
    graph_extraction: bool = True,
    extraction_profile: ExtractionProfile | None = None,
) -> str:
    """幂等缓存键 = (checksum, 解析器版本, 模型, prompt 版本, 分片策略, 图谱抽取开关,
    抽取领域配置 key + 内容指纹)。

    chunk_strategy/graph_extraction/extraction_profile 为本次生效值（已按运行覆盖/空间覆盖
    解析，见 ChunkStep）：纳入缓存键是为了让切换这些设置（或改了自定义配置的内容）后重跑能
    绕过旧缓存，而不是"设置改了但命中旧分片结果不重跑"（03 文档 §二.5）。
    extraction_profile 只在 LLM 分片且抽图谱时才影响结果，其余情况传 None。
    """
    raw = "|".join(
        [
            checksum or "",
            settings.pipeline.parser_version,
            llm_model or ("mock" if settings.ai.mock else "unknown"),
            settings.pipeline.prompt_version,
            chunk_strategy,
            "graph-on" if graph_extraction else "graph-off",
            (
                f"{extraction_profile.key}@{extraction_profile.fingerprint}"
                if extraction_profile
                else "-"
            ),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def failed_extraction_batches(chunk_meta: Mapping[str, Any] | None, *, has_extraction: bool) -> int:
    """version_meta.chunk 记录的「抽图谱时失败的批数」（LLM 调用失败 / 抽取结构无效而无实体）。

    ChunkStep 把它记在 `extraction_failed_batches`；该字段出现前的旧记录只有 `fallback_batches`
    （还含 marker 失效的批次，宁可多算）——旧记录仅当确实抽过图谱（has_extraction：
    version_meta.graph.extraction 存在）才按它计，规则分片的兜底批次不算抽取失败。
    ChunkStep 的缓存命中与 scripts/reextract_graph.py 的「已完成」判定共用本函数。
    """
    chunk_meta = chunk_meta or {}
    failed = chunk_meta.get("extraction_failed_batches")
    if failed is None:
        failed = (chunk_meta.get("fallback_batches") or 0) if has_extraction else 0
    return int(failed)


def image_unit_text(asset: Asset, parse_meta: dict) -> str:
    """图片检索单元文本：caption + 关键 EXIF（供 BM25 与 mock clip 共用）。"""
    parts = [str(parse_meta.get("caption") or asset.name)]
    exif = parse_meta.get("exif") or {}
    for key in ("Make", "Model", "DateTimeOriginal", "FNumber", "FocalLength"):
        if exif.get(key):
            parts.append(f"{key}: {exif[key]}")
    return "\n".join(parts)
