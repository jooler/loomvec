import '@testing-library/jest-dom/vitest';
// i18n 先于被测模块初始化（模块级文案在 import 阶段取值）
import './i18n';

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
