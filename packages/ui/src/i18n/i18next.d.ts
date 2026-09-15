/**
 * i18next 类型增强（ui 包内生效）：为 useTranslation/t 提供 common+ui 命名空间的
 * key 自动补全。各 app 在自身 src/i18n/ 下有同名声明（含 app 命名空间）。
 */
import type common from './locales/zh-CN/common.json';
import type ui from './locales/zh-CN/ui.json';

declare module 'i18next' {
  interface CustomTypeOptions {
    defaultNS: 'common';
    /** key 未命中当前命名空间时的回退链（与 initI18n 运行时配置一致） */
    fallbackNS: ['common', 'ui'];
    resources: {
      common: typeof common;
      ui: typeof ui;
    };
  }
}
