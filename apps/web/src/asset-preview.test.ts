import { describe, expect, it } from 'vitest';
import { isMineruDocumentMime, isMineruParsePending } from '@loomvec/ui/lib/asset-preview';

describe('isMineruParsePending', () => {
  it('keeps a processing PDF in the parsing state', () => {
    expect(isMineruParsePending('application/pdf', 'processing')).toBe(true);
  });

  it.each([
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  ])('recognizes every MIME type processed by MinerU: %s', (mimeType) => {
    expect(isMineruDocumentMime(mimeType)).toBe(true);
  });

  it('does not treat a processing image as a MinerU document', () => {
    expect(isMineruParsePending('image/png', 'processing')).toBe(false);
  });

  it('stops the pending state after the document job finishes', () => {
    expect(isMineruParsePending('application/pdf', 'ready')).toBe(false);
  });
});
