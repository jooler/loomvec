import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { apiTarget, loomvecPort } from './vite.env';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': new URL('./src', import.meta.url).pathname,
      '@loomvec/ui': new URL('../../packages/ui/src', import.meta.url).pathname,
    },
  },
  server: {
    // 端口单源 LOOMVEC_WEB_PORT（.env，默认 5173）；API 代理目标单源 LOOMVEC_API_PORT
    port: loomvecPort('LOOMVEC_WEB_PORT', 5173),
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
      '/metrics': { target: apiTarget, changeOrigin: true },
    },
  },
});
