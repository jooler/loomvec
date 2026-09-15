import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { HashRouter } from 'react-router';
import { Toaster } from '@loomvec/ui/components/ui/sonner';
import { ThemeProvider } from '@loomvec/ui/components/theme-provider';
// i18n 必须最先初始化：页面模块的模块级文案（zod 校验消息等）在 import 阶段取值
import '@/i18n';
import { AuthProvider } from './auth';
import App from './App';
import './index.css';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 30_000 } },
});

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {/* ThemeProvider 最外层：布局切换按钮与 Toaster 都消费 useTheme */}
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        {/* AuthProvider 必须包住 Router：useAuth 在路由守卫（RequireAuth）中消费 */}
        <AuthProvider>
          {/* HashRouter：静态托管（对象存储/Nginx）零配置 */}
          <HashRouter>
            <App />
          </HashRouter>
        </AuthProvider>
      </QueryClientProvider>
      <Toaster richColors position="top-center" />
    </ThemeProvider>
  </React.StrictMode>,
);
