import { useMemo, useRef, useState } from 'react';
import {
  FileText,
  Image as ImageIcon,
  Inbox,
  LayoutGrid,
  MessageSquare,
  RefreshCw,
  Upload,
  Waypoints,
} from 'lucide-react';
import type { ColumnDef } from '@tanstack/react-table';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useMySpaces, useSpaceCategories, useSpaceTags } from '@/hooks';
import { extractApiError, formatBytes, REVIEW_STATUS_META } from '@/utils';
import { uploadFile } from '@/upload';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@loomvec/ui/components/ui/select';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { Tabs, TabsList, TabsTrigger } from '@loomvec/ui/components/ui/tabs';
import { Tooltip, TooltipContent, TooltipTrigger } from '@loomvec/ui/components/ui/tooltip';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { MultiSelect } from '@/components/multi-select';
import { ReviewStatusTag } from '@/components/ReviewStatusTag';
import { StatusBadge } from '@loomvec/ui/components/status-badge';

/**
 * P2-WEB-02 资产管理：聚合 / 空间双模式 + 双视图（列表/缩略图网格）+ 多维筛选。
 * 空间模式额外提供 标签/分类 筛选与上传入口（配额与去重交互见上传页）。
 */

const STATUS_TONE: Record<string, { tone: 'gray' | 'blue' | 'green' | 'red'; text: string }> = {
  pending: { tone: 'gray', text: '排队中' },
  processing: { tone: 'blue', text: '处理中' },
  ready: { tone: 'green', text: '就绪' },
  failed: { tone: 'red', text: '失败' },
};

/** 筛选下拉的“不过滤”哨兵值（Radix Select 不允许空串 value）。 */
const ALL = '__all__';

interface AssetFilters {
  ext?: string;
  status?: string;
  review_status?: string;
  tagIds: string[];
  categoryId?: string;
}

const EMPTY_FILTERS: AssetFilters = { tagIds: [] };

export function AssetsPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<AssetFilters>(EMPTY_FILTERS);
  const [view, setView] = useState<'list' | 'grid'>('list');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);

  const spaces = useMySpaces();
  const tags = useSpaceTags(spaceId);
  const categories = useSpaceCategories(spaceId);

  const assets = useQuery({
    queryKey: ['assets', spaceId ?? 'all', filters],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets', {
        params: {
          query: {
            limit: 50,
            space_id: spaceId,
            ext: filters.ext || undefined,
            status: filters.status || undefined,
            review_status: filters.review_status || undefined,
            category_id: filters.categoryId || undefined,
            tag_ids: filters.tagIds.length > 0 ? filters.tagIds.join(',') : undefined,
          },
        },
      });
      if (error) throw new Error(extractApiError(error, '加载资产列表失败'));
      return data;
    },
    // 处理中轮询（P1 用轮询，SSE/Webhook 后置）
    refetchInterval: (query) =>
      query.state.data?.items.some((a) => ['pending', 'processing'].includes(a.status))
        ? 3000
        : false,
  });

  // 缩略图网格：列表 API 不含 renditions，为图片资产批量取详情中的 thumbnail URL
  const items = useMemo(() => assets.data?.items ?? [], [assets.data]);
  const imageIds = useMemo(
    () => items.filter((a) => a.is_image).slice(0, 24).map((a) => a.id),
    [items],
  );
  const thumbs = useQueries({
    queries: imageIds.map((id) => ({
      queryKey: ['asset-thumb', id],
      queryFn: async () => {
        const { data, error } = await api.GET('/api/v1/assets/{asset_id}', {
          params: { path: { asset_id: id } },
        });
        if (error) throw new Error(extractApiError(error, '加载缩略图失败'));
        return data.renditions.find((r) => r.kind === 'thumbnail')?.url ?? null;
      },
      staleTime: 10 * 60_000,
    })),
  });
  const thumbById = useMemo(() => {
    const map = new Map<string, string | null>();
    imageIds.forEach((id, i) => map.set(id, thumbs[i]?.data ?? null));
    return map;
  }, [imageIds, thumbs]);

  const spaceNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const s of spaces.data ?? []) map.set(s.id, s.name);
    return map;
  }, [spaces.data]);

  const retry = useMutation({
    mutationFn: async (assetId: string) => {
      const { error } = await api.POST('/api/v1/assets/{asset_id}/retry', {
        params: { path: { asset_id: assetId } },
        body: {},
      });
      if (error) throw new Error(extractApiError(error, '重试失败'));
    },
    onSuccess: () => {
      toast.success('已重新入队');
      void assets.refetch();
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: async (assetId: string) => {
      const { error } = await api.DELETE('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId } },
      });
      if (error) throw new Error(extractApiError(error, '删除失败'));
    },
    onSuccess: () => {
      toast.success('已删除（含向量清理）');
      void assets.refetch();
    },
    onError: (e) => toast.error(e.message),
  });

  // P1 聚合页保留拖拽上传（无空间上下文，登记到默认空间）；空间模式引导到上传页
  const uploadFiles = async (files: File[]) => {
    if (files.length === 0 || uploading) return;
    setUploading(true);
    setProgress(0);
    let doneCount = 0;
    let okCount = 0;
    for (const file of files) {
      const base = (doneCount / files.length) * 100;
      try {
        await uploadFile(file, {
          onProgress: (pct) => setProgress(Math.round(base + pct / files.length)),
        });
        okCount += 1;
      } catch (e) {
        toast.error(e instanceof Error ? e.message : '上传失败');
      }
      doneCount += 1;
    }
    setUploading(false);
    // 全部文件落定后刷新列表；有成功才失效缓存
    void assets.refetch();
    if (okCount > 0) {
      queryClient.invalidateQueries({ queryKey: ['assets'] });
    }
  };

  const setFilter = (patch: Partial<AssetFilters>) => setFilters((f) => ({ ...f, ...patch }));

  const reviewOptions = Object.entries(REVIEW_STATUS_META).map(([v, m]) => ({ value: v, label: m.text }));
  const statusOptions = Object.entries(STATUS_TONE).map(([v, s]) => ({ value: v, label: s.text }));

  type AssetRow = (typeof items)[number];

  const columns: ColumnDef<AssetRow, unknown>[] = [
    {
      accessorKey: 'name',
      header: '名称',
      cell: ({ row }) => (
        <button
          className="inline-flex items-center gap-1.5 text-sm hover:underline"
          onClick={() => navigate(`/a/${row.original.id}`)}
        >
          {row.original.is_image && <ImageIcon className="size-4 text-muted-foreground" />}
          {row.original.name}
        </button>
      ),
    },
    ...(spaceId
      ? []
      : [
          {
            accessorKey: 'space_id',
            header: '空间',
            cell: ({ row }) => {
              const id = row.original.space_id;
              return <span>{id ? spaceNameById.get(id) ?? id.slice(0, 8) : '-'}</span>;
            },
          } as ColumnDef<AssetRow, unknown>,
        ]),
    { accessorKey: 'ext', header: '类型' },
    {
      accessorKey: 'size_bytes',
      header: '大小',
      cell: ({ row }) => <span>{formatBytes(row.original.size_bytes)}</span>,
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) => {
        const meta = STATUS_TONE[row.original.status];
        const badge = (
          <StatusBadge tone={meta?.tone}>
            {meta?.text ?? row.original.status}
          </StatusBadge>
        );
        return row.original.status_reason ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="cursor-help">{badge}</span>
            </TooltipTrigger>
            <TooltipContent>{row.original.status_reason}</TooltipContent>
          </Tooltip>
        ) : (
          badge
        );
      },
    },
    {
      accessorKey: 'review_status',
      header: '审核',
      cell: ({ row }) => <ReviewStatusTag status={row.original.review_status} />,
    },
    {
      accessorKey: 'created_at',
      header: '创建时间',
      cell: ({ row }) => <span>{new Date(row.original.created_at).toLocaleString()}</span>,
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => (
        <div className="flex gap-1">
          {row.original.status === 'failed' && (
            <Button variant="outline" size="xs" onClick={() => retry.mutate(row.original.id)}>
              重试
            </Button>
          )}
          <ConfirmAction
            trigger={
              <Button variant="outline" size="xs" className="text-destructive hover:text-destructive">
                删除
              </Button>
            }
            title="删除资产？"
            description="将级联清理语义单元与向量。"
            confirmText="删除"
            danger
            onConfirm={() => remove.mutate(row.original.id)}
          />
        </div>
      ),
    },
  ];

  const grid = (
    <div className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-4">
      {items.map((a) => {
        const thumb = thumbById.get(a.id);
        const open = () => navigate(`/a/${a.id}`);
        return (
          <Card key={a.id} className="gap-2 py-3 transition-shadow hover:shadow-md">
            {a.is_image && thumb ? (
              <img
                alt={a.name}
                src={thumb}
                className="h-[120px] w-full cursor-pointer object-cover"
                onClick={open}
              />
            ) : (
              <div
                className="grid h-[120px] cursor-pointer place-items-center bg-muted/60"
                onClick={open}
              >
                {a.is_image ? (
                  <Spinner className="size-4 text-muted-foreground" />
                ) : (
                  <FileText className="size-10 text-muted-foreground/70" />
                )}
              </div>
            )}
            <CardContent className="space-y-1.5 px-3">
              <button
                className="block max-w-full truncate text-left text-[13px] font-medium hover:underline"
                onClick={open}
              >
                {a.name}
              </button>
              <div className="flex flex-wrap items-center gap-1">
                <StatusBadge tone={STATUS_TONE[a.status]?.tone}>
                  {STATUS_TONE[a.status]?.text ?? a.status}
                </StatusBadge>
                {a.review_status && <ReviewStatusTag status={a.review_status} />}
                <span className="text-xs text-muted-foreground">{formatBytes(a.size_bytes)}</span>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {spaceId ? `资产 · ${spaceNameById.get(spaceId) ?? '空间'}` : '资产（全部空间）'}
        </CardTitle>
        {spaceId && (
          <CardAction>
            <Button variant="outline" onClick={() => navigate(`/s/${spaceId}/chat`)}>
              <MessageSquare /> 问答
            </Button>
            <Button variant="outline" onClick={() => navigate(`/s/${spaceId}/graph`)}>
              <Waypoints /> 图谱
            </Button>
            <Button onClick={() => navigate(`/s/${spaceId}/upload`)}>
              <Upload /> 上传
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={filters.ext ?? ALL}
            onValueChange={(v) => setFilter({ ext: v === ALL ? undefined : v })}
          >
            <SelectTrigger className="w-[110px]">
              <SelectValue placeholder="类型" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部</SelectItem>
              {(
                [
                  ['.pdf', 'PDF'],
                  ['.docx', 'DOCX'],
                  ['.md', 'MD'],
                  ['.txt', 'TXT'],
                  ['.png', 'PNG'],
                ] as const
              ).map(([v, label]) => (
                <SelectItem key={v} value={v}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={filters.status ?? ALL}
            onValueChange={(v) => setFilter({ status: v === ALL ? undefined : v })}
          >
            <SelectTrigger className="w-[110px]">
              <SelectValue placeholder="状态" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部</SelectItem>
              {statusOptions.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select
            value={filters.review_status ?? ALL}
            onValueChange={(v) => setFilter({ review_status: v === ALL ? undefined : v })}
          >
            <SelectTrigger className="w-[120px]">
              <SelectValue placeholder="审核状态" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>全部</SelectItem>
              {reviewOptions.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {spaceId && (
            <MultiSelect
              className="min-w-40"
              placeholder="标签"
              loading={tags.isLoading}
              value={filters.tagIds}
              options={(tags.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
              onChange={(v) => setFilter({ tagIds: v })}
            />
          )}
          {spaceId && (
            <Select
              value={filters.categoryId ?? ALL}
              onValueChange={(v) => setFilter({ categoryId: v === ALL ? undefined : v })}
            >
              <SelectTrigger className="w-[140px]">
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
          <Tabs value={view} onValueChange={(v) => setView(v as 'list' | 'grid')}>
            <TabsList>
              <TabsTrigger value="list">
                <LayoutGrid className="size-4" /> 列表
              </TabsTrigger>
              <TabsTrigger value="grid">
                <ImageIcon className="size-4" /> 缩略图
              </TabsTrigger>
            </TabsList>
          </Tabs>
          <Button variant="outline" onClick={() => assets.refetch()}>
            <RefreshCw /> 刷新
          </Button>
        </div>

        {!spaceId && (
          <div
            className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed p-6 text-center"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              void uploadFiles(Array.from(e.dataTransfer.files));
            }}
          >
            <Inbox className="size-8 text-muted-foreground" />
            <p className="text-sm font-medium">拖拽文件上传（PDF / DOCX / PPTX / XLSX / TXT / MD）</p>
            <p className="text-sm text-muted-foreground">
              上传前自动做同空间去重预检；空间内上传请进入具体空间的上传页。
            </p>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.docx,.pptx,.xlsx,.txt,.md,.markdown"
              className="hidden"
              onChange={(e) => {
                const files = Array.from(e.target.files ?? []);
                e.target.value = '';
                void uploadFiles(files);
              }}
            />
            <Button
              variant="outline"
              size="sm"
              disabled={uploading}
              onClick={() => fileInputRef.current?.click()}
            >
              {uploading ? '上传中…' : '选择文件'}
            </Button>
            {uploading && <Progress value={progress} className="w-full max-w-xs" />}
          </div>
        )}

        {assets.isError ? (
          <p className="text-sm text-destructive">{(assets.error as Error).message}</p>
        ) : !assets.isLoading && items.length === 0 ? (
          <EmptyState title="没有符合筛选条件的资产" className="mt-6" />
        ) : view === 'grid' ? (
          grid
        ) : (
          <div className="mt-2">
            <DataTable
              columns={columns}
              data={items}
              loading={assets.isLoading}
              getRowId={(row) => row.id}
              emptyTitle="没有符合筛选条件的资产"
            />
          </div>
        )}
        <p className="text-sm text-muted-foreground">
          文本类资产也可经 <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">POST /api/v1/assets/text</code> 直接摄取。
        </p>
      </CardContent>
    </Card>
  );
}
