/**
 * 管理域调用统一解包：openapi-fetch 返回 { data, error }，
 * 非 2xx 时抛出可读错误（页面 catch 后 message.error），成功直接给 data。
 */
import { api } from '@loomvec/sdk-ts';
import { extractApiError } from './utils';

/** openapi-fetch 统一解包：非 2xx 时抛出可读错误（页面 catch 后 message.error），成功断言为 T。 */
export async function unwrap<T>(p: Promise<{ data?: unknown; error?: unknown }>): Promise<T> {
  const resp = await p;
  if (resp.error != null) throw new Error(extractApiError(resp.error));
  return resp.data as T;
}

export { api };
