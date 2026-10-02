import { describe, expect, it } from 'vitest';
import {
  DEFAULT_FINDER_SORT,
  nextFinderSort,
  parseFinderSort,
  sortFinderAssets,
  sortFinderFolders,
  type FinderSort,
} from '@loomvec/ui/components/finder/finder-sort';
import type {
  FinderAsset,
  FinderFolder,
  FinderPaperMeta,
} from '@loomvec/ui/components/finder/types';

function asset(id: string, over: Partial<FinderAsset> = {}, paper?: FinderPaperMeta): FinderAsset {
  return {
    id,
    name: `${id}.pdf`,
    ext: '.pdf',
    mime_type: 'application/pdf',
    size_bytes: 0,
    status: 'ready',
    status_reason: null,
    review_status: null,
    is_image: false,
    folder_id: null,
    created_at: '2026-01-01T00:00:00Z',
    paper: paper ?? null,
    ...over,
  };
}

function folder(id: string, name: string, created_at = '2026-01-01T00:00:00Z'): FinderFolder {
  return { id, parent_id: null, name, created_at };
}

const ids = (items: Array<{ id: string }>) => items.map((x) => x.id);
const asc = (key: FinderSort['key']): FinderSort => ({ key, dir: 'asc' });
const desc = (key: FinderSort['key']): FinderSort => ({ key, dir: 'desc' });

describe('nextFinderSort', () => {
  it('toggles direction on the same column', () => {
    expect(nextFinderSort(asc('name'), 'name')).toEqual(desc('name'));
    expect(nextFinderSort(desc('name'), 'name')).toEqual(asc('name'));
  });

  it('defaults text columns to asc and date/size columns to desc', () => {
    expect(nextFinderSort(desc('size'), 'journal')).toEqual(asc('journal'));
    expect(nextFinderSort(asc('name'), 'online')).toEqual(desc('online'));
    expect(nextFinderSort(asc('name'), 'size')).toEqual(desc('size'));
    expect(nextFinderSort(asc('name'), 'modified')).toEqual(desc('modified'));
  });
});

describe('parseFinderSort', () => {
  it('restores a valid persisted sort', () => {
    expect(parseFinderSort(JSON.stringify(desc('online')))).toEqual(desc('online'));
  });

  it('falls back to default on missing or invalid values', () => {
    for (const raw of [
      null,
      '',
      'not json',
      'null',
      '{"key":"status","dir":"asc"}',
      '{"key":"name","dir":"up"}',
    ]) {
      expect(parseFinderSort(raw)).toEqual(DEFAULT_FINDER_SORT);
    }
  });
});

describe('sortFinderAssets', () => {
  it('keeps empty values last in both directions', () => {
    const items = [
      asset('none'),
      asset('j-b', {}, { journal: 'Nature' }),
      asset('j-a', {}, { journal: 'Cell' }),
    ];
    expect(ids(sortFinderAssets(items, asc('journal')))).toEqual(['j-a', 'j-b', 'none']);
    expect(ids(sortFinderAssets(items, desc('journal')))).toEqual(['j-b', 'j-a', 'none']);
  });

  it('sorts online dates with year-only and unpadded values comparable', () => {
    const items = [
      asset('y2023', {}, { year: 2023 }),
      asset('d2023-5-7', {}, { published_online: '2023-5-7' }),
      asset('d2023-12', {}, { published_online: '2023-12' }),
      asset('d2022', {}, { published_online: '2022-11-30', year: 2024 }),
      asset('bad', {}, { published_online: 'n/a' }),
      asset('none'),
    ];
    expect(ids(sortFinderAssets(items, asc('online')))).toEqual([
      'd2022',
      'y2023',
      'd2023-5-7',
      'd2023-12',
      'bad',
      'none',
    ]);
  });

  it('sorts authors with first_author fallback', () => {
    const items = [
      asset('zhang', {}, { authors: ['Zhang W', 'Li M'] }),
      asset('legacy', {}, { first_author: 'Brown T' }),
      asset('none', {}, {}),
    ];
    expect(ids(sortFinderAssets(items, asc('authors')))).toEqual(['legacy', 'zhang', 'none']);
  });

  it('sorts timestamps by instant rather than string form', () => {
    const items = [
      // 字符串序与时刻序相反：08:30+08:00 = 00:30Z，早于 01:00Z
      asset('later', { created_at: '2026-01-01T01:00:00Z' }),
      asset('earlier', { created_at: '2026-01-01T08:30:00+08:00' }),
      asset('bad', { created_at: 'unknown' }),
    ];
    expect(ids(sortFinderAssets(items, asc('modified')))).toEqual(['earlier', 'later', 'bad']);
    expect(ids(sortFinderAssets(items, desc('modified')))).toEqual(['later', 'earlier', 'bad']);
  });

  it('breaks ties by name then id and does not mutate input', () => {
    const items = [
      asset('b', { name: 'same.pdf', size_bytes: 1 }),
      asset('a', { name: 'same.pdf', size_bytes: 1 }),
      asset('c', { name: 'alpha.pdf', size_bytes: 1 }),
    ];
    expect(ids(sortFinderAssets(items, desc('size')))).toEqual(['c', 'a', 'b']);
    expect(ids(items)).toEqual(['b', 'a', 'c']);
  });
});

describe('sortFinderFolders', () => {
  const items = [
    folder('f1', '乙', '2026-03-01T00:00:00Z'),
    folder('f2', '甲', '2026-01-01T00:00:00Z'),
    folder('f3', 'B', '2026-02-01T00:00:00Z'),
  ];

  it('follows name and modified sort direction', () => {
    expect(ids(sortFinderFolders(items, desc('modified')))).toEqual(['f1', 'f3', 'f2']);
    expect(ids(sortFinderFolders(items, asc('name')))).toEqual(
      ids(sortFinderFolders(items, desc('name'))).reverse(),
    );
  });

  it('keeps name order for columns folders do not have', () => {
    expect(ids(sortFinderFolders(items, desc('size')))).toEqual(
      ids(sortFinderFolders(items, asc('name'))),
    );
  });
});
