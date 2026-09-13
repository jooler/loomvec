/**
 * web 应用工具层：业务字典与共享工具再导出。
 * 格式化/错误提取/指纹计算的单源在 @loomvec/ui/lib/format。
 */

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

/** 空间角色 → 徽标。 */
export const ROLE_META: Record<string, { color: string; text: string }> = {
  owner: { color: 'gold', text: '所有者' },
  editor: { color: 'blue', text: '编辑者' },
  viewer: { color: 'default', text: '查看者' },
};
