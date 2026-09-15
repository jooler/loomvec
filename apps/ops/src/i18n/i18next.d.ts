/**
 * i18next 类型增强（ops app）：key 自动补全与插值变量检查。
 * 约定：组件内 useTranslation('<ns>') 后用裸 key；未命中自身命名空间时按
 * fallbackNS 顺序回退（common → ui）。非 React 模块用 '@/i18n' 的 t，显式 'ns:key'。
 */
import type common from '@loomvec/ui/i18n/locales/zh-CN/common.json';
import type ui from '@loomvec/ui/i18n/locales/zh-CN/ui.json';
import type auth from './locales/zh-CN/auth.json';
import type groups from './locales/zh-CN/groups.json';
import type layout from './locales/zh-CN/layout.json';
import type overview from './locales/zh-CN/overview.json';
import type spaces from './locales/zh-CN/spaces.json';

declare module 'i18next' {
  interface CustomTypeOptions {
    defaultNS: 'common';
    /** key 未命中当前命名空间时的回退链（与 @loomvec/ui/i18n 运行时配置一致） */
    fallbackNS: ['common', 'ui'];
    resources: {
      common: typeof common;
      ui: typeof ui;
      auth: typeof auth;
      groups: typeof groups;
      layout: typeof layout;
      overview: typeof overview;
      spaces: typeof spaces;
    };
  }
}
