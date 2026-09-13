import { ThemeProvider as NextThemesProvider } from 'next-themes';

/**
 * 应用级明暗主题 Provider（基于 next-themes）：以 class 形式挂在 <html> 上，
 * 配合各 app index.css 的 .dark 变量与 index.html 里的防首屏闪烁脚本
 * （存储键沿用 next-themes 默认的 "theme"）。
 */
export function ThemeProvider(props: React.ComponentProps<typeof NextThemesProvider>) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      {...props}
    />
  );
}
