/**
 * 跨 app 共享的 i18n 核心（i18next + react-i18next，默认实例单例）。
 *
 * - 各 app 在入口（main.tsx / test-setup.ts）`import '@/i18n'` 完成初始化：
 *   app 命名空间经 initI18n(resources) 注入，与共享命名空间（common/ui）合并。
 * - 本包内 React 组件用 useTranslation；纯函数库（lib/format、lib/upload）用 t。
 * - 资源全部内联（无后端/懒加载），init 同步完成；新增语言见 locales.ts。
 */
import i18next, { type Resource } from 'i18next';
import { initReactI18next } from 'react-i18next';
import { DEFAULT_LOCALE, SUPPORTED_LOCALES } from './locales';

import commonZhCN from './locales/zh-CN/common.json';
import uiZhCN from './locales/zh-CN/ui.json';

/** 共享命名空间资源；新增语言在此按语言 code 追加。 */
const SHARED_RESOURCES: Resource = {
  'zh-CN': { common: commonZhCN, ui: uiZhCN },
};

function mergeResources(base: Resource, extra: Resource): Resource {
  const out: Resource = { ...base };
  for (const [lng, namespaces] of Object.entries(extra)) {
    out[lng] = { ...(out[lng] ?? {}), ...namespaces };
  }
  return out;
}

/**
 * 初始化 i18next（幂等）：注册共享命名空间并合并 app 注入的命名空间资源。
 * app 侧典型用法：`export const i18n = initI18n({ 'zh-CN': { auth, spaces } })`。
 */
export function initI18n(appResources: Resource = {}): typeof i18next {
  if (!i18next.isInitialized) {
    i18next.use(initReactI18next).init({
      lng: DEFAULT_LOCALE,
      fallbackLng: DEFAULT_LOCALE,
      supportedLngs: SUPPORTED_LOCALES.map((l) => l.code),
      // 组件里统一用裸 key：先查自身命名空间，再回退 common → ui（见 i18next.d.ts）
      fallbackNS: ['common', 'ui'],
      // React 已对插值做 XSS 转义，i18next 不必二次转义
      interpolation: { escapeValue: false },
      returnNull: false,
      // 资源内联同步就绪；不用 Suspense，避免测试/入口额外包裹边界
      react: { useSuspense: false },
      resources: mergeResources(SHARED_RESOURCES, appResources),
    });
  }
  if (typeof document !== 'undefined') {
    document.documentElement.lang = i18next.resolvedLanguage ?? DEFAULT_LOCALE;
  }
  return i18next;
}

/** 面向纯函数库的宽松签名（接受模板拼接的动态 key，绕过 CustomTypeOptions 的字面量收窄）。 */
type LooseT = (key: string, options?: Record<string, unknown>) => string;

/** 非 React 模块（lib/format、lib/upload 等纯函数库）使用的翻译函数；key 需带命名空间前缀。 */
export const t: LooseT = (key, options) => (i18next.t as LooseT)(key, options);

/** 未来切换界面语言入口（当前仅 zh-CN）；切换后同步 <html lang>。 */
export async function setLocale(lang: string): Promise<void> {
  await i18next.changeLanguage(lang);
  if (typeof document !== 'undefined') {
    document.documentElement.lang = i18next.resolvedLanguage ?? DEFAULT_LOCALE;
  }
}

export { i18next };
export { DEFAULT_LOCALE, SUPPORTED_LOCALES } from './locales';
export type { AppLocale } from './locales';
