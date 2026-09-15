/**
 * 运维端展示层共享常量：组件/队列展示名、任务状态徽章映射。
 * 单源维护，Overview / System / Pipeline 列表与详情共用。
 */
import type { BadgeTone } from '@loomvec/ui/components/status-badge';
import { t } from '@/i18n';

/** 组件名 → 展示名（七组件，docs/04 §5.1）。 */
export const COMPONENT_LABEL: Record<string, string> = {
  api: 'API',
  worker: 'Worker',
  postgres: 'PostgreSQL(+AGE)',
  milvus: 'Milvus',
  storage: 'RustFS',
  redis: 'Redis',
  mineru: 'MinerU',
};

/** 队列名 → 展示名。 */
export const QUEUE_LABEL: Record<string, string> = {
  pipeline: t('components:queue.pipeline'),
  pipeline_high: t('components:queue.pipelineHigh'),
};

/** 管线任务状态 → 徽章色调（文案在渲染处经 pipeline 命名空间 jobStatus.* 翻译）。 */
export const JOB_STATUS_TONE: Record<string, BadgeTone> = {
  pending: 'gray',
  running: 'blue',
  succeeded: 'green',
  failed: 'red',
};

/** 状态徽章兜底：未知状态原样灰底展示（文案由渲染处翻译）。 */
export function jobStatusTone(status: string): BadgeTone {
  return JOB_STATUS_TONE[status] ?? 'gray';
}
