/** 资产详情页共享字典/常量。 */

export const JOB_TYPE_LABEL: Record<string, string> = {
  parse: '解析',
  chunk: 'LLM 分片+抽取',
  graph: '图谱写入',
  embed: '嵌入',
  index: '索引',
  transcode: '懒转码',
};

export const JOB_STATUS_TONE: Record<string, 'gray' | 'blue' | 'green' | 'red'> = {
  pending: 'gray',
  running: 'blue',
  succeeded: 'green',
  failed: 'red',
};

/** 任务记录表每页条数（与旧版一致）。 */
export const JOB_PAGE_SIZE = 8;

/** 分类下拉/元数据下拉的「未设置」哨兵值（Radix Select 不允许空串 value）。 */
export const NONE = '__unset__';
