import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router';
import { Toaster } from '@loomvec/ui/components/ui/sonner';
import { ThemeProvider } from '@loomvec/ui/components/theme-provider';
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
        {/* AuthProvider 必须包住 Router：RequireAuth 路由守卫消费 useAuth */}
        <AuthProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </AuthProvider>
      </QueryClientProvider>
      <Toaster richColors position="top-center" />
    </ThemeProvider>
  </React.StrictMode>,
);
