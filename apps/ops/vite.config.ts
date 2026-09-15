import { fileURLToPath } from 'node:url';
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
    port: 5175,
    proxy: {
      // dev 下 API 代理到本地 FastAPI（Makefile 默认端口 8080；8000 被 MinerU 占用）
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/metrics': { target: 'http://localhost:8080', changeOrigin: true },
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
