import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { extractApiError, formatBytes, quotaReasonOf, sha256Hex } from './utils';

describe('extractApiError', () => {
  it('提取后端 message 字段', () => {
    expect(extractApiError({ code: 'x', message: '空间不存在' })).toBe('空间不存在');
  });

  it('提取 FastAPI 422 校验错误', () => {
    expect(extractApiError({ detail: [{ msg: 'field required', loc: [], type: 'missing' }] })).toBe(
      'field required',
    );
  });

  it('兜底默认文案', () => {
    expect(extractApiError(null, '加载失败')).toBe('加载失败');
    expect(extractApiError(new Error('网络错误'))).toBe('网络错误');
  });
});

describe('quotaReasonOf', () => {
  it('读取 details.quota_reason', () => {
    expect(quotaReasonOf({ message: '配额超限', details: { quota_reason: 'space_file_count_exceeded' } })).toBe(
      'space_file_count_exceeded',
    );
    expect(quotaReasonOf({ message: '配额超限' })).toBeNull();
  });
});

describe('formatBytes', () => {
  it('按阶梯格式化', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
    expect(formatBytes(1.5 * 1024 ** 3)).toBe('1.50 GB');
  });
});

describe('sha256Hex', () => {
  it('与后端口径一致（sha256(bytes).hexdigest()）', { skip: typeof crypto?.subtle === 'undefined' }, async () => {
    const raw = 'hello loomvec';
    const expected = createHash('sha256').update(raw).digest('hex');
    await expect(sha256Hex(new Blob([raw]))).resolves.toBe(expected);
  });
});
