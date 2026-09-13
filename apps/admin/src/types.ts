/**
 * 管理域（/api/v1/admin/*）响应类型：OpenAPI 中多数聚合端点为宽松 dict，
 * 这里按后端 routers 的输出契约显式声明（以 services/api/src/loomvec/api/routes/admin 为准）。
 */

/** 分页统一返回。 */
export interface PagedResp<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

/** GET /admin/system/status（P4-API-02 状态聚合）。 */
export interface SystemStatus {
  components: { name: string; ok: boolean; detail: Record<string, unknown> }[];
  pipeline: {
    queue_depths: Record<string, number>;
    dead_letter: number;
    last_24h: {
      total: number;
      succeeded: number;
      failed: number;
      running: number;
      pending: number;
      failure_rate: number;
    };
    stage_latency_seconds: Record<string, { p50: number; p95: number }>;
  };
  scale: {
    tenants: number;
    users: number;
    spaces: number;
    assets: number;
    semantic_units: number;
    storage_bytes: number;
    file_count: number;
  };
  todo: { pending_review_assets: number; failed_jobs: number; dead_letter: number };
  backup: Record<string, unknown>;
  generated_at: string;
}

/** 租户（含配额使用率）。 */
export interface TenantRow {
  id: string;
  name: string;
  plan: string;
  status: string;
  quota_storage_bytes: number;
  quota_file_count: number;
  created_at: string;
  space_count: number;
  user_count: number;
  used_storage_bytes: number;
  used_file_count: number;
}

/** 租户 OIDC 域绑定。 */
export interface OidcBinding {
  id: string;
  tenant_id: string;
  domain: string;
  issuer: string;
  client_id: string;
  has_secret: boolean;
  enabled: boolean;
  created_at: string;
}

/** 用户（平台视角）。 */
export interface UserRow {
  id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  status: string;
  auth_source: string;
  tenant_id: string | null;
  tenant_name: string | null;
  last_active_at: string | null;
  platform_roles: string[];
}

/** 空间（平台视角，含个人空间）。 */
export interface SpaceRow {
  id: string;
  slug: string;
  name: string;
  space_type: string;
  tenant_id: string | null;
  owner_id: string | null;
  owner_name: string | null;
  member_count: number;
  asset_count: number;
  storage_bytes: number;
  file_count: number;
  review_required: boolean;
  embedding_model: string | null;
  chunk_preset: string | null;
  banned: boolean;
  created_at: string;
}

/** 空间详情（含成员）。 */
export interface SpaceDetail extends SpaceRow {
  members: { user_id: string; role: string; invited_by: string | null }[];
}

/** 审核队列资产。 */
export interface ReviewAsset {
  id: string;
  name: string;
  space_id: string;
  space_slug: string | null;
  mime_type: string;
  size_bytes: number;
  status: string;
  review_status: string | null;
  review_reason: string | null;
  created_by: string | null;
  created_at: string;
}

/** 语义单元（chunk 复核）。 */
export interface UnitRow {
  id: string;
  unit_type: string;
  title: string | null;
  content: string;
  chunk_method: string;
  locator: string | null;
  char_count: number;
  order_index: number;
  embed_model_version: string | null;
}

/** 管线任务。 */
export interface JobRow {
  id: string;
  asset_id: string | null;
  asset_name: string | null;
  job_type: string;
  status: string;
  progress: number;
  attempts: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** 任务详情（同资产各步骤）。 */
export interface JobDetail extends JobRow {
  asset_steps: JobRow[];
}

/** 失败原因聚合项。 */
export interface FailureItem {
  error_prefix: string;
  count: number;
}

/** 重嵌入任务。 */
export interface ReembedTask {
  id: string;
  space_id: string | null;
  target_model: string;
  status: string;
  total_assets: number;
  done_assets: number;
  failed_assets: number;
  error: string | null;
  created_at: string;
  updated_at: string;
}

/** GET /admin/reindex/status。 */
export interface ReindexStatus {
  spaces: {
    space_id: string;
    units: number;
    embedded_units: number;
    pending_units: number;
    model_versions: { model_version: string | null; units: number }[];
  }[];
}

/** 配置项（GET /admin/settings 列表项）。 */
export interface SettingItem {
  key: string;
  group: string;
  description: string;
  value: unknown;
  sensitive: boolean;
  effect: string; // immediate | restart
  admin_only: boolean;
  overridden: boolean;
}

/** OAuth 应用。 */
export interface OauthClient {
  id: string;
  name: string;
  client_id: string;
  redirect_uris: string[];
  scopes: string[];
  description: string | null;
  homepage_url: string | null;
  status: string;
  created_at: string;
  /** 创建/重置响应额外携带的一次性明文 secret */
  client_secret?: string;
}

/** Webhook 订阅。 */
export interface Subscription {
  id: string;
  name: string;
  url: string;
  event_types: string[];
  description: string | null;
  paused: boolean;
  created_at: string;
  /** 创建响应额外携带的一次性明文 secret */
  secret?: string;
}

/** Webhook 投递记录。 */
export interface Delivery {
  id: string;
  subscription_id: string;
  event_type: string;
  status: string;
  attempt: number;
  response_status: number | null;
  error: string | null;
  next_retry_at: string | null;
  delivered_at: string | null;
  created_at: string;
  payload: Record<string, unknown> | null;
}

/** 审计日志行。 */
export interface AuditLogRow {
  id: string;
  tenant_id: string | null;
  actor_user_id: string | null;
  action: string;
  object_type: string;
  object_id: string | null;
  reason: string | null;
  before_value: Record<string, unknown> | null;
  after_value: Record<string, unknown> | null;
  request_id: string | null;
  ip: string | null;
  created_at: string;
}

/** API Key。 */
export interface ApiKeyRow {
  id: string;
  name: string;
  scopes: string[];
  rate_limit_per_min: number;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  /** 创建响应额外携带的一次性明文 key */
  key?: string;
}

/** 可订阅的 webhook 事件类型（与 core/constants.WEBHOOK_EVENT_TYPES 对齐）。 */
export const WEBHOOK_EVENT_TYPES = [
  'job.progress',
  'asset.ready',
  'asset.failed',
  'member.added',
  'member.removed',
  'member.role_changed',
  'review.pending',
  'review.approved',
  'review.rejected',
] as const;

/** 嵌入模型白名单（与 core/constants.EMBEDDING_MODELS 对齐）。 */
export const EMBEDDING_MODEL_OPTIONS = ['text-embedding-v4', 'multimodal-embedding-v1', 'mock'];
