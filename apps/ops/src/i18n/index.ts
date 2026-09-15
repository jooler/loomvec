/**
 * ops i18n 初始化：注入本 app 命名空间，与共享命名空间（common/ui）合并。
 * 语言资源在 src/i18n/locales/zh-CN/<ns>.json；新增语言见 @loomvec/ui/i18n 的 locales。
 * 引入即初始化（资源内联，同步完成）：main.tsx / test-setup.ts 首行 import 本模块。
 */
import { initI18n, t as sharedT } from '@loomvec/ui/i18n';

import auth from './locales/zh-CN/auth.json';
import groups from './locales/zh-CN/groups.json';
import layout from './locales/zh-CN/layout.json';
import overview from './locales/zh-CN/overview.json';
import spaces from './locales/zh-CN/spaces.json';

const i18n = initI18n({
  'zh-CN': {
    auth,
    groups,
    layout,
    overview,
    spaces,
  },
});

export default i18n;

/** 非 React 模块（模块级 zod schema、菜单/业务字典等）使用的翻译函数；key 必须带 'ns:' 前缀。 */
export const t = sharedT;
