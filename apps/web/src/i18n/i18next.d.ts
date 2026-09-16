/**
 * i18next 类型增强（web app）：key 自动补全与插值变量检查。
 * 约定：组件内 useTranslation('<ns>') 后用裸 key；未命中自身命名空间时按
 * fallbackNS 顺序回退（common → ui）。非 React 模块用 '@/i18n' 的 t，显式 'ns:key'。
 */
import type common from '@loomvec/ui/i18n/locales/zh-CN/common.json';
import type ui from '@loomvec/ui/i18n/locales/zh-CN/ui.json';
import type agent from './locales/zh-CN/agent.json';
import type assetDetail from './locales/zh-CN/assetDetail.json';
import type assets from './locales/zh-CN/assets.json';
import type auth from './locales/zh-CN/auth.json';
import type graph from './locales/zh-CN/graph.json';
import type layout from './locales/zh-CN/layout.json';
import type notifications from './locales/zh-CN/notifications.json';
import type profile from './locales/zh-CN/profile.json';
import type review from './locales/zh-CN/review.json';
import type search from './locales/zh-CN/search.json';
import type spaces from './locales/zh-CN/spaces.json';

declare module 'i18next' {
  interface CustomTypeOptions {
    defaultNS: 'common';
    /** key 未命中当前命名空间时的回退链（与 @loomvec/ui/i18n 运行时配置一致） */
    fallbackNS: ['common', 'ui'];
    resources: {
      common: typeof common;
      ui: typeof ui;
      agent: typeof agent;
      assetDetail: typeof assetDetail;
      assets: typeof assets;
      auth: typeof auth;
      graph: typeof graph;
      layout: typeof layout;
      notifications: typeof notifications;
      profile: typeof profile;
      review: typeof review;
      search: typeof search;
      spaces: typeof spaces;
    };
  }
}
