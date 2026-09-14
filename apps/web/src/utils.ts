/**
 * web 应用工具层：业务字典与共享工具再导出。
 * 格式化/错误提取/指纹计算的单源在 @loomvec/ui/lib/format。
 */
import type { BadgeTone } from '@loomvec/ui/components/status-badge';

export {
  blobToArrayBuffer,
  extractApiError,
  formatBytes,
  sha256Hex,
} from '@loomvec/ui/lib/format';

/** 配额超限原因 → 用户可读提示（登记资产 403 details.quota_reason）。 */
export const QUOTA_REASON_LABEL: Record<string, string> = {
  tenant_storage_exceeded: '租户存储配额已用尽，请联系管理员调整',
  tenant_file_count_exceeded: '租户文件数配额已用尽，请联系管理员调整',
  space_storage_exceeded: '空间存储配额已用尽，请清理文件或联系空间所有者调整配额',
  space_file_count_exceeded: '空间文件数配额已用尽，请清理文件或联系空间所有者调整配额',
};

/** 从错误体里取 details.quota_reason（无则 null）。 */
export function quotaReasonOf(err: unknown): string | null {
  if (err && typeof err === 'object') {
    const details = (err as { details?: { quota_reason?: unknown } }).details;
    if (details && typeof details.quota_reason === 'string') return details.quota_reason;
  }
  return null;
}

/** 审核状态 → 徽标。 */
export const REVIEW_STATUS_META: Record<string, { color: string; text: string }> = {
  pending_review: { color: 'warning', text: '待审核' },
  approved: { color: 'success', text: '已通过' },
  rejected: { color: 'error', text: '已驳回' },
};

/** 空间角色 → 文案。 */
export const ROLE_META: Record<string, { color: string; text: string }> = {
  owner: { color: 'gold', text: '所有者' },
  editor: { color: 'blue', text: '编辑者' },
  viewer: { color: 'default', text: '查看者' },
};

/** 空间角色 → 徽标 tone（替代各页重复的 ROLE_COLOR_TONE 映射）。 */
export const ROLE_TONE: Record<string, BadgeTone> = {
  owner: 'amber',
  editor: 'blue',
  viewer: 'gray',
};

/** 空间可选嵌入模型（创建空间 / 空间设置共用）。 */
export const EMBEDDING_MODELS = [
  { value: 'mock', label: 'mock（测试用确定性向量）' },
  { value: 'bge-m3', label: 'bge-m3' },
];

/** 空间可选分片预设（创建空间 / 空间设置共用）。 */
export const CHUNK_PRESETS = [
  { value: 'balanced', label: 'balanced（均衡）' },
  { value: 'fine', label: 'fine（细粒度分片）' },
  { value: 'long', label: 'long（长文分片）' },
];

/** 用量/配额百分比（0~100）；quota<=0 视为不限额，恒为 0。 */
export function percentOf(used: number, quota: number): number {
  if (quota <= 0) return 0;
  return Math.min(100, Math.round((used / quota) * 100));
}

/** 秒 → mm:ss（检索命中定位、媒体时间轴共用）。 */
export function formatClock(t: number): string {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
