"""P0-CORE-01 分级配置体系（pydantic-settings）。

配置分三层，**来源唯一**（同一参数只在一处定义，不做跨层兜底）：

- **环境变量 / `.env`（基础设施与运行环境）**：数据库/Redis/Milvus/对象存储连接、
  运行环境（ENV）、日志、认证密钥、限流、worker 运行参数、OTel、**服务端口**；
  统一前缀 `LOOMVEC_`，嵌套字段用 `__` 分隔（如 `LOOMVEC_POSTGRES__URL`）；
- **`config/loomvec.json`（应用运行参数，应用自管理）**：AI 供方（地址/密钥/模型）、
  MinerU、检索、管线分片、上传白名单、图片处理、图谱、媒体、转写、问答；
  路径经 `LOOMVEC_APP_CONFIG` 指定（缺省 `<仓库根>/config/loomvec.json`），
  文件不存在时使用内置默认值；模板见 `config/loomvec.example.json`；
- **DB `system_config`（admin 动态配置，显式管理覆盖）**：经管理端集中配置的
  运营可调项（检索/上传/图谱开关/AI 供方/SSO），DB 有值即生效，
  未配置回落 AppConfig 默认——回落目标是文件默认而非环境变量。

各服务（api/worker）从各自的入口加载同一份 `Settings` + `AppConfig`。
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> str | None:
    """从 cwd 向上查找最近的 .env，止步于仓库根（.git 所在目录）。

    配置加载原本只认 cwd 下的 .env，子目录命令（如 `make migrate` 在
    services/api 执行）会静默回退到内置默认值，可能指向与 API 不同的
    数据库；环境变量优先级始终高于 .env，部署注入不受影响。
    """
    root = _find_repo_root()
    if root is None:
        return None
    env = root / ".env"
    return str(env) if env.is_file() else None


def _find_repo_root() -> Path | None:
    """从 cwd 向上定位仓库根（.git 所在目录）；找不到返回 None。"""
    directory = Path.cwd().resolve()
    while True:
        if (directory / ".git").exists():
            return directory
        if directory.parent == directory:
            return None
        directory = directory.parent


def _resolve_app_config_path() -> Path:
    """应用参数文件路径：LOOMVEC_APP_CONFIG 优先，缺省仓库根 config/loomvec.json。"""
    override = os.environ.get("LOOMVEC_APP_CONFIG")
    if override:
        return Path(override)
    root = _find_repo_root()
    return (root / "config" / "loomvec.json") if root else Path("config/loomvec.json")


class Env(StrEnum):
    DEV = "dev"
    TEST = "test"
    STAGING = "staging"
    PROD = "prod"


class OtelSettings(BaseModel):
    """OpenTelemetry：未配置 endpoint 时不导出（本地开发默认关闭）。"""

    endpoint: str | None = None  # OTLP gRPC endpoint，如 http://localhost:4317
    sample_ratio: float = 1.0


class PostgresSettings(BaseModel):
    # 默认对齐 compose 宿主机端口（deploy/compose/.env 的 POSTGRES_PORT）；
    # 系统自带 PostgreSQL 占 5432，指向它会静默读写错误的库
    url: str = "postgresql+asyncpg://loomvec:loomvec@localhost:5433/loomvec"
    pool_size: int = 10
    max_overflow: int = 20
    echo: bool = False


class RedisSettings(BaseModel):
    url: str = "redis://localhost:6379/0"


class MilvusSettings(BaseModel):
    uri: str = "http://localhost:19530"
    token: str | None = None


class StorageSettings(BaseModel):
    """RustFS / 任意 S3 兼容对象存储。"""

    endpoint: str = "http://localhost:9000"
    access_key: str = "loomvec"
    secret_key: str = "loomvec-secret"
    region: str = "us-east-1"
    secure: bool = False
    # bucket 约定：原始文件 / 派生物（缩略图、关键帧、解析产物等）
    bucket_raw: str = "loomvec-raw"
    bucket_derived: str = "loomvec-derived"
    # 浏览器直传预签名 URL 依赖桶级 CORS（PUT/OPTIONS）。仅在 dev 启动时由
    # ensure_buckets 自动写入（幂等）；生产桶由运维手工配置 CORS（收紧为前端
    # 实际来源，如 ["https://web.example.com"]），本配置不生效。
    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["*"])


class MineruSettings(BaseModel):
    """MinerU 解析服务（mineru-api，HTTP）。"""

    base_url: str = "http://localhost:8000"
    # 大 PDF 首次/冷启动解析可能超过 10 分钟（模型加载 + 版面推理）
    timeout_seconds: float = 1800.0
    backend: Literal["pipeline", "vlm-transformers", "vlm-vllm-engine"] = "pipeline"


class AiProviderConfig(BaseModel):
    """单个 AI 供方端点（OpenAI 兼容）。密钥只经环境变量注入。

    api_style="dashscope" 时走阿里云百炼 DashScope 原生协议：
    - rerank → POST {base_url}/services/rerank/text-rerank/text-rerank
      （base_url 填 https://dashscope.aliyuncs.com/api/v1；
        rerank 无 OpenAI 兼容端点）；
    - clip → POST {base_url}/services/embeddings/multimodal-embedding/multimodal-embedding
    """

    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float = 120.0
    api_style: Literal["openai", "dashscope"] = "openai"


class EmbeddingSettings(AiProviderConfig):
    dim: int = 1024  # 嵌入维度（Milvus 集合按此建 dim；更换模型/维度需重建集合）
    # 供方自定义维度（如 text-embedding-v4 支持 64~2048，经请求体 dimensions 透传；
    # None = 不传参，用模型默认维度；改动需与 dim 一致并重建集合）
    dimensions: int | None = None
    batch_size: int = 16  # 单次请求批量（压测项，见 06 文档风险提示）


class ClipSettings(AiProviderConfig):
    """图文向量通道（P2-CORE-05，BGE-VL / 百炼 multimodal-embedding 类）。

    - api_style="openai"（默认）：OpenAI 兼容 `/embeddings`，input 为混合列表
      `[{"text": "..."} | {"image": "<base64>"}]`（Jina 风格）；
    - api_style="dashscope"：百炼原生 multimodal-embedding 接口；
    文本查询与图片走同一向量空间（以文搜图）。
    """

    dim: int = 1024  # clip_dense 通道维度（Milvus 第二向量字段）


class LlmSettings(AiProviderConfig):
    temperature: float = 0.2
    json_retries: int = 2  # JSON 输出解析失败重试次数


class MockSettings(BaseModel):
    """mock 供方行为参数（仅 ai.mock=true 时生效，用于本地开发与 CI）。"""

    chunk_window_lines: int = 12  # mock 分片粒度：约 N 行/片


class AiSettings(BaseModel):
    """AI 供方网关配置：向量化 / rerank / VLM / LLM 四通道。

    mock=true 时全部通道走确定性本地实现（无需外网/密钥），
    用于无云凭据的本地开发与 CI；生产禁止开启。
    """

    mock: bool = False
    mock_impl: MockSettings = Field(default_factory=MockSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    rerank: AiProviderConfig = Field(default_factory=AiProviderConfig)
    vlm: AiProviderConfig = Field(default_factory=AiProviderConfig)
    clip: ClipSettings = Field(default_factory=ClipSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)


class SearchSettings(BaseModel):
    """P1-CORE-03 混合检索参数。"""

    default_top_k: int = 10
    max_top_k: int = 50
    dense_top_k: int = 50  # dense 召回候选数
    sparse_top_k: int = 50  # BM25 召回候选数
    rrf_k: int = 60  # RRF 平滑常数
    rerank_enabled: bool = True
    rerank_candidates: int = 30  # 送精排的候选上限
    image_search_enabled: bool = True  # clip_dense 以文搜图召回路（clip 通道未配置时自动降级）
    # P3-CORE-04 图谱召回（L3/L4）：默认开启，可用请求参数 use_graph 按次关闭
    graph_enabled: bool = True
    # 标签/分类过滤的 PG 侧 asset 预扫描上限（收敛为 asset_ids 注入 Milvus expr）
    filter_scan_limit: int = 10000


class PipelineSettings(BaseModel):
    """P1 管线参数：分片钳制、LLM 分批、嵌入批处理、截断上限、可选摘要。"""

    parser_version: str = "mineru-3.4.5"
    # P3 起分片与图谱抽取合并为单次 LLM 调用（03 文档 §3.2），prompt 版本升级；
    # 缓存键含此版本 → 升级后旧资产重跑会重新计费抽取
    prompt_version: str = "chunk-graph-v2"
    chunk_min_chars: int = 120
    chunk_max_chars: int = 2000
    llm_batch_chars: int = 6000  # 单次 LLM 分片调用的最大文档字符量
    embed_batch_size: int = 16
    embed_text_max_chars: int = 6000  # 嵌入输入截断（云端模型 token 上限内保守值）
    index_text_max_chars: int = 30000  # Milvus 索引正文截断（VARCHAR 上限 65535）
    summary_enabled: bool = False  # 资产摘要生成（可选，默认关）


class WorkerSettings(BaseModel):
    """P1-WRK-01 Celery 工程参数（celery_app.py 读取）。"""

    heartbeat_interval_seconds: float = 30.0
    task_soft_time_limit: int = 3500
    task_time_limit: int = 3600
    # Prometheus 指标端口（prometheus.yml 的 loomvec-worker 抓取目标）；0 = 关闭
    metrics_port: int = 9808


class UploadSettings(BaseModel):
    """P1-API-01 上传白名单与限额（MIME/大小）。"""

    max_size_bytes: int = 200 * 1024 * 1024
    # 允许的扩展名（MIME 嗅探 + 白名单双重校验）；P3 起纳入音视频
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
            ".txt",
            ".md",
            ".markdown",
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
            ".bmp",
            ".mp4",
            ".mov",
            ".webm",
            ".mkv",
            ".mp3",
            ".wav",
            ".m4a",
            ".flac",
        ]
    )
    presign_expires_seconds: int = 3600


class ImageSettings(BaseModel):
    """P2-CORE-05 图片处理：缩略图 / EXIF / 图文向量 / VLM caption。"""

    thumbnail_max_width: int = 480
    thumbnail_max_height: int = 480
    thumbnail_format: str = "webp"
    thumbnail_quality: int = 82
    # VLM caption 开关（开启要求 ai.vlm 配置或 mock 模式）
    caption_enabled: bool = True
    # EXIF 提取白名单键（exiftool 输出过滤，避免存入设备隐私全量字段）
    exif_whitelist: list[str] = Field(
        default_factory=lambda: [
            "Make",
            "Model",
            "LensModel",
            "DateTimeOriginal",
            "ExposureTime",
            "FNumber",
            "ISO",
            "FocalLength",
            "Orientation",
            "GPSLatitude",
            "GPSLongitude",
            "ImageWidth",
            "ImageHeight",
        ]
    )


class GraphSettings(BaseModel):
    """P3-CORE 图谱管线：抽取、链接、合并、社区、召回参数。

    enabled 为运行时总开关（检索/写入联动降级为 L1+L2）；
    运维端可通过 SystemConfig(key="graph.enabled") 运行时全局关停（P3-API-02）。
    """

    enabled: bool = True  # 图谱总开关（写入与 L3/L4 召回联动；关闭即纯 L1/L2）
    extraction_enabled: bool = True  # 合并抽取开关（关闭后新资产不再产出实体，存量保留）
    entity_link_threshold: float = 0.90  # 阶段一内联链接：Milvus entities top-1 相似度阈值
    merge_threshold: float = 0.94  # 阶段二空间合并：向量近邻判定阈值（高于链接阈值）
    merge_name_distance: int = 2  # 阶段二规范名编辑距离候选阈值（归一化后）
    merge_min_candidates: int = 1  # 空间级合并任务触发：发现候选 ≥ N 才执行
    merge_trigger_delta: int = 20  # 距上次合并新增实体达该数量即触发合并
    expand_hops: int = 2  # L3 召回：AGE 扩展跳数（1~2）
    link_top_k: int = 4  # 查询侧实体链接：entities 向量 top-k
    graph_top_k: int = 30  # L3 召回：图谱路径回收 chunk 候选上限
    adjacency_enabled: bool = True  # 图谱邻接补全（命中 chunk 的 1 跳邻居并入候选池）
    adjacency_top_k: int = 8  # 邻接补全：取融合前 top-N 命中做种子
    max_entities_per_asset: int = 300  # 单资产抽取实体上限（防 prompt 失控）
    max_relations_per_asset: int = 600  # 单资产抽取关系上限
    merge_interval_seconds: float = 1800.0  # 空间合并周期任务扫描间隔（beat）
    community_interval_seconds: float = 3600.0  # 社区检测周期任务扫描间隔（beat）


class MediaSettings(BaseModel):
    """P3-WRK-03/05 视频/音频管线与懒转码（ffmpeg/ffprobe 走系统二进制）。"""

    scene_threshold: float = 27.0  # PySceneDetect ContentDetector 阈值
    min_scene_seconds: float = 3.0  # 场景最短时长（过短场景并入前一场景）
    max_scenes_per_asset: int = 200  # 场景数上限（超出截断，超长视频降采样）
    keyframe_width: int = 640  # 关键帧缩放宽度（0 = 原宽）
    keyframe_format: str = "jpg"  # 关键帧编码（jpg 兼容性好、体积小）
    # 懒转码：预览转码目标（H.264/AAC，web 播放兼容）；触发格式为不可直播的容器
    transcode_enabled: bool = True
    transcode_height: int = 720  # 预览转码最大高度（等比缩放）
    transcode_crf: int = 23  # x264 恒定质量因子
    transcode_preset: str = "veryfast"
    native_video_mimes: list[str] = Field(
        default_factory=lambda: ["video/mp4", "video/webm"]  # 浏览器可直放容器
    )
    probe_timeout_seconds: float = 60.0
    transcode_timeout_seconds: float = 3600.0


class TranscribeSettings(BaseModel):
    """P3-WRK-04 faster-whisper 转写（懒加载，未安装/未配置时降级为无转写）。"""

    enabled: bool = True
    model_size: str = "base"  # tiny/base/small/medium/large-v3
    device: str = "cpu"  # cpu / cuda
    compute_type: str = "int8"  # int8 / float16 / auto
    language: str | None = None  # None = 自动检测
    segment_max_chars: int = 400  # 转写分块目标字符数（按时间戳归组）
    segment_max_seconds: float = 60.0  # 转写分块最大时长


class QaSettings(BaseModel):
    """P3-CORE-05 问答服务参数。"""

    enabled: bool = True
    retrieval_top_k: int = 8  # 送入 prompt 的引用上限
    history_messages: int = 6  # 携带的历史消息条数（user/assistant 合计）
    max_question_chars: int = 2000
    max_answer_tokens: int = 1024
    session_max_messages: int = 200  # 单会话消息上限（超出拒绝续聊，提示新建）


class AuthSettings(BaseModel):
    """P0 用 dev 模式签发测试 JWT；P4-API-06 起接入 OIDC（租户域绑定）。"""

    dev_mode: bool = True
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 12 * 3600


class SecuritySettings(BaseModel):
    """P4-INF-04 安全加固：全局限流（fail-open）、密钥管理约定见 api/ratelimit.py。"""

    rate_limit_enabled: bool = True
    rate_limit_per_min: int = 300
    # 是否拒绝无 Token 的匿名写流量（/healthz、/metrics、/readyz 除外）
    anonymous_write_allowed: bool = False


class AppConfig(BaseModel):
    """应用运行参数（config/loomvec.json，应用自管理）。

    与环境变量解耦：AI 供方（地址/密钥/模型）、MinerU、检索、管线分片、
    上传白名单、图片处理、图谱、媒体、转写、问答。文件支持部分覆盖
    （未写的键取内置默认）；admin 动态配置（DB）未覆盖时以此为生效值。
    """

    ai: AiSettings = Field(default_factory=AiSettings)
    mineru: MineruSettings = Field(default_factory=MineruSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    image: ImageSettings = Field(default_factory=ImageSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    transcribe: TranscribeSettings = Field(default_factory=TranscribeSettings)
    qa: QaSettings = Field(default_factory=QaSettings)


class Settings(BaseSettings):
    """基础设施与运行环境（env / .env 单源；不含应用运行参数）。

    应用参数段（ai/mineru/search/...）以只读 property 委托 AppConfig，
    仅为既有消费方提供 `settings.ai` 形态的访问；配置来源仍是
    config/loomvec.json，环境变量无法覆盖（来源唯一）。
    """

    model_config = SettingsConfigDict(
        env_prefix="LOOMVEC_",
        env_nested_delimiter="__",
        env_file=_find_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Env = Env.DEV
    service_name: str = "loomvec"
    log_level: str = "INFO"
    # JSON 结构化日志；dev 控制台可切 human-readable
    log_json: bool = True
    # P1 单空间运行：默认空间 slug（seed 于迁移 0002，P2 展开空间语义）
    default_space_slug: str = "default"
    # 应用参数文件路径（仅路径属运行环境；文件内容见 AppConfig）
    app_config_path: str = ""

    # ---- 服务端口（部署时经 env 灵活调整） ----
    api_port: int = 8080
    web_port: int = 5173
    admin_port: int = 5174
    ops_port: int = 5175

    otel: OtelSettings = Field(default_factory=OtelSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    milvus: MilvusSettings = Field(default_factory=MilvusSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)

    # ---- 应用参数段：委托 AppConfig（config/loomvec.json），env 不可覆盖 ----

    @property
    def ai(self) -> AiSettings:
        return get_app_config().ai

    @property
    def mineru(self) -> MineruSettings:
        return get_app_config().mineru

    @property
    def search(self) -> SearchSettings:
        return get_app_config().search

    @property
    def pipeline(self) -> PipelineSettings:
        return get_app_config().pipeline

    @property
    def upload(self) -> UploadSettings:
        return get_app_config().upload

    @property
    def image(self) -> ImageSettings:
        return get_app_config().image

    @property
    def graph(self) -> GraphSettings:
        return get_app_config().graph

    @property
    def media(self) -> MediaSettings:
        return get_app_config().media

    @property
    def transcribe(self) -> TranscribeSettings:
        return get_app_config().transcribe

    @property
    def qa(self) -> QaSettings:
        return get_app_config().qa


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例入口；测试中可用 get_settings.cache_clear() 重置。"""
    return Settings()


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    """应用参数单例：加载 LOOMVEC_APP_CONFIG（缺省 config/loomvec.json）。

    文件不存在时使用内置默认值（等价 config/loomvec.example.json）；
    JSON 支持部分覆盖（未写的键取默认）。测试可用 get_app_config.cache_clear()。
    """
    path = Path(os.environ.get("LOOMVEC_APP_CONFIG") or _resolve_app_config_path())
    if not path.is_file():
        return AppConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    return AppConfig.model_validate(data)
