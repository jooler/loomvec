/**
 * 运维端展示层共享常量：组件/队列展示名、任务状态徽章映射。
 * 单源维护，Overview / System / Pipeline 列表与详情共用。
 */
import type { BadgeTone } from '@loomvec/ui/components/status-badge';

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
  pipeline: '管线队列',
  pipeline_high: '高优队列',
};

export interface StatusMeta {
  tone: BadgeTone;
  text: string;
}

/** 管线任务状态 → 徽章（PipelineList / PipelineDetail 共用）。 */
export const JOB_STATUS_META: Record<string, StatusMeta> = {
  pending: { tone: 'gray', text: '排队' },
  running: { tone: 'blue', text: '进行中' },
  succeeded: { tone: 'green', text: '成功' },
  failed: { tone: 'red', text: '失败' },
};

/** 状态徽章兜底：未知状态原样灰底展示。 */
export function jobStatusMeta(status: string): StatusMeta {
  return JOB_STATUS_META[status] ?? { tone: 'gray', text: status };
}
