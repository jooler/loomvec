import '@testing-library/jest-dom/vitest';
// i18n 先于被测模块初始化（模块级文案在 import 阶段取值）
import './i18n';

// Node ≥22 的实验性全局 localStorage（未加 --localstorage-file 时 getter 恒为
// undefined，访问即告警）会遮蔽 jsdom 的 storage 实现——window.localStorage 一并
// 失效。测试环境补内存版 Storage（仅本 setup 生效，不影响生产代码）。
if (typeof localStorage === 'undefined') {
  const store = new Map<string, string>();
  const memoryStorage: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => store.get(k) ?? null,
    key: (i) => [...store.keys()][i] ?? null,
    removeItem: (k) => {
      store.delete(k);
    },
    setItem: (k, v) => {
      store.set(k, String(v));
    },
  };
  Object.defineProperty(globalThis, 'localStorage', {
    value: memoryStorage,
    configurable: true,
  });
  if (typeof window !== 'undefined') {
    Object.defineProperty(window, 'localStorage', {
      value: memoryStorage,
      configurable: true,
    });
  }
}

// jsdom 未实现 matchMedia：next-themes（ThemeProvider）等需要
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}
