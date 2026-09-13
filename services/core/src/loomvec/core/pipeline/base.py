"""管线共享定义：步骤表、运行依赖、幂等缓存键、图片单元文本。

拆分说明（P2 重构）：steps.py 保留四步的文档流实现，图片分支在 image_flow.py；
本模块承载两者共用的常量与数据结构，避免相互导入成环。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loomvec.core.db.models import Asset
from loomvec.core.mineru_client import MineruClient
from loomvec.core.retrieval import MilvusStore
from loomvec.core.storage import ObjectStorage

if TYPE_CHECKING:
    from loomvec.core.ai import AiGateway
    from loomvec.core.config import Settings

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


def chunk_cache_key(settings: Settings, checksum: str | None, llm_model: str | None) -> str:
    """幂等缓存键 = (checksum, 解析器版本, 模型, prompt 版本)——03 文档 §二.5。"""
    raw = "|".join(
        [
            checksum or "",
            settings.pipeline.parser_version,
            llm_model or ("mock" if settings.ai.mock else "unknown"),
            settings.pipeline.prompt_version,
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def image_unit_text(asset: Asset, parse_meta: dict) -> str:
    """图片检索单元文本：caption + 关键 EXIF（供 BM25 与 mock clip 共用）。"""
    parts = [str(parse_meta.get("caption") or asset.name)]
    exif = parse_meta.get("exif") or {}
    for key in ("Make", "Model", "DateTimeOriginal", "FNumber", "FocalLength"):
        if exif.get(key):
            parts.append(f"{key}: {exif[key]}")
    return "\n".join(parts)
