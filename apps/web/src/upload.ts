/**
 * 三步直传管线（P1 沿用 + P2 增强）：去重预检 → 预签名 → PUT（带进度）→ 登记。
 * 配额超限（登记 403）映射为用户可读提示。
 */
import { api } from '@loomvec/sdk-ts';
import { extractApiError, quotaReasonOf, sha256Hex, QUOTA_REASON_LABEL } from './utils';

/** 用 XMLHttpRequest PUT 以获取上传进度。 */
function putWithProgress(
  url: string,
  file: File,
  contentType: string,
  onProgress: (pct: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', url);
    xhr.setRequestHeader('Content-Type', contentType);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`上传对象存储失败：HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('上传对象存储失败（网络错误）'));
    xhr.send(file);
  });
}

export interface UploadOptions {
  spaceId?: string | null;
  onProgress?: (pct: number) => void;
  /**
   * 去重预检命中时的用户确认：resolve(true) 继续上传，resolve(false) 放弃。
   * 未提供则命中也继续上传。
   */
  onDuplicate?: (existingNames: string[]) => Promise<boolean>;
}

export interface UploadResult {
  assetId: string;
  name: string;
}

/**
 * 单文件完整上传。失败抛错（message 已用户可读）；用户放弃上传抛 Error('已跳过上传')。
 */
export async function uploadFile(file: File, opts: UploadOptions = {}): Promise<UploadResult> {
  const contentType = file.type || 'application/octet-stream';

  // 1) 去重预检：同空间同 checksum 的现存资产
  try {
    const checksum = await sha256Hex(file);
    const dup = await api.GET('/api/v1/assets/duplicate-check', {
      params: { query: { checksum, space_id: opts.spaceId ?? undefined } },
    });
    if (!dup.error && dup.data.exists && dup.data.assets.length > 0) {
      const names = dup.data.assets.map((a) => a.name);
      if (opts.onDuplicate && !(await opts.onDuplicate(names))) {
        throw new Error('已跳过上传');
      }
    }
  } catch (e) {
    if (e instanceof Error && e.message === '已跳过上传') throw e;
    // 预检失败不阻断上传（后端语义：不阻断）
  }

  // 2) 预签名
  const presign = await api.POST('/api/v1/uploads', {
    body: {
      filename: file.name,
      size: file.size,
      content_type: file.type || undefined,
      space_id: opts.spaceId ?? undefined,
    },
  });
  if (presign.error) throw new Error(extractApiError(presign.error, '预签名失败'));

  // 3) PUT 对象存储（带进度；Content-Type 必须与预签名请求一致）
  await putWithProgress(presign.data.upload_url, file, contentType, (pct) =>
    opts.onProgress?.(pct),
  );

  // 4) 登记资产并触发管线（配额超限在此返回 403）
  const created = await api.POST('/api/v1/assets', {
    body: {
      key: presign.data.key,
      filename: file.name,
      size: file.size,
      content_type: file.type || undefined,
      space_id: opts.spaceId ?? undefined,
    },
  });
  if (created.error) {
    const reason = quotaReasonOf(created.error);
    if (reason) throw new Error(QUOTA_REASON_LABEL[reason] ?? `配额超限：${reason}`);
    throw new Error(extractApiError(created.error, '登记资产失败'));
  }
  return { assetId: created.data.id, name: created.data.name };
}
