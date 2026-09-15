/** 资产列表页共享字典。 */

/** 资产状态 → 徽标 tone（文案走 ui 命名空间 assetStatus.*）。 */
export const STATUS_TONE: Record<string, { tone: 'gray' | 'blue' | 'green' | 'red' }> = {
  pending: { tone: 'gray' },
  processing: { tone: 'blue' },
  ready: { tone: 'green' },
  failed: { tone: 'red' },
};

/** 筛选下拉的“不过滤”哨兵值（Radix Select 不允许空串 value）。 */
export const ALL = '__all__';

/** 类型筛选选项（与后端上传白名单一致，含图片）。 */
export const EXT_OPTIONS = [
  ['.pdf', 'PDF'],
  ['.docx', 'DOCX'],
  ['.md', 'MD'],
  ['.txt', 'TXT'],
  ['.png', 'PNG'],
] as const;

/** 聚合页拖拽/选择上传可接受的扩展名（与空间上传页一致，含图片）。 */
export const ACCEPT_TYPES = '.pdf,.docx,.pptx,.xlsx,.txt,.md,.markdown,.png,.jpg,.jpeg,.webp,.gif';
