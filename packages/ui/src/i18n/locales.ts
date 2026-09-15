/**
 * 界面语言注册表（单源）。
 * 新增语言三步：locales/<code>/ 下补齐全部命名空间 JSON → 在此注册 → 各 app 的
 * src/i18n/locales 里按语言目录组织资源即可，组件代码零改动。
 * label 用语言自称（语言选择器惯例，不随界面语言翻译）。
 */
export const SUPPORTED_LOCALES = [{ code: 'zh-CN', label: '简体中文' }] as const;

export type AppLocale = (typeof SUPPORTED_LOCALES)[number]['code'];

/** 当前默认（也是唯一）界面语言；功能稳定后再增加其它语言。 */
export const DEFAULT_LOCALE: AppLocale = 'zh-CN';
