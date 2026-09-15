import { useMemo, useState } from 'react';
import { RefreshCw, Search } from 'lucide-react';
import { ChevronDown, ChevronRight, Waypoints } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { useMySpaces, useSpaceCategories, useSpaceTags } from '@/hooks';
import { extractApiError, formatClock } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@loomvec/ui/components/ui/select';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { Switch } from '@loomvec/ui/components/ui/switch';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { MultiSelect } from '@/components/multi-select';
import { StatusBadge } from '@loomvec/ui/components/status-badge';

/**
 * P2-WEB-04 检索升级：空间/聚合双模式 + 过滤器（类型/标签/分类）。
 * 命中卡带空间名徽标（聚合模式）与 unit_type 标识（图片命中显示“图片”）。
 * /search 与 /s/:spaceId/search 共用本组件。
 */

/** 语义单元类型 → 徽标 tone 与文案 key（复用 ui 命名空间 chunks.type*）。 */
const UNIT_TYPE_META: Record<
  string,
  { tone: 'gray' | 'blue' | 'purple'; key: 'typeText' | 'typeTable' | 'typeImage' }
> = {
  text: { tone: 'gray', key: 'typeText' },
  table: { tone: 'blue', key: 'typeTable' },
  image: { tone: 'purple', key: 'typeImage' },
};

/** 分类筛选的“不过滤”哨兵值（Radix Select 不允许空串 value）。 */
const ALL = '__all__';

/** 空间选择器的「全部空间（聚合）」哨兵值。 */
const ALL_SPACES = '__all_spaces__';

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderHighlight(h?: string | null): string | undefined {
  // 后端只输出 <em> 标签：先整体转义再放行 <em>，防注入
  if (!h) return undefined;
  return escapeHtml(h)
    .replace(/&lt;em&gt;/g, '<em>')
    .replace(/&lt;\/em&gt;/g, '</em>');
}

interface UnitLocator {
  pages?: number[];
  start_line?: number;
  end_line?: number;
  time_start?: number;
  time_end?: number;
}

interface EvidencePath {
  head: { key: string; name: string; type: string };
  relation: { type: string; chunk_ids?: string[] };
  tail: { key: string; name: string; type: string };
  hops: number;
}

/** P3-WEB-04 图谱证据链（实体→关系→实体路径）可展开面板。 */
function EvidencePanel({ evidence }: { evidence: EvidencePath[] }) {
  const { t } = useTranslation('search');
  const [open, setOpen] = useState(false);
  if (evidence.length === 0) return null;
  return (
    <div className="mt-2">
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        onClick={() => setOpen(!open)}
      >
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
        <Waypoints className="size-3" />
        {t('evidenceCount', { count: evidence.length })}
      </button>
      {open && (
        <ul className="mt-1 space-y-1 rounded-md bg-muted/60 p-2">
          {evidence.map((ev, i) => (
            <li key={i} className="text-xs">
              <span className="font-medium">{ev.head.name}</span>
              <span className="mx-1 rounded bg-primary/10 px-1 py-0.5 font-mono text-[10px] text-primary">
                {ev.relation.type}
              </span>
              <span className="font-medium">{ev.tail.name}</span>
              {ev.hops > 1 && (
                <span className="ml-1 text-muted-foreground">{t('hops', { count: ev.hops })}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function SearchPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const { t } = useTranslation('search');
  const [query, setQuery] = useState('');
  const [rerank, setRerank] = useState<boolean | undefined>(undefined);
  const [imageSearch, setImageSearch] = useState<boolean | undefined>(undefined);
  const [useGraph, setUseGraph] = useState(true);
  const [unitTypes, setUnitTypes] = useState<string[]>([]);
  const [tagIds, setTagIds] = useState<string[]>([]);
  const [categoryId, setCategoryId] = useState<string | undefined>();

  const spaces = useMySpaces();
  const tags = useSpaceTags(spaceId);
  const categories = useSpaceCategories(spaceId);

  const spaceNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const s of spaces.data ?? []) map.set(s.id, s.name);
    return map;
  }, [spaces.data]);

  const search = useMutation({
    mutationFn: async (q: string) => {
      const { data, error } = await api.POST('/api/v1/search', {
        body: {
          query: q,
          top_k: 10,
          rerank,
          image_search: imageSearch,
          use_graph: useGraph ? undefined : false,
          space_id: spaceId,
          unit_types: unitTypes.length > 0 ? unitTypes : undefined,
          tag_ids: tagIds.length > 0 ? tagIds : undefined,
          category_id: categoryId || undefined,
        },
      });
      if (error) throw new Error(extractApiError(error, t('searchFailed')));
      return data;
    },
  });

  const hits = search.data?.items ?? [];

  const runSearch = () => {
    const q = query.trim();
    if (q) search.mutate(q);
  };

  const switchSpace = (next: string) => {
    // 切换检索范围：全部空间 ↔ 单个空间（重新挂路由，筛选状态随路由复位）
    if (next === ALL_SPACES) navigate('/search');
    else navigate(`/s/${next}/search`);
  };

  return (
    <div className="space-y-4">
      <Card className="gap-3 py-4">
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Select value={spaceId ?? ALL_SPACES} onValueChange={switchSpace}>
              <SelectTrigger className="w-[200px]">
                <SelectValue placeholder={t('spacePlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_SPACES}>{t('allSpaces')}</SelectItem>
                {(spaces.data ?? []).map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <MultiSelect
              className="w-[200px]"
              placeholder={t('unitTypePlaceholder')}
              value={unitTypes}
              options={[
                { value: 'text', label: t('chunks.typeText') },
                { value: 'table', label: t('chunks.typeTable') },
                { value: 'image', label: t('chunks.typeImage') },
              ]}
              onChange={setUnitTypes}
            />
            {spaceId && (
              <MultiSelect
                className="min-w-40"
                placeholder={t('tagPlaceholder')}
                loading={tags.isLoading}
                value={tagIds}
                options={(tags.data ?? []).map((tag) => ({ value: tag.id, label: tag.name }))}
                onChange={setTagIds}
              />
            )}
            {spaceId && (
              <Select
                value={categoryId ?? ALL}
                onValueChange={(v) => setCategoryId(v === ALL ? undefined : v)}
              >
                <SelectTrigger className="w-36">
                  <SelectValue placeholder={t('categoryPlaceholder')} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>{t('action.all')}</SelectItem>
                  {(categories.data ?? []).map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <span className="flex items-center gap-2 text-sm">
              {t('imageSearchLabel')}
              <Switch
                checked={imageSearch === true}
                onCheckedChange={(v) => setImageSearch(v || undefined)}
              />
            </span>
            <span className="flex items-center gap-2 text-sm">
              {t('graphRecallLabel')}
              <Switch checked={useGraph} onCheckedChange={setUseGraph} />
            </span>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                className="h-10 pl-9"
                placeholder={
                  spaceId
                    ? t('inSpacePlaceholder', {
                        name: spaceNameById.get(spaceId) ?? t('thisSpace'),
                      })
                    : t('globalPlaceholder')
                }
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') runSearch();
                }}
              />
            </div>
            <Button size="lg" disabled={search.isPending} onClick={runSearch}>
              {t('searchButton')}
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            {t('recallDesc')}
            <button
              type="button"
              className="ml-3 text-primary hover:underline"
              onClick={() => setRerank(rerank === false ? undefined : false)}
            >
              {rerank === false ? t('rerankOff') : t('rerankToggle')}
            </button>
          </p>
        </CardContent>
      </Card>

      <Card className="py-4">
        <CardHeader className="items-center border-b pb-3">
          <CardTitle className="text-base">
            {spaceId
              ? t('spaceScopeTitle', { name: spaceNameById.get(spaceId) ?? '' })
              : t('allScopeTitle')}
          </CardTitle>
          {search.data && (
            <CardAction>
              <Button variant="outline" size="sm" onClick={() => search.reset()}>
                <RefreshCw /> {t('clearResults')}
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <CardContent>
          {search.isPending ? (
            <div className="grid place-items-center py-10">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : search.isError ? (
            <p className="text-sm text-destructive">{(search.error as Error).message}</p>
          ) : search.data && hits.length === 0 ? (
            <EmptyState title={t('emptyHits')} />
          ) : (
            <div className="divide-y">
              {hits.map((hit, i) => {
                const locator = hit.locator as UnitLocator;
                const page = locator?.pages?.[0];
                const tStart = locator?.time_start;
                const params = new URLSearchParams();
                if (page !== undefined) params.set('page', String(page + 1));
                if (tStart !== undefined) params.set('t', String(tStart));
                const qs = params.toString();
                const unitMeta = UNIT_TYPE_META[hit.unit_type];
                return (
                  <div
                    key={`${hit.asset_id}-${hit.unit_id}-${i}`}
                    className="cursor-pointer py-4 transition-colors hover:bg-muted/50"
                    onClick={() => navigate(`/a/${hit.asset_id}${qs ? `?${qs}` : ''}`)}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">{hit.title || t('untitledHit')}</span>
                      {unitMeta && (
                        <StatusBadge tone={unitMeta.tone}>{t(`chunks.${unitMeta.key}`)}</StatusBadge>
                      )}
                      {!spaceId && hit.space_name && (
                        <StatusBadge tone="blue">{hit.space_name}</StatusBadge>
                      )}
                      <StatusBadge>score {hit.score.toFixed(4)}</StatusBadge>
                      {hit.scores?.graph !== undefined && (
                        <StatusBadge tone="green">{t('graphPathBadge')}</StatusBadge>
                      )}
                    </div>
                    <div
                      className="mt-1.5 max-h-20 overflow-hidden text-sm"
                      dangerouslySetInnerHTML={{
                        __html: renderHighlight(hit.highlight) ?? escapeHtml(hit.text),
                      }}
                    />
                    <p className="mt-1.5 text-sm text-muted-foreground">
                      {t('sourceLabel')}
                      {hit.asset_name}
                      {page !== undefined && ` · ${t('chunks.pageLocator', { pages: page + 1 })}`}
                      {tStart !== undefined && ` · ${t('fromTime', { time: formatClock(tStart) })}`}
                      {locator?.start_line !== undefined &&
                        ` · ${t('chunks.lineLocator', {
                          start: locator.start_line,
                          end: locator.end_line ?? '',
                        })}`}
                    </p>
                    <EvidencePanel evidence={(hit.graph_evidence ?? []) as unknown as EvidencePath[]} />
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
