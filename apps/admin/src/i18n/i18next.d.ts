/**
 * i18next 类型增强（admin app）：key 自动补全与插值变量检查。
 * 约定：组件内 useTranslation('<ns>') 后用裸 key；未命中自身命名空间时按
 * fallbackNS 顺序回退（common → ui）。非 React 模块用 '@/i18n' 的 t，显式 'ns:key'。
 */
import type common from '@loomvec/ui/i18n/locales/zh-CN/common.json';
import type ui from '@loomvec/ui/i18n/locales/zh-CN/ui.json';
import type audit from './locales/zh-CN/audit.json';
import type auth from './locales/zh-CN/auth.json';
import type components from './locales/zh-CN/components.json';
import type layout from './locales/zh-CN/layout.json';
import type models from './locales/zh-CN/models.json';
import type open from './locales/zh-CN/open.json';
import type overview from './locales/zh-CN/overview.json';
import type pipeline from './locales/zh-CN/pipeline.json';
import type reviews from './locales/zh-CN/reviews.json';
import type settings from './locales/zh-CN/settings.json';
import type spaces from './locales/zh-CN/spaces.json';
import type system from './locales/zh-CN/system.json';
import type tenants from './locales/zh-CN/tenants.json';
import type users from './locales/zh-CN/users.json';

declare module 'i18next' {
  interface CustomTypeOptions {
    defaultNS: 'common';
    /** key 未命中当前命名空间时的回退链（与 @loomvec/ui/i18n 运行时配置一致） */
    fallbackNS: ['common', 'ui'];
    resources: {
      common: typeof common;
      ui: typeof ui;
      audit: typeof audit;
      auth: typeof auth;
      components: typeof components;
      layout: typeof layout;
      models: typeof models;
      open: typeof open;
      overview: typeof overview;
      pipeline: typeof pipeline;
      reviews: typeof reviews;
      settings: typeof settings;
      spaces: typeof spaces;
      system: typeof system;
      tenants: typeof tenants;
      users: typeof users;
    };
  }
}
