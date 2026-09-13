/**
 * 共享格式化与错误工具（web / admin 单源；app 内经各自 utils.ts 再导出使用）。
 */

/** 从 openapi-fetch 的 error（后端 {code,message,details} 或 FastAPI 422 校验体）提取人类可读信息。 */
export function extractApiError(err: unknown, fallback = '请求失败'): string {
  if (err && typeof err === 'object') {
    const e = err as Record<string, unknown>;
    if (typeof e.message === 'string' && e.message) return e.message;
    if (typeof e.reason === 'string' && e.reason) return e.reason;
    // FastAPI 校验错误：{ detail: [{ msg, loc, type }] }
    if (Array.isArray(e.detail)) {
      const first = (e.detail[0] ?? null) as { msg?: unknown } | null;
      if (first && typeof first.msg === 'string' && first.msg) return first.msg;
    }
    if (typeof e.detail === 'string' && e.detail) return e.detail;
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

/** 字节数格式化（null/undefined 与非有限值返回 -）。 */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '-';
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  if (n < 1024 ** 4) return `${(n / 1024 ** 3).toFixed(2)} GB`;
  return `${(n / 1024 ** 4).toFixed(2)} TB`;
}

/** ISO 时间 → 本地可读时间（空值返回 -）。 */
export function formatDateTime(v: string | null | undefined): string {
  if (!v) return '-';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString('zh-CN', { hour12: false });
}

/** 配额使用率展示：quota 为 0 视为不限。 */
export function formatQuota(used: number, quota: number): string {
  if (!quota) return `${formatBytes(used)} / 不限`;
  return `${formatBytes(used)} / ${formatBytes(quota)}（${((used / quota) * 100).toFixed(1)}%）`;
}

/** 秒数格式化（阶段耗时用）。 */
export function formatSeconds(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '-';
  if (n < 1) return `${(n * 1000).toFixed(0)} ms`;
  if (n < 60) return `${n.toFixed(2)} s`;
  return `${(n / 60).toFixed(1)} min`;
}

/** Blob → ArrayBuffer（兼容 jsdom 等无 Blob.arrayBuffer 的环境）。 */
export function blobToArrayBuffer(data: Blob): Promise<ArrayBuffer> {
  if (typeof data.arrayBuffer === 'function') return data.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsArrayBuffer(data);
  });
}

/** 文件内容 SHA-256（与后端校验和口径一致：sha256(bytes).hexdigest()），用于去重预检。 */
export async function sha256Hex(data: Blob): Promise<string> {
  const buf = await blobToArrayBuffer(data);
  const digest = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}
