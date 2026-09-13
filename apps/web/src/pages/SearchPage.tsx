import { useMemo, useState } from 'react';
import { RefreshCw, Search } from 'lucide-react';
import { ChevronDown, ChevronRight, Waypoints } from 'lucide-react';
import { useMutation } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { api } from '@loomvec/sdk-ts';
import { useMySpaces, useSpaceCategories, useSpaceTags } from '@/hooks';
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

const UNIT_TYPE_LABEL: Record<string, { tone: 'gray' | 'blue' | 'purple'; text: string }> = {
  text: { tone: 'gray', text: '文本' },
  table: { tone: 'blue', text: '表格' },
  image: { tone: 'purple', text: '图片' },
};

/** 分类筛选的“不过滤”哨兵值（Radix Select 不允许空串 value）。 */
const ALL = '__all__';

function fmtClock(t: number): string {
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

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
        图谱证据链（{evidence.length} 条路径）
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
              {ev.hops > 1 && <span className="ml-1 text-muted-foreground">（{ev.hops} 跳）</span>}
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
    mutationFn: (q: string) =>
      api.POST('/api/v1/search', {
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
      }),
  });

  const hits = search.data?.data?.items ?? [];

  const runSearch = () => {
    const q = query.trim();
    if (q) search.mutate(q);
  };

  const switchSpace = (next: string | undefined) => {
    // 切换检索范围：全部空间 ↔ 单个空间（保留query参数重新挂路由）
    if (next) navigate(`/s/${next}/search`);
    else navigate('/search');
  };

  return (
    <div className="space-y-4">
      <Card className="gap-3 py-4">
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Select value={spaceId} onValueChange={(v) => switchSpace(v)}>
              <SelectTrigger className="w-[200px]">
                <SelectValue placeholder="选择空间" />
              </SelectTrigger>
              <SelectContent>
                {(spaces.data ?? []).map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <MultiSelect
              className="w-[200px]"
              placeholder="类型（text/table/image）"
              value={unitTypes}
              options={[
                { value: 'text', label: '文本' },
                { value: 'table', label: '表格' },
                { value: 'image', label: '图片' },
              ]}
              onChange={setUnitTypes}
            />
            {spaceId && (
              <MultiSelect
                className="min-w-40"
                placeholder="标签"
                loading={tags.isLoading}
                value={tagIds}
                options={(tags.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
                onChange={setTagIds}
              />
            )}
            {spaceId && (
              <Select
                value={categoryId ?? ALL}
                onValueChange={(v) => setCategoryId(v === ALL ? undefined : v)}
              >
                <SelectTrigger className="w-36">
                  <SelectValue placeholder="分类" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>全部</SelectItem>
                  {(categories.data ?? []).map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <span className="flex items-center gap-2 text-sm">
              以文搜图
              <Switch
                checked={imageSearch === true}
                onCheckedChange={(v) => setImageSearch(v || undefined)}
              />
            </span>
            <span className="flex items-center gap-2 text-sm">
              图谱召回
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
                    ? `在「${spaceNameById.get(spaceId) ?? '该空间'}」内语义检索`
                    : '语义检索：命中语义单元并回溯到来源页码'
                }
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') runSearch();
                }}
              />
            </div>
            <Button size="lg" disabled={search.isPending} onClick={runSearch}>
              检索
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            dense + BM25 混合召回，RRF 融合，cross-encoder 精排。
            <button
              type="button"
              className="ml-3 text-primary hover:underline"
              onClick={() => setRerank(rerank === false ? undefined : false)}
            >
              {rerank === false ? '已关闭精排（A/B 对比）' : '关闭精排对比'}
            </button>
          </p>
        </CardContent>
      </Card>

      <Card className="py-4">
        <CardHeader className="items-center border-b pb-3">
          <CardTitle className="text-base">
            {spaceId ? `空间内检索 · ${spaceNameById.get(spaceId) ?? ''}` : '全部空间聚合检索'}
          </CardTitle>
          {search.data && (
            <CardAction>
              <Button variant="outline" size="sm" onClick={() => search.reset()}>
                <RefreshCw /> 清空结果
              </Button>
            </CardAction>
          )}
        </CardHeader>
        <CardContent>
          {search.isPending ? (
            <div className="grid place-items-center py-10">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : search.data && hits.length === 0 ? (
            <EmptyState title="没有命中的语义单元" />
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
                const unitMeta = UNIT_TYPE_LABEL[hit.unit_type];
                return (
                  <div
                    key={`${hit.asset_id}-${hit.unit_id}-${i}`}
                    className="cursor-pointer py-4 transition-colors hover:bg-muted/50"
                    onClick={() => navigate(`/a/${hit.asset_id}${qs ? `?${qs}` : ''}`)}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium">{hit.title || '(无标题片段)'}</span>
                      {unitMeta && <StatusBadge tone={unitMeta.tone}>{unitMeta.text}</StatusBadge>}
                      {!spaceId && hit.space_name && (
                        <StatusBadge tone="blue">{hit.space_name}</StatusBadge>
                      )}
                      <StatusBadge>score {hit.score.toFixed(4)}</StatusBadge>
                      {hit.scores?.graph !== undefined && <StatusBadge tone="green">图谱路径</StatusBadge>}
                    </div>
                    <div
                      className="mt-1.5 max-h-20 overflow-hidden text-sm"
                      dangerouslySetInnerHTML={{
                        __html: renderHighlight(hit.highlight) ?? escapeHtml(hit.text),
                      }}
                    />
                    <p className="mt-1.5 text-sm text-muted-foreground">
                      来源：{hit.asset_name}
                      {page !== undefined && ` · 第 ${page + 1} 页`}
                      {tStart !== undefined && ` · ${fmtClock(tStart)} 起`}
                      {locator?.start_line !== undefined &&
                        ` · 行 ${locator.start_line}-${locator.end_line}`}
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
