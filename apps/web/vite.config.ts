import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': new URL('./src', import.meta.url).pathname,
      '@loomvec/ui': new URL('../../packages/ui/src', import.meta.url).pathname,
    },
  },
  server: {
    port: 5173,
    proxy: {
      // dev 下 API 代理到本地 FastAPI（Makefile 默认端口 8080）
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/metrics': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
});
