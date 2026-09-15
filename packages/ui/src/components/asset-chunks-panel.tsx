import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FlaskConical, ListChecks, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import type { ApiClient } from '@loomvec/sdk-ts';

import { cn } from 'cn';
import { useTranslation } from 'react-i18next';
import { Badge } from './ui/badge';
import { Button } from './ui/button';
import { Card, CardContent } from './ui/card';
import { Checkbox } from './ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';
import { Input } from './ui/input';
import { Label } from './ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select';
import { Separator } from './ui/separator';
import { Spinner } from './ui/spinner';
import { Textarea } from './ui/textarea';
import { ConfirmAction } from './confirm-action';
import { EmptyState } from './empty-state';
import { StatusBadge } from './status-badge';
import { extractApiError } from '../lib/format';

/**
 * chunk（语义单元）管理面板（RAGFlow「Document → Chunks」借鉴实现）：
 * 列表查看 / 关键词与类型过滤 / 手动新增 / 编辑（即时重新向量化）/ 批删 /
 * 检索测试（复用 POST /search 的 asset_ids 过滤，验证分片可召回性）。
 * web（用户端）与 ops（运营端）共用；经注入的 openapi-fetch client 调用。
 */

const PAGE_SIZE = 20;
const ALL = '__all__';

/** 语义单元类型 / 分片方式 → ui:chunks 下的文案 key；未知值回退原值。 */
const UNIT_TYPE_KEY: Record<string, string> = {
  text: 'typeText',
  table: 'typeTable',
  image: 'typeImage',
};

const CHUNK_METHOD_KEY: Record<string, string> = {
  llm_markers: 'sourceLlmMarkers',
  structural_fallback: 'sourceStructuralFallback',
  manual: 'sourceManual',
};

interface UnitRow {
  id: string;
  unit_type: string;
  title: string | null;
  content: string;
  keywords: string[];
  locator: { pages?: number[]; start_line?: number; end_line?: number };
  chunk_method: string;
  order_index: number;
  char_count: number;
  embed_model_version: string | null;
  created_at: string;
}

interface UnitFormValues {
  title: string;
  content: string;
  keywords: string;
  unit_type: string;
}

/** escape 后仅放行 <em>（与用户端检索页同口径，防注入）。 */
function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function renderHighlight(h?: string | null): string | undefined {
  if (!h) return undefined;
  return escapeHtml(h)
    .replace(/&lt;em&gt;/g, '<em>')
    .replace(/&lt;\/em&gt;/g, '</em>');
}

function UnitDialog({
  open,
  mode,
  initial,
  onClose,
  onSubmit,
  submitting,
}: {
  open: boolean;
  mode: 'create' | 'edit';
  initial: UnitFormValues;
  onClose: () => void;
  onSubmit: (values: UnitFormValues) => void;
  submitting: boolean;
}) {
  const [values, setValues] = useState(initial);
  const { t } = useTranslation();
  useEffect(() => {
    if (open) setValues(initial);
  }, [open, initial]);
  const valid = values.content.trim().length > 0;
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
    >
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {mode === 'create' ? t('chunks.dialogCreateTitle') : t('chunks.dialogEditTitle')}
          </DialogTitle>
          <DialogDescription>
            {mode === 'create' ? t('chunks.dialogCreateDesc') : t('chunks.dialogEditDesc')}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>{t('chunks.fieldTitle')}</Label>
            <Input
              value={values.title}
              maxLength={512}
              placeholder={t('chunks.titlePlaceholder')}
              onChange={(e) => setValues((v) => ({ ...v, title: e.target.value }))}
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('chunks.fieldContent')}</Label>
            <Textarea
              value={values.content}
              rows={8}
              placeholder={t('chunks.contentPlaceholder')}
              onChange={(e) => setValues((v) => ({ ...v, content: e.target.value }))}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>{t('chunks.fieldKeywords')}</Label>
              <Input
                value={values.keywords}
                placeholder="kw1, kw2"
                onChange={(e) => setValues((v) => ({ ...v, keywords: e.target.value }))}
              />
            </div>
            {mode === 'create' && (
              <div className="space-y-1.5">
                <Label>{t('chunks.fieldType')}</Label>
                <Select
                  value={values.unit_type}
                  onValueChange={(v) => setValues((prev) => ({ ...prev, unit_type: v }))}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="text">{t('chunks.typeText')}</SelectItem>
                    <SelectItem value="table">{t('chunks.typeTable')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t('action.cancel')}
          </Button>
          <Button disabled={!valid || submitting} onClick={() => onSubmit(values)}>
            {submitting ? t('action.saving') : t('action.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 检索测试：对本资产发一条查询，验证 chunk 是否可被召回（RAGFlow Retrieval Testing）。 */
function RetrievalTest({
  client,
  assetId,
  spaceId,
}: {
  client: ApiClient;
  assetId: string;
  spaceId?: string;
}) {
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const { t } = useTranslation();
  const test = useQuery({
    queryKey: ['asset-units-test', assetId, submitted],
    enabled: submitted.trim().length > 0,
    queryFn: async () => {
      const { data, error } = await client.POST('/api/v1/search', {
        body: {
          query: submitted.trim(),
          space_id: spaceId,
          asset_ids: [assetId],
          top_k: 5,
        },
      });
      if (error) throw new Error(extractApiError(error, t('chunks.retrieveTestFailed')));
      return data;
    },
  });

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <FlaskConical className="size-4 text-muted-foreground" />
        <p className="text-sm font-medium">{t('chunks.testTitle')}</p>
        <p className="text-xs text-muted-foreground">{t('chunks.testDesc')}</p>
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(query);
        }}
      >
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('chunks.testPlaceholder')}
        />
        <Button type="submit" size="sm" disabled={query.trim().length === 0}>
          {t('chunks.testButton')}
        </Button>
      </form>
      {test.isFetching && (
        <div className="grid place-items-center py-4">
          <Spinner className="size-4 text-muted-foreground" />
        </div>
      )}
      {test.isError && (
        <p className="text-sm text-destructive">{(test.error as Error).message}</p>
      )}
      {test.data && test.data.items.length === 0 && (
        <p className="py-2 text-sm text-muted-foreground">{t('chunks.testEmpty')}</p>
      )}
      {(test.data?.items ?? []).map((hit) => (
        <div key={hit.unit_id} className="rounded-md border p-2.5">
          <div className="mb-1 flex items-center gap-2">
            <Badge variant="secondary">{(hit.score * 100).toFixed(0)}</Badge>
            <span className="text-xs text-muted-foreground">
              {t(`chunks.${UNIT_TYPE_KEY[hit.unit_type] ?? ''}`, { defaultValue: hit.unit_type })}
              {(() => {
                const pages = (hit.locator ?? {}) as { pages?: number[] };
                return pages.pages?.length
                  ? ` · ${t('chunks.pageLocator', { pages: pages.pages.map((p) => p + 1).join(',') })}`
                  : '';
              })()}
            </span>
          </div>
          <div
            className="line-clamp-3 text-sm [&_em]:font-semibold [&_em]:text-primary"
            dangerouslySetInnerHTML={{
              __html: renderHighlight(hit.highlight) ?? escapeHtml(hit.text),
            }}
          />
        </div>
      ))}
    </div>
  );
}

/**
 * chunk 管理面板。
 * @param client openapi-fetch 客户端（web 用 SDK 单例，ops 用自建带 401 处理的实例）
 * @param canManage editor+ 才开放写操作（与资产编辑权限矩阵一致）
 */
export function AssetChunksPanel({
  client,
  assetId,
  spaceId,
  canManage,
  className,
}: {
  client: ApiClient;
  assetId: string;
  spaceId?: string;
  canManage: boolean;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  const [input, setInput] = useState('');
  const [q, setQ] = useState('');
  const [unitType, setUnitType] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<{ mode: 'create' | 'edit'; unit?: UnitRow } | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showTest, setShowTest] = useState(false);

  // 关键词防抖（300ms），变化时回到第一页
  useEffect(() => {
    const timer = setTimeout(() => {
      setQ(input);
      setPage(1);
    }, 300);
    return () => clearTimeout(timer);
  }, [input]);

  const units = useQuery({
    queryKey: ['asset-units', assetId, q, unitType, page],
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/assets/{asset_id}/units', {
        params: {
          path: { asset_id: assetId },
          query: {
            limit: PAGE_SIZE,
            offset: (page - 1) * PAGE_SIZE,
            unit_type: unitType || undefined,
            q: q || undefined,
          },
        },
      });
      if (error) throw new Error(extractApiError(error, t('chunks.loadFailed')));
      return data;
    },
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['asset-units', assetId] });
  };

  const saveUnit = useMutation({
    mutationFn: async (values: UnitFormValues) => {
      const keywords = values.keywords
        .split(/[,，]/)
        .map((k) => k.trim())
        .filter(Boolean);
      if (dialog?.mode === 'edit' && dialog.unit) {
        const { error } = await client.PATCH('/api/v1/assets/{asset_id}/units/{unit_id}', {
          params: { path: { asset_id: assetId, unit_id: dialog.unit.id } },
          body: {
            title: values.title || null,
            content: values.content,
            keywords,
          },
        });
        if (error) throw new Error(extractApiError(error, t('chunks.saveFailed')));
      } else {
        const { error } = await client.POST('/api/v1/assets/{asset_id}/units', {
          params: { path: { asset_id: assetId } },
          body: {
            content: values.content,
            title: values.title || null,
            keywords,
            unit_type: values.unit_type,
          },
        });
        if (error) throw new Error(extractApiError(error, t('chunks.createFailed')));
      }
    },
    onSuccess: () => {
      toast.success(
        dialog?.mode === 'edit' ? t('chunks.savedRevectorized') : t('chunks.createdRevectorized'),
      );
      setDialog(null);
      invalidate();
    },
  });

  const bulkDelete = useMutation({
    mutationFn: async (ids: string[]) => {
      const { error } = await client.POST('/api/v1/assets/{asset_id}/units/bulk-delete', {
        params: { path: { asset_id: assetId } },
        body: { unit_ids: ids },
      });
      if (error) throw new Error(extractApiError(error, t('chunks.deleteFailed')));
    },
    onSuccess: (_data, ids) => {
      toast.success(t('chunks.deletedChunks', { count: ids.length }));
      setSelected(new Set());
      invalidate();
    },
  });

  const rows = useMemo(
    () => (units.data?.items ?? []) as unknown as UnitRow[],
    [units.data],
  );
  const total = units.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pageIds = useMemo(() => rows.map((r) => r.id), [rows]);
  const allChecked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  return (
    <Card className={className}>
      <CardContent className="space-y-3">
        {/* 工具条：过滤 / 搜索 / 操作 */}
        <div className="flex flex-wrap items-center gap-2">
          <ListChecks className="size-4 text-muted-foreground" />
          <p className="text-sm font-medium">Chunks</p>
          <Badge variant="secondary">{total}</Badge>
          <div className="flex-1" />
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={t('chunks.searchPlaceholder')}
            className="h-8 w-48"
          />
          <Select value={unitType || ALL} onValueChange={(v) => { setUnitType(v === ALL ? '' : v); setPage(1); }}>
            <SelectTrigger className="h-8 w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>{t('chunks.allTypes')}</SelectItem>
              <SelectItem value="text">{t('chunks.typeText')}</SelectItem>
              <SelectItem value="table">{t('chunks.typeTable')}</SelectItem>
              <SelectItem value="image">{t('chunks.typeImage')}</SelectItem>
            </SelectContent>
          </Select>
          <Button variant="outline" size="sm" onClick={() => void units.refetch()}>
            <RefreshCw /> {t('action.refresh')}
          </Button>
          {canManage && (
            <Button size="sm" onClick={() => setDialog({ mode: 'create' })}>
              <Plus /> {t('chunks.newChunk')}
            </Button>
          )}
        </div>

        {/* 批量操作条 */}
        {canManage && selected.size > 0 && (
          <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-1.5">
            <span className="text-sm">{t('chunks.selectedCount', { count: selected.size })}</span>
            <div className="flex-1" />
            <ConfirmAction
              trigger={
                <Button variant="destructive" size="sm" disabled={bulkDelete.isPending}>
                  <Trash2 /> {t('chunks.batchDelete')}
                </Button>
              }
              title={t('chunks.deleteSelectedTitle')}
              description={t('chunks.deleteDesc')}
              confirmText={t('action.delete')}
              danger
              onConfirm={() => bulkDelete.mutate([...selected])}
            />
          </div>
        )}

        {/* 列表 */}
        {units.isError ? (
          <p className="text-sm text-destructive">{(units.error as Error).message}</p>
        ) : units.isLoading ? (
          <div className="grid place-items-center py-8">
            <Spinner className="size-5 text-muted-foreground" />
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            title={q || unitType ? t('chunks.emptyFiltered') : t('chunks.emptyInitial')}
            className="py-8"
          />
        ) : (
          <div className="space-y-2">
            {canManage && (
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <Checkbox
                  checked={allChecked}
                  onCheckedChange={(v) =>
                    setSelected(v === true ? new Set(pageIds) : new Set())
                  }
                />
                {t('chunks.selectAllPage')}
              </label>
            )}
            {rows.map((u) => {
              const isOpen = expanded.has(u.id);
              const pages = u.locator?.pages ?? [];
              return (
                <div key={u.id} className="rounded-md border p-3">
                  <div className="flex items-start gap-2">
                    {canManage && (
                      <Checkbox
                        className="mt-1"
                        checked={selected.has(u.id)}
                        onCheckedChange={(v) =>
                          setSelected((prev) => {
                            const next = new Set(prev);
                            if (v === true) next.add(u.id);
                            else next.delete(u.id);
                            return next;
                          })
                        }
                      />
                    )}
                    <div className="min-w-0 flex-1 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-mono text-xs text-muted-foreground">
                          #{u.order_index + 1}
                        </span>
                        <Badge variant="outline">
                          {t(`chunks.${UNIT_TYPE_KEY[u.unit_type] ?? ''}`, { defaultValue: u.unit_type })}
                        </Badge>
                        <Badge variant="secondary">
                          {t(`chunks.${CHUNK_METHOD_KEY[u.chunk_method] ?? ''}`, {
                            defaultValue: u.chunk_method,
                          })}
                        </Badge>
                        {u.embed_model_version ? (
                          <StatusBadge tone="green">{t('chunks.vectorized')}</StatusBadge>
                        ) : (
                          <StatusBadge tone="gray">{t('chunks.notVectorized')}</StatusBadge>
                        )}
                        <span className="text-xs text-muted-foreground">
                          {t('chunks.charCount', { count: u.char_count })}
                          {pages.length > 0 &&
                            ` · ${t('chunks.pageLocator', { pages: pages.map((p) => p + 1).join(',') })}`}
                          {u.locator?.start_line !== undefined &&
                            ` · ${t('chunks.lineLocator', {
                              start: u.locator.start_line,
                              end: u.locator.end_line,
                            })}`}
                        </span>
                      </div>
                      {u.title && <p className="text-sm font-medium">{u.title}</p>}
                      <div
                        className={cn(
                          'whitespace-pre-wrap break-words text-sm text-foreground/90',
                          !isOpen && 'line-clamp-3',
                        )}
                        onClick={() =>
                          setExpanded((prev) => {
                            const next = new Set(prev);
                            if (next.has(u.id)) next.delete(u.id);
                            else next.add(u.id);
                            return next;
                          })
                        }
                      >
                        {u.content}
                      </div>
                      {u.keywords.length > 0 && (
                        <div className="flex flex-wrap gap-1">
                          {u.keywords.map((k) => (
                            <Badge key={k} variant="outline" className="font-normal">
                              {k}
                            </Badge>
                          ))}
                        </div>
                      )}
                    </div>
                    {canManage && (
                      <div className="flex shrink-0 gap-1">
                        <Button
                          variant="outline"
                          size="icon-sm"
                          onClick={() =>
                            setDialog({
                              mode: 'edit',
                              unit: u,
                            })
                          }
                        >
                          <Pencil />
                        </Button>
                        <ConfirmAction
                          trigger={
                            <Button
                              variant="outline"
                              size="icon-sm"
                              className="text-destructive hover:text-destructive"
                            >
                              <Trash2 />
                            </Button>
                          }
                          title={t('chunks.deleteOneTitle')}
                          description={t('chunks.deleteDesc')}
                          confirmText={t('action.delete')}
                          danger
                          onConfirm={() => bulkDelete.mutate([u.id])}
                        />
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* 分页 */}
        {pageCount > 1 && (
          <div className="flex items-center justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              {t('pagination.prev')}
            </Button>
            <span className="text-xs text-muted-foreground">
              {page} / {pageCount}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= pageCount}
              onClick={() => setPage((p) => p + 1)}
            >
              {t('pagination.next')}
            </Button>
          </div>
        )}

        <Separator />
        <button
          className="text-sm text-muted-foreground hover:text-foreground"
          onClick={() => setShowTest((v) => !v)}
        >
          {showTest ? t('chunks.hideTest') : t('chunks.showTest')}
        </button>
        {showTest && <RetrievalTest client={client} assetId={assetId} spaceId={spaceId} />}
      </CardContent>

      <UnitDialog
        open={dialog !== null}
        mode={dialog?.mode ?? 'create'}
        initial={
          dialog?.mode === 'edit' && dialog.unit
            ? {
                title: dialog.unit.title ?? '',
                content: dialog.unit.content,
                keywords: (dialog.unit.keywords ?? []).join(', '),
                unit_type: dialog.unit.unit_type,
              }
            : { title: '', content: '', keywords: '', unit_type: 'text' }
        }
        submitting={saveUnit.isPending}
        onClose={() => setDialog(null)}
        onSubmit={(values) => saveUnit.mutate(values)}
      />
    </Card>
  );
}
