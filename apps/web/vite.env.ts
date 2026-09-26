/**
 * 读取仓库根 .env 的 LOOMVEC_ 端口变量（服务端口单源）：
 * LOOMVEC_API_PORT / LOOMVEC_WEB_PORT / LOOMVEC_ADMIN_PORT / LOOMVEC_OPS_PORT。
 * 仅 dev server 使用；部署端口由反向代理/K8s 决定。进程环境变量优先于 .env。
 * 本文件位于 <root>/apps/<app>/，URL ../../ 即仓库根。
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const repoRoot = fileURLToPath(new URL('../../', import.meta.url));

function parseLoomvecEnv(): Record<string, string> {
  const out: Record<string, string> = {};
  const envPath = repoRoot + '.env';
  if (!existsSync(envPath)) return out;
  for (const line of readFileSync(envPath, 'utf-8').split('\n')) {
    const m = line.match(/^\s*(LOOMVEC_[A-Z_]+)\s*=\s*(.*?)\s*$/);
    if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
  return out;
}

const envVars = parseLoomvecEnv();

export function loomvecPort(envKey: string, fallback: number): number {
  return Number(process.env[envKey] ?? envVars[envKey] ?? fallback);
}

export const apiTarget = `http://localhost:${loomvecPort('LOOMVEC_API_PORT', 38080)}`;

/** 对象存储 endpoint（.env LOOMVEC_STORAGE__ENDPOINT）：/s3 同源代理目标，须与 API 签名用的 endpoint 一致。 */
export const storageTarget =
  process.env.LOOMVEC_STORAGE__ENDPOINT ??
  envVars.LOOMVEC_STORAGE__ENDPOINT ??
  'http://localhost:39000';

/**
 * 预签名 URL 同源代理：API 请求带上前缀声明，对象存储请求经 /s3 转发。
 * changeOrigin 把 Host 改回 endpoint、rewrite 剥掉前缀，二者都是 SigV4 签名校验的前提。
 */
export const devProxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: true,
    headers: { 'X-Loomvec-Storage-Prefix': '/s3' },
  },
  '/s3': {
    target: storageTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/s3/, ''),
  },
};
