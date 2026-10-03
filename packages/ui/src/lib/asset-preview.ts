/**
 * 资产预览的解析能力与处理状态判定。
 *
 * 与后端 `core.pipeline.mime.MINERU_MIMES` 保持一致；前端在解析产物尚未写入时
 * 需要据此区分「文档解析中」和「该类型不走文档解析」。
 */
const MINERU_DOCUMENT_MIMES = new Set([
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);

export function isMineruDocumentMime(mimeType: string | null | undefined): boolean {
  return !!mimeType && MINERU_DOCUMENT_MIMES.has(mimeType.toLowerCase());
}

export function isMineruParsePending(
  mimeType: string | null | undefined,
  status: string | null | undefined,
): boolean {
  return isMineruDocumentMime(mimeType) && (status === 'pending' || status === 'processing');
}
