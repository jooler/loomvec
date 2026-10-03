import { fileURLToPath } from 'node:url';
import { apiTarget, devProxy, loomvecPort } from './vite.env';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // fileURLToPath：避免路径含空格/中文时 URL.pathname 百分号编码导致别名失效
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@loomvec/ui': fileURLToPath(new URL('../../packages/ui/src', import.meta.url)),
    },
  },
  server: {
    // 端口单源 LOOMVEC_ADMIN_PORT（.env，默认 35174）；API 代理目标单源 LOOMVEC_API_PORT
    port: loomvecPort('LOOMVEC_ADMIN_PORT', 35174),
    // 监听所有接口，便于开发容器/远程环境的端口转发访问。
    host: '0.0.0.0',
    allowedHosts: true, // 允许开发容器/隧道的转发 Host
    proxy: {
      ...devProxy,
      '/metrics': { target: apiTarget, changeOrigin: true },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 稳定的大依赖单拆：框架/数据层/表单校验各自缓存，避免业务迭代使整包缓存失效
        manualChunks: {
          react: ['react', 'react-dom', 'react-router'],
          query: ['@tanstack/react-query', '@tanstack/react-table'],
          form: ['react-hook-form', '@hookform/resolvers', 'zod'],
        },
      },
    },
  },
});
