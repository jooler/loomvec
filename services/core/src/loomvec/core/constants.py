"""基础设施标识单源化：队列、Redis 键、事件类型、scope。

约定：跨服务（api/worker）共享的字符串标识一律在此定义，禁止散落字面量——
P4 的 SSE/Webhook、运营端观测都挂钩在这些键与事件类型上，改名即破坏契约。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Celery 队列拓扑（P1-WRK-01；Makefile worker 命令需与此同步）
# ---------------------------------------------------------------------------

QUEUE_PIPELINE = "pipeline"  # 默认管线队列（parse/chunk/graph/embed/index 整链）
QUEUE_PIPELINE_HIGH = "pipeline_high"  # 交互优先队列（单步重跑等用户等待场景）
QUEUE_PIPELINE_LOW = "pipeline_low"  # 低优先级队列（实体合并/社区检测等周期任务，P3）

# AGE 图名（deploy/compose/postgres-age/init 01-age-init.sql 首建；本常量与迁移 0005 兜底重建）
GRAPH_NAME = "loomvec_graph"

# 懒转码进度 Redis 键（media.transcode 任务写，播放接口读；{asset_id} 占位）
TRANSCODE_PROGRESS_KEY = "loomvec:transcode:progress:{asset_id}"

# ---------------------------------------------------------------------------
# Redis 键（P1-WRK-01 心跳 / 死信；P4 运营端观测复用）
# ---------------------------------------------------------------------------

HEARTBEAT_KEY = "loomvec:worker:heartbeat"
HEARTBEAT_TTL_SECONDS = 120
DEAD_LETTER_KEY = "loomvec:jobs:dead"
DEAD_LETTER_MAX_LEN = 1000

# 固定窗口限流（API Key，P1-API-04）
RATE_WINDOW_SECONDS = 60
# 限流计数键（identity.identity_from_api_key 拼接 {prefix}:{key_id}:{window}）
API_KEY_RATE_LIMIT_KEY_PREFIX = "loomvec:rl:apikey"

# ---------------------------------------------------------------------------
# 事件通道与类型（P1-CORE-04 进度事件；P4 SSE/Webhook 挂此扩展）
# ---------------------------------------------------------------------------

EVENTS_CHANNEL = "loomvec:events"

EVENT_JOB_PROGRESS = "job.progress"
EVENT_ASSET_READY = "asset.ready"
EVENT_ASSET_FAILED = "asset.failed"

# P2 事件（成员/审核；通知中心与用户端订阅消费）
EVENT_MEMBER_ADDED = "member.added"
EVENT_MEMBER_REMOVED = "member.removed"
EVENT_MEMBER_ROLE_CHANGED = "member.role_changed"
EVENT_REVIEW_PENDING = "review.pending"
EVENT_REVIEW_APPROVED = "review.approved"
EVENT_REVIEW_REJECTED = "review.rejected"

# P3 事件（图谱管线 / 懒转码进度）
EVENT_GRAPH_READY = "graph.ready"  # 资产图谱写入完成
EVENT_GRAPH_PENDING_RETRY = "graph.pending_retry"  # 抽取降级，待重试
EVENT_TRANSCODE_PROGRESS = "transcode.progress"  # 懒转码进度（播放接口/前端订阅）

# 通知类型（notifications.type；P2-API-06 通知接口复用）
NOTIFY_MEMBER_ADDED = "member.added"
NOTIFY_MEMBER_REMOVED = "member.removed"
NOTIFY_MEMBER_ROLE_CHANGED = "member.role_changed"
NOTIFY_REVIEW_PENDING = "review.pending"
NOTIFY_REVIEW_APPROVED = "review.approved"
NOTIFY_REVIEW_REJECTED = "review.rejected"
NOTIFY_ASSET_FAILED = "asset.failed"

# ---------------------------------------------------------------------------
# Webhook（P4-API-08）：可订阅事件目录、投递常量、Redis 键
# ---------------------------------------------------------------------------

# 可订阅事件白名单（订阅时校验；"asset.ready/failed、review.*、member.*" 起步，
# 后续事件类型加入此目录即自动可订阅）
WEBHOOK_EVENT_TYPES: tuple[str, ...] = (
    EVENT_JOB_PROGRESS,
    EVENT_ASSET_READY,
    EVENT_ASSET_FAILED,
    EVENT_MEMBER_ADDED,
    EVENT_MEMBER_REMOVED,
    EVENT_MEMBER_ROLE_CHANGED,
    EVENT_REVIEW_PENDING,
    EVENT_REVIEW_APPROVED,
    EVENT_REVIEW_REJECTED,
)
# 事件持久化日志（LPUSH，供 webhook 派发器轮询；pub/sub 广播不落盘会丢）
EVENTS_LOG_KEY = "loomvec:events:log"
EVENTS_LOG_MAX_LEN = 10000
# 派发游标（派发器读事件日志的位置）
WEBHOOK_CURSOR_KEY = "loomvec:webhook:cursor"
# 投递重试梯度（秒）：失败后按梯度排期，走完全部梯度置 dead
WEBHOOK_RETRY_SCHEDULE_SECONDS: tuple[int, ...] = (30, 120, 600, 3600, 6 * 3600, 24 * 3600)
WEBHOOK_TIMEOUT_SECONDS = 10.0
WEBHOOK_SIGNATURE_HEADER = "X-LoomVec-Signature"  # HMAC-SHA256(secret, body) hex
WEBHOOK_EVENT_HEADER = "X-LoomVec-Event"

# 重嵌入任务（P4-API-04）：管线级常量
REEMBED_QUEUE = QUEUE_PIPELINE  # 重嵌入与普通管线同队列（批量任务限速执行）

# ---------------------------------------------------------------------------
# P2 空间协作：角色序、分片预设、嵌入模型白名单
# ---------------------------------------------------------------------------

# 空间角色（序数比较：owner > editor > viewer）
SPACE_ROLES: tuple[str, ...] = ("viewer", "editor", "owner")

# 分片预设（创建空间时白名单内选一；映射到 ChunkStep 的钳制参数）
CHUNK_PRESETS: dict[str, dict[str, int]] = {
    "balanced": {"chunk_min_chars": 120, "chunk_max_chars": 2000},
    "fine": {"chunk_min_chars": 60, "chunk_max_chars": 800},
    "long": {"chunk_min_chars": 300, "chunk_max_chars": 4000},
}
DEFAULT_CHUNK_PRESET = "balanced"

# 空间可选嵌入模型白名单（创建/设置空间时校验；P2 仅记录选择，实际路由随网关配置）
EMBEDDING_MODELS: tuple[str, ...] = (
    "text-embedding-v4",
    "multimodal-embedding-v1",
    "mock",
)

# ---------------------------------------------------------------------------
# P3 图谱与问答
# ---------------------------------------------------------------------------

# 实体类型白名单（抽取 prompt 与校验器共用；未知类型归并为 other）
ENTITY_TYPES: tuple[str, ...] = (
    "person",
    "organization",
    "product",
    "location",
    "event",
    "concept",
    "other",
)
DEFAULT_ENTITY_TYPE = "other"

# 实体合并发现途径（merge_log.reason）
MERGE_REASON_VECTOR = "auto_vector"  # 阶段二向量近邻候选
MERGE_REASON_NAME = "auto_name"  # 阶段二规范名归并候选
MERGE_REASON_MANUAL = "manual"  # 人工触发

# merge_log 状态
MERGE_STATUS_APPLIED = "applied"
MERGE_STATUS_ROLLBACK = "rolled_back"

# ---------------------------------------------------------------------------
# API Key scope（P1-API-04：本阶段仅读写两类；P2 随空间权限展开）
# P4-API-01：管理域独立 scopes（admin:*），仅平台角色可授予
# ---------------------------------------------------------------------------

SCOPE_READ = "read"  # search / 列表 / 详情 / 预览
SCOPE_WRITE = "write"  # 上传 / 登记 / 删除 / 重试
SCOPE_ADMIN_READ = "admin:read"  # 运维端只读域（状态/列表/审计查看）
SCOPE_ADMIN_WRITE = "admin:write"  # 运维端治理写操作
ALL_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN_READ, SCOPE_ADMIN_WRITE)

# ---------------------------------------------------------------------------
# 平台角色（P4-API-01）：全局行，见 Role.is_platform_role；迁移 0004 种子
# super_admin：全部（含系统配置、平台角色分配）；operator：日常运营（无系统配置）；
# auditor：全局只读 + 审计导出
# ---------------------------------------------------------------------------

PLATFORM_ROLE_SUPER_ADMIN = "super_admin"
PLATFORM_ROLE_OPERATOR = "operator"
PLATFORM_ROLE_AUDITOR = "auditor"
PLATFORM_ROLES: tuple[str, ...] = (
    PLATFORM_ROLE_SUPER_ADMIN,
    PLATFORM_ROLE_OPERATOR,
    PLATFORM_ROLE_AUDITOR,
)
# operator 禁改的配置组（docs/04 §5.9：系统配置仅 super_admin）
SUPER_ADMIN_ONLY_SETTINGS_GROUPS: tuple[str, ...] = ("ai", "sso", "extensions")

# ---------------------------------------------------------------------------
# 默认空间（迁移 0002 seed 的固定 UUID；slug 见 Settings.default_space_slug）
# 运行时应经 slug 查库解析（见 api/deps.get_space_id），此常量仅作查库失败兜底
# 与 Alembic 迁移 0002 的 seed 值保持一致。
# ---------------------------------------------------------------------------

FALLBACK_DEFAULT_SPACE_ID = "0198bec0-0000-7000-8000-000000000001"

# P2 迁移 0003 种子：默认租户与演示用户（alice=owner / bob=editor / carol=viewer）。
# 与迁移脚本中的 seed 值保持一致；dev 登录的未知用户也会挂到该租户（dev 模式）。
SEED_TENANT_ID = "0198bec0-0000-7000-8000-0000000000a0"
SEED_USERS: dict[str, str] = {
    "alice": "0198bec0-0000-7000-8000-000000000101",
    "bob": "0198bec0-0000-7000-8000-000000000102",
    "carol": "0198bec0-0000-7000-8000-000000000103",
}
