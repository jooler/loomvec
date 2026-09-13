"""P1 管线包：mime 嗅探 / LLM 分片 / 页码定位 / 步骤与编排。

模块划分：
- mime.py      MIME 嗅探与白名单
- chunking.py  marker 契约、校验/钳制、结构化兜底（纯逻辑）
- pages.py     行→页映射与文本清洗（定位回溯基础）
- steps.py     步骤实现（parse/chunk/embed/index）+ PipelineDeps
- runner.py    编排器（进度事件、失败记录、单步重跑）
"""

from loomvec.core.pipeline.chunking import (
    ChunkDraft,
    TableRange,
    build_chunk_messages,
    detect_table_ranges,
    parse_markers,
    split_batches,
    structural_fallback,
    validate_markers,
)
from loomvec.core.pipeline.mime import file_ext, is_mineru_mime, sniff_mime
from loomvec.core.pipeline.pages import build_line_page_map, sanitize_text, unit_locator
from loomvec.core.pipeline.runner import PipelineRunner
from loomvec.core.pipeline.steps import (
    LAST_STEP,
    PIPELINE_STEPS,
    ChunkStep,
    EmbedStep,
    IndexStep,
    ParseStep,
    PipelineDeps,
    chunk_cache_key,
)

__all__ = [
    "LAST_STEP",
    "PIPELINE_STEPS",
    "ChunkDraft",
    "ChunkStep",
    "EmbedStep",
    "IndexStep",
    "ParseStep",
    "PipelineDeps",
    "PipelineRunner",
    "TableRange",
    "build_chunk_messages",
    "build_line_page_map",
    "chunk_cache_key",
    "detect_table_ranges",
    "file_ext",
    "is_mineru_mime",
    "parse_markers",
    "sanitize_text",
    "sniff_mime",
    "split_batches",
    "structural_fallback",
    "unit_locator",
    "validate_markers",
]
