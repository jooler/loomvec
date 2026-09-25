/**
 * web 应用工具层：业务字典与共享工具再导出。
 * 格式化/错误提取/指纹计算的单源在 @loomvec/ui/lib/format；
 * 配额超限提示（quotaReasonLabel/quotaReasonOf）单源在 @loomvec/ui/lib/upload。
 * 带文案的字典（角色/审核状态等）改为渲染处经 i18n 取值，此处仅保留 tone 映射。
 */
import type { BadgeTone } from '@loomvec/ui/components/status-badge';

export {
  blobToArrayBuffer,
  extractApiError,
  formatBytes,
  formatDateTime,
  sha256Hex,
} from '@loomvec/ui/lib/format';

export { quotaReasonLabel, quotaReasonOf, UPLOAD_SKIPPED } from '@loomvec/ui/lib/upload';

/** 空间角色 → 徽标 tone（替代各页重复的 ROLE_COLOR_TONE 映射）；文案走 i18n role.*。 */
export const ROLE_TONE: Record<string, BadgeTone> = {
  owner: 'amber',
  editor: 'blue',
  viewer: 'gray',
};

/** 空间可选嵌入模型（创建空间 / 空间设置共用）；文案走 i18n embeddingModel.*。 */
export const EMBEDDING_MODELS = ['mock', 'bge-m3'] as const;

/** 空间可选分片预设（创建空间 / 空间设置共用）；文案走 i18n chunkPreset.*。 */
export const CHUNK_PRESETS = ['balanced', 'fine', 'long'] as const;

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
