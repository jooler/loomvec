/**
 * @loomvec/sdk-ts —— 由 OpenAPI 契约生成的 TypeScript SDK（P0-API-03）。
 *
 * 流水线：`scripts/export_openapi.py` 产出 openapi.json → `pnpm sdk:generate`
 * 生成 src/sdk/schema.d.ts → 前端一律经 `createApiClient` 调用，禁止手写请求。
 * CI 校验：重新生成后 `git diff --exit-code`，保证契约与代码同步。
 */
import createClient, { type Middleware } from 'openapi-fetch';
import type { paths } from './sdk/schema';

// 导出的 openapi.json 中路径已含 /api/v1 前缀，故 baseUrl 留空（相对当前 origin）
const DEFAULT_BASE_URL = '';
const TOKEN_STORAGE_KEY = 'loomvec.access_token';

export type ApiClient = ReturnType<typeof createClient<paths>>;

export function getStoredToken(): string | null {
  if (typeof localStorage === 'undefined') return null;
  return localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function storeToken(token: string): void {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
}

export interface CreateApiClientOptions {
  baseUrl?: string;
  getToken?: () => string | null;
  onUnauthorized?: () => void;
  getRequestId?: () => string;
}

export function createApiClient(options: CreateApiClientOptions = {}): ApiClient {
  const {
    baseUrl = DEFAULT_BASE_URL,
    getToken = getStoredToken,
    onUnauthorized,
    getRequestId = () => crypto.randomUUID(),
  } = options;

  const client = createClient<paths>({ baseUrl });

  const middleware: Middleware = {
    onRequest({ request }) {
      const token = getToken();
      if (token) {
        request.headers.set('Authorization', `Bearer ${token}`);
      }
      request.headers.set('X-Request-Id', getRequestId());
      return request;
    },
    onResponse({ response }) {
      if (response.status === 401) {
        clearToken();
        onUnauthorized?.();
      }
      return undefined;
    },
  };
  client.use(middleware);
  return client;
}

export const api = createApiClient();
