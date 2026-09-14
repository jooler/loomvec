/**
 * 运维端 API 客户端与统一解包。
 * - 客户端经 createApiClient 创建（非 SDK 默认单例），以便注册 401 处理：
 *   会话中途 token 过期时清登录态并回登录页，而非让每个查询静默失败。
 * - unwrap：openapi-fetch 返回 { data, error }，非 2xx 时抛出可读错误
 *   （页面 catch 后 toast），成功直接给 data。
 */
import { createApiClient } from '@loomvec/sdk-ts';
import { extractApiError } from './utils';

type UnauthorizedHandler = () => void;

let onUnauthorized: UnauthorizedHandler = () => {
  // AuthProvider 注册前的兜底（如极早期请求）：直接回登录页
  if (window.location.hash !== '#/login') window.location.hash = '#/login';
};

/** AuthProvider 挂载时注册/卸载时清除（handler 内清 React 登录态）。 */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler ?? (() => {
    if (window.location.hash !== '#/login') window.location.hash = '#/login';
  });
}

export const api = createApiClient({ onUnauthorized: () => onUnauthorized() });

/** openapi-fetch 统一解包：非 2xx 时抛出可读错误，成功断言为 T。 */
export async function unwrap<T>(p: Promise<{ data?: unknown; error?: unknown }>): Promise<T> {
  const resp = await p;
  if (resp.error != null) throw new Error(extractApiError(resp.error));
  return resp.data as T;
}
