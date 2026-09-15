import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FlaskConical, ListChecks, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import type { ApiClient } from '@loomvec/sdk-ts';

import { cn } from 'cn';
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

const UNIT_TYPE_TEXT: Record<string, string> = {
  text: '文本',
  table: '表格',
  image: '图片',
};

const CHUNK_METHOD_TEXT: Record<string, string> = {
  llm_markers: 'LLM 分片',
  structural_fallback: '结构化兜底',
  manual: '手动',
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
          <DialogTitle>{mode === 'create' ? '新增 chunk' : '编辑 chunk'}</DialogTitle>
          <DialogDescription>
            {mode === 'create'
              ? '追加为本资产的最后一个分片，保存后立即向量化并进入检索索引。'
              : '修改标题或内容保存后，将立即重新向量化。'}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label>标题（可选）</Label>
            <Input
              value={values.title}
              maxLength={512}
              placeholder="chunk 标题"
              onChange={(e) => setValues((v) => ({ ...v, title: e.target.value }))}
            />
          </div>
          <div className="space-y-1.5">
            <Label>内容</Label>
            <Textarea
              value={values.content}
              rows={8}
              placeholder="chunk 正文（表格类可粘贴 HTML 片段）"
              onChange={(e) => setValues((v) => ({ ...v, content: e.target.value }))}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>关键词（逗号分隔，可选）</Label>
              <Input
                value={values.keywords}
                placeholder="kw1, kw2"
                onChange={(e) => setValues((v) => ({ ...v, keywords: e.target.value }))}
              />
            </div>
            {mode === 'create' && (
              <div className="space-y-1.5">
                <Label>类型</Label>
                <Select
                  value={values.unit_type}
                  onValueChange={(v) => setValues((prev) => ({ ...prev, unit_type: v }))}
                >
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="text">文本</SelectItem>
                    <SelectItem value="table">表格</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button disabled={!valid || submitting} onClick={() => onSubmit(values)}>
            {submitting ? '保存中…' : '保存'}
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
      if (error) throw new Error(extractApiError(error, '检索测试失败'));
      return data;
    },
  });

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <FlaskConical className="size-4 text-muted-foreground" />
        <p className="text-sm font-medium">检索测试</p>
        <p className="text-xs text-muted-foreground">输入查询验证本资产的 chunk 是否可被召回</p>
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
          placeholder="例如：核心结论是什么？"
        />
        <Button type="submit" size="sm" disabled={query.trim().length === 0}>
          测试
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
        <p className="py-2 text-sm text-muted-foreground">没有召回任何 chunk——可尝试调整分片内容。</p>
      )}
      {(test.data?.items ?? []).map((hit) => (
        <div key={hit.unit_id} className="rounded-md border p-2.5">
          <div className="mb-1 flex items-center gap-2">
            <Badge variant="secondary">{(hit.score * 100).toFixed(0)}</Badge>
            <span className="text-xs text-muted-foreground">
              {UNIT_TYPE_TEXT[hit.unit_type] ?? hit.unit_type}
              {(() => {
                const pages = (hit.locator ?? {}) as { pages?: number[] };
                return pages.pages?.length
                  ? ` · 第 ${pages.pages.map((p) => p + 1).join(',')} 页`
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
    const t = setTimeout(() => {
      setQ(input);
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
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
      if (error) throw new Error(extractApiError(error, '加载 chunk 列表失败'));
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
        if (error) throw new Error(extractApiError(error, '保存失败'));
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
        if (error) throw new Error(extractApiError(error, '新增失败'));
      }
    },
    onSuccess: () => {
      toast.success(dialog?.mode === 'edit' ? '已保存并重新向量化' : '已新增 chunk 并向量化');
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
      if (error) throw new Error(extractApiError(error, '删除失败'));
    },
    onSuccess: (_data, ids) => {
      toast.success(`已删除 ${ids.length} 个 chunk（含向量清理）`);
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
            placeholder="搜索内容/标题…"
            className="h-8 w-48"
          />
          <Select value={unitType || ALL} onValueChange={(v) => { setUnitType(v === ALL ? '' : v); setPage(1); }}>
            <SelectTrigger className="h-8 w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部类型</SelectItem>
              <SelectItem value="text">文本</SelectItem>
              <SelectItem value="table">表格</SelectItem>
              <SelectItem value="image">图片</SelectItem>
            </SelectContent>
          </Select>
          <Button variant="outline" size="sm" onClick={() => void units.refetch()}>
            <RefreshCw /> 刷新
          </Button>
          {canManage && (
            <Button size="sm" onClick={() => setDialog({ mode: 'create' })}>
              <Plus /> 新增 chunk
            </Button>
          )}
        </div>

        {/* 批量操作条 */}
        {canManage && selected.size > 0 && (
          <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-1.5">
            <span className="text-sm">已选 {selected.size} 项</span>
            <div className="flex-1" />
            <ConfirmAction
              trigger={
                <Button variant="destructive" size="sm" disabled={bulkDelete.isPending}>
                  <Trash2 /> 批量删除
                </Button>
              }
              title="删除选中的 chunk？"
              description="将同步清理向量与索引，不可恢复。"
              confirmText="删除"
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
            title={q || unitType ? '没有符合筛选条件的 chunk' : '解析完成后这里会展示切分结果'}
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
                全选本页
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
                        <Badge variant="outline">{UNIT_TYPE_TEXT[u.unit_type] ?? u.unit_type}</Badge>
                        <Badge variant="secondary">
                          {CHUNK_METHOD_TEXT[u.chunk_method] ?? u.chunk_method}
                        </Badge>
                        {u.embed_model_version ? (
                          <StatusBadge tone="green">已向量化</StatusBadge>
                        ) : (
                          <StatusBadge tone="gray">未向量化</StatusBadge>
                        )}
                        <span className="text-xs text-muted-foreground">
                          {u.char_count} 字
                          {pages.length > 0 && ` · 第 ${pages.map((p) => p + 1).join(',')} 页`}
                          {u.locator?.start_line !== undefined &&
                            ` · 行 ${u.locator.start_line}-${u.locator.end_line}`}
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
                          title="删除该 chunk？"
                          description="将同步清理向量与索引，不可恢复。"
                          confirmText="删除"
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
              上一页
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
              下一页
            </Button>
          </div>
        )}

        <Separator />
        <button
          className="text-sm text-muted-foreground hover:text-foreground"
          onClick={() => setShowTest((v) => !v)}
        >
          {showTest ? '收起检索测试 ▾' : '展开检索测试 ▸'}
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
