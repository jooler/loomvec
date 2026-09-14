import { useMemo, useRef, useState } from 'react';
import { Image as ImageIcon, Inbox } from 'lucide-react';
import type { ColumnDef } from '@tanstack/react-table';
import { useMutation, useQueries, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useSpaceCategories, useSpaceTags } from '@/hooks';
import { extractApiError, formatBytes } from '@/utils';
import { uploadFile } from '@/upload';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent } from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { ReviewStatusTag } from '@/components/review-status-tag';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@loomvec/ui/components/ui/tooltip';
import { ACCEPT_TYPES, STATUS_TONE } from './constants';
import { AssetsFiltersBar } from './AssetsFiltersBar';
import { AssetGridView } from './AssetGridView';
import { EMPTY_FILTERS, useAssetsList, type AssetFilters, type AssetListItem } from './use-assets-list';

/**
 * 空间资产管理（挂在空间详情布局「资产」页签下）：多维筛选 + 双视图（列表/缩略图网格）+
 * 拖拽直传本空间（含同空间去重确认与配额错误提示）。
 */
export function AssetsPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [filters, setFilters] = useState<AssetFilters>(EMPTY_FILTERS);
  const [view, setView] = useState<'list' | 'grid'>('list');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  // 去重确认：resolve(true)=仍要上传，resolve(false)=放弃
  const [dupConfirm, setDupConfirm] = useState<{
    names: string[];
    resolve: (ok: boolean) => void;
  } | null>(null);

  const tags = useSpaceTags(spaceId);
  const categories = useSpaceCategories(spaceId);

  const assets = useAssetsList(spaceId, filters);

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

  // 拖拽直传本空间：逐个串行；命中同空间重复文件时弹窗确认
  const uploadFiles = async (files: File[]) => {
    if (files.length === 0 || uploading || !spaceId) return;
    setUploading(true);
    setProgress(0);
    let doneCount = 0;
    let okCount = 0;
    for (const file of files) {
      const base = (doneCount / files.length) * 100;
      try {
        await uploadFile(file, {
          spaceId,
          onProgress: (pct) => setProgress(Math.round(base + pct / files.length)),
          onDuplicate: (names) =>
            new Promise<boolean>((resolve) => {
              setDupConfirm({ names, resolve });
            }),
        });
        okCount += 1;
      } catch (e) {
        const msg = e instanceof Error ? e.message : '上传失败';
        if (msg === '已跳过上传') toast.info(`已跳过重复文件：${file.name}`);
        else toast.error(msg);
      }
      doneCount += 1;
    }
    setUploading(false);
    // invalidate 会刷新当前激活查询（含其它筛选 key），无需再手动 refetch
    if (okCount > 0) {
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
    }
  };

  const columns: ColumnDef<AssetListItem, unknown>[] = [
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

  return (
    <Card>
      <CardContent className="space-y-4">
        <AssetsFiltersBar
          spaceId={spaceId}
          filters={filters}
          onFilterChange={(patch) => setFilters((f) => ({ ...f, ...patch }))}
          view={view}
          onViewChange={setView}
          tags={tags.data ?? []}
          tagsLoading={tags.isLoading}
          categories={categories.data ?? []}
          onRefresh={() => void assets.refetch()}
        />

        <div
          className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed p-6 text-center"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            void uploadFiles(Array.from(e.dataTransfer.files));
          }}
        >
          <Inbox className="size-8 text-muted-foreground" />
          <p className="text-sm font-medium">拖拽文件上传到本空间（PDF / Office / TXT / MD / 图片）</p>
          <p className="text-sm text-muted-foreground">
            上传前自动做同空间去重预检与配额校验。
          </p>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ACCEPT_TYPES}
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

        {assets.isError ? (
          <p className="text-sm text-destructive">{(assets.error as Error).message}</p>
        ) : !assets.isLoading && items.length === 0 ? (
          <EmptyState title="没有符合筛选条件的资产" className="mt-6" />
        ) : view === 'grid' ? (
          <AssetGridView items={items} thumbById={thumbById} onOpen={(id) => navigate(`/a/${id}`)} />
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

      {/* 去重确认：仍要上传 / 放弃 */}
      <Dialog
        open={dupConfirm !== null}
        onOpenChange={(open) => {
          if (!open) {
            dupConfirm?.resolve(false);
            setDupConfirm(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>发现疑似重复文件</DialogTitle>
            <DialogDescription>
              本空间已存在相同内容：{dupConfirm?.names.join('、')}。仍要上传这份副本吗？
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                dupConfirm?.resolve(false);
                setDupConfirm(null);
              }}
            >
              放弃
            </Button>
            <Button
              onClick={() => {
                dupConfirm?.resolve(true);
                setDupConfirm(null);
              }}
            >
              仍要上传
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
