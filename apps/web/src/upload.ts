/**
 * 三步直传管线（P1 沿用 + P2 增强）：去重预检 → 预签名 → PUT（带进度）→ 登记。
 * 配额超限（登记 403）映射为用户可读提示。
 */
import { api } from '@loomvec/sdk-ts';
import { extractApiError, quotaReasonOf, sha256Hex, QUOTA_REASON_LABEL } from './utils';

/**
 * 后端 EXT_TO_MIME（services/core/pipeline/mime.py）的镜像：
 * 预签名会把 Content-Type 纳入签名；file.type 为空时后端按扩展推断，
 * 客户端 PUT 必须发送完全相同的值，否则对象存储校验签名不匹配（403）。
 */
const EXT_TO_MIME: Record<string, string> = {
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.txt': 'text/plain',
  '.md': 'text/markdown',
  '.markdown': 'text/markdown',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.bmp': 'image/bmp',
  '.mp4': 'video/mp4',
  '.mov': 'video/quicktime',
  '.webm': 'video/webm',
  '.mkv': 'video/x-matroska',
  '.mp3': 'audio/mpeg',
  '.wav': 'audio/x-wav',
  '.m4a': 'audio/mp4',
  '.flac': 'audio/flac',
};

function extOf(filename: string): string {
  const i = filename.lastIndexOf('.');
  return i >= 0 ? filename.slice(i).toLowerCase() : '';
}

/** 与后端 presign 相同口径的 Content-Type：浏览器探测值优先，扩展名兜底。 */
function effectiveContentType(file: File): string {
  return file.type || EXT_TO_MIME[extOf(file.name)] || 'application/octet-stream';
}

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
  // 预签名 / PUT / 登记三处必须使用同一个 Content-Type（签名一致性）
  const contentType = effectiveContentType(file);

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
      content_type: contentType,
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
      content_type: contentType,
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
