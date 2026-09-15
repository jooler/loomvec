import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { extractApiError, formatBytes, quotaReasonOf, sha256Hex } from './utils';

describe('extractApiError', () => {
  it('extracts backend message field', () => {
    expect(extractApiError({ code: 'x', message: 'space not found' })).toBe('space not found');
  });

  it('extracts FastAPI 422 validation error', () => {
    expect(extractApiError({ detail: [{ msg: 'field required', loc: [], type: 'missing' }] })).toBe(
      'field required',
    );
  });

  it('falls back to default message', () => {
    expect(extractApiError(null, 'load failed')).toBe('load failed');
    expect(extractApiError(new Error('network error'))).toBe('network error');
  });
});

describe('quotaReasonOf', () => {
  it('reads details.quota_reason', () => {
    expect(
      quotaReasonOf({ message: 'quota exceeded', details: { quota_reason: 'space_file_count_exceeded' } }),
    ).toBe('space_file_count_exceeded');
    expect(quotaReasonOf({ message: 'quota exceeded' })).toBeNull();
  });
});

describe('formatBytes', () => {
  it('formats by magnitude ladder', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2.0 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
    expect(formatBytes(1.5 * 1024 ** 3)).toBe('1.50 GB');
  });
});

describe('sha256Hex', () => {
  it('matches backend convention sha256(bytes).hexdigest()', { skip: typeof crypto?.subtle === 'undefined' }, async () => {
    const raw = 'hello loomvec';
    const expected = createHash('sha256').update(raw).digest('hex');
    await expect(sha256Hex(new Blob([raw]))).resolves.toBe(expected);
  });
});
