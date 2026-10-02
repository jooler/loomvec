/**
 * Finder 条目排序（三视图共用）：文件夹置顶，组内按列比较；空值始终靠后。
 * 列表的上线/作者列取值也在此，展示与排序同源。
 */
import type { FinderAsset, FinderFolder } from './types';

export type FinderSortKey = 'name' | 'online' | 'authors' | 'journal' | 'size' | 'modified';

export type FinderSortDir = 'asc' | 'desc';

export interface FinderSort {
  key: FinderSortKey;
  dir: FinderSortDir;
}

export const DEFAULT_FINDER_SORT: FinderSort = { key: 'name', dir: 'asc' };

const SORT_KEYS: readonly FinderSortKey[] = [
  'name',
  'online',
  'authors',
  'journal',
  'size',
  'modified',
];

/** 本地持久化的排序偏好（JSON）→ FinderSort；缺失或非法回落默认。 */
export function parseFinderSort(raw: string | null): FinderSort {
  if (!raw) return DEFAULT_FINDER_SORT;
  try {
    const v = JSON.parse(raw) as Partial<FinderSort> | null;
    if (v && SORT_KEYS.includes(v.key as FinderSortKey) && (v.dir === 'asc' || v.dir === 'desc')) {
      return { key: v.key as FinderSortKey, dir: v.dir };
    }
  } catch {
    // 非法 JSON：回落默认
  }
  return DEFAULT_FINDER_SORT;
}

/** 点击列头：同列切换方向；换列时文本列升序，上线/大小/修改日期降序。 */
export function nextFinderSort(prev: FinderSort, key: FinderSortKey): FinderSort {
  if (prev.key === key) {
    return { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' };
  }
  const defaultDesc = key === 'size' || key === 'modified' || key === 'online';
  return { key, dir: defaultDesc ? 'desc' : 'asc' };
}

const zhCollator = new Intl.Collator('zh');

/** 名称比较（中文排序规则；共享 Collator，避免逐次 localeCompare 构造）。 */
export const compareFinderNames = zhCollator.compare;

/** 作者列表；兼容仅有 first_author 的旧数据。 */
export function paperAuthors(asset: FinderAsset): string[] {
  const paper = asset.paper;
  if (!paper) return [];
  if (paper.authors && paper.authors.length > 0) return paper.authors;
  return paper.first_author ? [paper.first_author] : [];
}

/** 上线日期：优先 Crossref published-online，否则回退年份（YYYY / YYYY-MM / YYYY-MM-DD）。 */
export function paperPublishedOnline(asset: FinderAsset): string {
  const online = asset.paper?.published_online?.trim();
  if (online) return online;
  const year = asset.paper?.year;
  return year != null ? String(year) : '';
}

/** 上线排序键：YYYY[-MM[-DD]] → YYYYMMDD 数值（缺月/日按 0）；空或无法解析为 NaN。 */
function onlineKey(asset: FinderAsset): number {
  const raw = paperPublishedOnline(asset);
  if (!raw) return NaN;
  const [y, m = 0, d = 0] = raw.split('-').map(Number);
  return y * 10000 + m * 100 + d;
}

/** 空串永远排在非空之后（与升降序无关）。 */
function cmpOptionalString(a: string, b: string, dir: 1 | -1): number {
  if (!a && !b) return 0;
  if (!a) return 1;
  if (!b) return -1;
  return compareFinderNames(a, b) * dir;
}

/** NaN（缺值或无法解析的日期）永远排在数值之后（与升降序无关）。 */
function cmpOptionalNumber(a: number, b: number, dir: 1 | -1): number {
  const an = Number.isNaN(a);
  const bn = Number.isNaN(b);
  if (an && bn) return 0;
  if (an) return 1;
  if (bn) return -1;
  if (a === b) return 0;
  return (a < b ? -1 : 1) * dir;
}

/** 名称升序 + id 兜底：比较键相等时保证稳定、确定的顺序。 */
function tieBreak(a: { name: string; id: string }, b: { name: string; id: string }): number {
  return compareFinderNames(a.name, b.name) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
}

function compareFinderFolders(a: FinderFolder, b: FinderFolder, sort: FinderSort): number {
  const dir: 1 | -1 = sort.dir === 'asc' ? 1 : -1;
  let primary = 0;
  if (sort.key === 'name') {
    primary = compareFinderNames(a.name, b.name) * dir;
  } else if (sort.key === 'modified') {
    primary = cmpOptionalNumber(Date.parse(a.created_at), Date.parse(b.created_at), dir);
  }
  // 文件夹无上线/作者/期刊/大小：组内按名称升序
  return primary || tieBreak(a, b);
}

function compareFinderAssets(a: FinderAsset, b: FinderAsset, sort: FinderSort): number {
  const dir: 1 | -1 = sort.dir === 'asc' ? 1 : -1;
  let primary = 0;
  switch (sort.key) {
    case 'name':
      primary = compareFinderNames(a.name, b.name) * dir;
      break;
    case 'online':
      primary = cmpOptionalNumber(onlineKey(a), onlineKey(b), dir);
      break;
    case 'authors':
      primary = cmpOptionalString(paperAuthors(a).join('; '), paperAuthors(b).join('; '), dir);
      break;
    case 'journal':
      primary = cmpOptionalString(a.paper?.journal ?? '', b.paper?.journal ?? '', dir);
      break;
    case 'size':
      primary = cmpOptionalNumber(a.size_bytes, b.size_bytes, dir);
      break;
    case 'modified':
      primary = cmpOptionalNumber(Date.parse(a.created_at), Date.parse(b.created_at), dir);
      break;
  }
  return primary || tieBreak(a, b);
}

/** 按当前排序返回新数组（不改入参）：文件夹与资产各自排序，渲染时文件夹在前。 */
export function sortFinderFolders(
  folders: readonly FinderFolder[],
  sort: FinderSort,
): FinderFolder[] {
  return [...folders].sort((a, b) => compareFinderFolders(a, b, sort));
}

export function sortFinderAssets(assets: readonly FinderAsset[], sort: FinderSort): FinderAsset[] {
  return [...assets].sort((a, b) => compareFinderAssets(a, b, sort));
}
