import { describe, expect, it } from 'vitest';
import { findMentionTrigger, removeMentionTrigger } from './chat-mention';

describe('findMentionTrigger', () => {
  it('detects @ at start and after whitespace', () => {
    expect(findMentionTrigger('@abc', 4)).toEqual({ start: 0, query: 'abc' });
    expect(findMentionTrigger('帮我总结 @论文', 8)).toEqual({ start: 5, query: '论文' });
    expect(findMentionTrigger('hi @', 4)).toEqual({ start: 3, query: '' });
  });

  it('ignores @ inside a word (e-mail) and after the query ended', () => {
    expect(findMentionTrigger('a@b.com', 7)).toBeNull();
    expect(findMentionTrigger('@abc 总结', 8)).toBeNull();
  });

  it('only looks at text before the caret', () => {
    expect(findMentionTrigger('@abc def', 4)).toEqual({ start: 0, query: 'abc' });
  });
});

describe('removeMentionTrigger', () => {
  it('drops the @query text and keeps the rest', () => {
    expect(removeMentionTrigger('请看 @pap 然后总结', 3, 7)).toBe('请看  然后总结');
  });
});
