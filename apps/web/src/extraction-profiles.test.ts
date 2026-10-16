import { describe, expect, it } from 'vitest';
import {
  entityTypeLabel,
  isProfileStale,
  resolveProfile,
  shortTypeLabel,
  type ExtractionProfile,
  type ExtractionProfileList,
} from '@loomvec/ui/lib/extraction-profiles';

const profile = (key: string, types: [string, string][]): ExtractionProfile => ({
  key,
  label: key,
  description: '',
  is_builtin: key !== 'mine',
  entity_types: types.map(([type, label]) => ({ type, label })),
});

const LIST: ExtractionProfileList = {
  default: 'general',
  items: [
    profile('general', [
      ['organization', '组织/机构（公司、政府机关、学校、团体等）'],
      ['concept', '领域公认的概念、术语、理论'],
    ]),
    profile('legal', [
      ['organization', '机构（法院、检察院、行政机关等）'],
      ['statute', '法律法规及具体条款'],
    ]),
    profile('mine', [['widget', 'Widget (自定义)']]),
  ],
};

describe('shortTypeLabel', () => {
  it('cuts at the first full-width or ASCII parenthesis', () => {
    expect(shortTypeLabel('组织/机构（公司、政府机关）', 'x')).toBe('组织/机构');
    expect(shortTypeLabel('Widget (自定义)', 'x')).toBe('Widget');
  });

  it('keeps labels without parentheses and falls back when nothing precedes them', () => {
    expect(shortTypeLabel('人物', 'person')).toBe('人物');
    expect(shortTypeLabel('（括号开头）', 'person')).toBe('person');
  });
});

describe('resolveProfile', () => {
  it('follows the global default when the key is empty', () => {
    expect(resolveProfile(LIST, null)?.key).toBe('general');
    expect(resolveProfile(LIST, undefined)?.key).toBe('general');
  });

  it('picks the space profile when it exists', () => {
    expect(resolveProfile(LIST, 'legal')?.key).toBe('legal');
  });

  it('falls back to the default for a stale key, like the pipeline does', () => {
    expect(resolveProfile(LIST, 'deleted')?.key).toBe('general');
  });

  it('is undefined while the list is loading', () => {
    expect(resolveProfile(undefined, 'legal')).toBeUndefined();
  });
});

describe('isProfileStale', () => {
  it('flags only non-empty keys missing from the list', () => {
    expect(isProfileStale(LIST, 'deleted')).toBe(true);
    expect(isProfileStale(LIST, 'legal')).toBe(false);
    expect(isProfileStale(LIST, null)).toBe(false);
    expect(isProfileStale(undefined, 'deleted')).toBe(false);
  });
});

describe('entityTypeLabel', () => {
  it('uses the labels of the space effective profile, not the first profile that has the type', () => {
    expect(entityTypeLabel(resolveProfile(LIST, 'legal'), 'organization')).toBe('机构');
    expect(entityTypeLabel(resolveProfile(LIST, null), 'organization')).toBe('组织/机构');
  });

  it('returns the raw type when the effective profile does not define it', () => {
    expect(entityTypeLabel(resolveProfile(LIST, 'legal'), 'concept')).toBe('concept');
    expect(entityTypeLabel(undefined, 'person')).toBe('person');
  });
});
