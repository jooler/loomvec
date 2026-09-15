/**
 * 三步直传管线（P1 沿用 + P2 增强，P5 提升为跨 app 共享）：去重预检 → 预签名 →
 * PUT（带进度）→ 登记。配额超限（登记 403）映射为用户可读提示。
 * web（apps/web）与运营端（apps/ops）共用本模块，禁止在各 app 内复制。
 */
import { api } from '@loomvec/sdk-ts';
import { extractApiError, sha256Hex } from './format';
import { t } from '../i18n';

/**
 * 用户在去重确认中放弃上传时，uploadFile 抛出的 Error 消息（流程哨兵，非用户文案）。
 * 捕获方比较该常量判断“用户主动跳过”，展示 toast 用 ui:upload.* 文案。
 */
export const UPLOAD_SKIPPED = 'UPLOAD_SKIPPED';

/** 配额超限原因 → 用户可读提示（登记资产 403 details.quota_reason）。 */
export function quotaReasonLabel(reason: string): string {
  return t(`ui:upload.quotaReason.${reason}`, {
    defaultValue: t('ui:upload.quotaExceededFallback', { reason }),
  });
}

/** 从错误体里取 details.quota_reason（无则 null）。 */
export function quotaReasonOf(err: unknown): string | null {
  if (err && typeof err === 'object') {
    const details = (err as { details?: { quota_reason?: unknown } }).details;
    if (details && typeof details.quota_reason === 'string') return details.quota_reason;
  }
  return null;
}

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
      else reject(new Error(t('ui:upload.putFailed', { status: xhr.status })));
    };
    xhr.onerror = () => reject(new Error(t('ui:upload.putNetworkError')));
    xhr.send(file);
  });
}

export interface UploadOptions {
  spaceId?: string | null;
  /** 上传目标文件夹（空/缺省 = 空间根目录；Finder 目录树挂靠） */
  folderId?: string | null;
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
 * 单文件完整上传。失败抛错（message 已用户可读）；用户放弃上传抛 Error(UPLOAD_SKIPPED)。
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
        throw new Error(UPLOAD_SKIPPED);
      }
    }
  } catch (e) {
    if (e instanceof Error && e.message === UPLOAD_SKIPPED) throw e;
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
  if (presign.error) throw new Error(extractApiError(presign.error, t('ui:upload.presignFailed')));

  // 3) PUT 对象存储（带进度；Content-Type 必须与预签名请求一致）
  await putWithProgress(presign.data.upload_url, file, contentType, (pct) =>
    opts.onProgress?.(pct),
  );

  // 4) 登记资产并触发管线（配额超限在此返回 403）；folder_id 挂靠目标文件夹
  const created = await api.POST('/api/v1/assets', {
    body: {
      key: presign.data.key,
      filename: file.name,
      size: file.size,
      content_type: contentType,
      space_id: opts.spaceId ?? undefined,
      folder_id: opts.folderId ?? undefined,
    },
  });
  if (created.error) {
    const reason = quotaReasonOf(created.error);
    if (reason) throw new Error(quotaReasonLabel(reason));
    throw new Error(extractApiError(created.error, t('ui:upload.registerFailed')));
  }
  return { assetId: created.data.id, name: created.data.name };
}
