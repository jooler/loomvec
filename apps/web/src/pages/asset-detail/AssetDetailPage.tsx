import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import { RefreshCw, Trash2 } from 'lucide-react';
import { api } from '@loomvec/sdk-ts';
import { useSpaceCategories } from '@/hooks';
import { extractApiError } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { MediaPlayer } from '@/components/media-player';
import { ReviewStatusTag } from '@/components/review-status-tag';
import { JOB_TYPE_LABEL } from './constants';
import { StepTimeline } from './StepTimeline';
import { JobsCard } from './JobsCard';
import { ImagePanel } from './ImagePanel';
import { AssetEditor } from './AssetEditor';
import { useAssetDetail, useAssetJobs, useAssetPreview, type AssetJob } from './use-asset-queries';

/** 从 URL 读取数字参数，非法值（缺失/非数字）返回 undefined。 */
function numberParam(value: string | null): number | undefined {
  if (value === null || value.trim() === '') return undefined;
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : undefined;
}

/**
 * P1 沿用：处理任务时间线 + 单步重试 + 预览页码跳转。
 * P2 增强：资产编辑器（名称/标签/分类/按空间 schema 动态元数据）+ 审核状态 + 图片体验
 * （renditions 缩略图 / 原图预览 / EXIF 信息面板）。
 */
export function AssetDetailPage() {
  const { assetId } = useParams<{ assetId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // P3-WEB-02/03：检索命中视频/音频单元时经 ?t=<秒> seek 到时间点
  const seekTo = numberParam(searchParams.get('t'));
  // PDF 预览页码：以 URL 为单一来源（检索命中 ?page=N 跳转时可同步更新）
  const page = numberParam(searchParams.get('page'));
  const [markdown, setMarkdown] = useState<string | null>(null);

  const asset = useAssetDetail(assetId);
  const jobs = useAssetJobs(assetId);
  const preview = useAssetPreview(assetId);

  const spaceId = asset.data?.space_id ?? undefined;

  const space = useQuery({
    queryKey: ['space', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载空间失败'));
      return data;
    },
  });

  const categories = useSpaceCategories(spaceId);

  const metadataFields = useQuery({
    queryKey: ['space-metadata-fields', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/metadata-fields', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载元数据 schema 失败'));
      return data;
    },
  });

  useEffect(() => {
    if (preview.data?.mode === 'markdown') {
      if (preview.data.content) setMarkdown(preview.data.content);
      else if (preview.data.url) void fetch(preview.data.url).then((r) => r.text()).then(setMarkdown);
    }
  }, [preview.data]);

  const retry = useMutation({
    mutationFn: async (step?: string) => {
      const { error } = await api.POST('/api/v1/assets/{asset_id}/retry', {
        params: { path: { asset_id: assetId! } },
        body: { step },
      });
      if (error) throw new Error(extractApiError(error, '重试失败'));
    },
    onSuccess: () => {
      toast.success('已重新入队');
      void asset.refetch();
      void jobs.refetch();
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, '删除失败'));
    },
    onSuccess: () => {
      toast.success('已删除（含向量清理）');
      void queryClient.invalidateQueries({ queryKey: ['assets'] });
      navigate(spaceId ? `/s/${spaceId}/assets` : '/assets');
    },
    onError: (e) => toast.error(e.message),
  });

  if (asset.isLoading)
    return (
      <Card>
        <CardContent className="space-y-3">
          <Skeleton className="h-6 w-1/3" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
          <Skeleton className="h-20 w-full" />
        </CardContent>
      </Card>
    );
  if (asset.isError)
    return (
      <Card>
        <CardContent>
          <p className="text-sm text-destructive">{(asset.error as Error).message}</p>
        </CardContent>
      </Card>
    );

  const a = asset.data;
  if (!a)
    return (
      <Card>
        <CardContent className="text-sm text-muted-foreground">资产不存在</CardContent>
      </Card>
    );

  const isMedia = ['video/', 'audio/'].some((p) => (a.mime_type ?? '').startsWith(p));
  const myRole = space.data?.my_role;
  const canEdit = myRole !== 'viewer'; // 未知（空间加载失败）时放开，由后端兜底

  // 任务列表按 created_at 倒序：每种 job_type 的首个即最新一次
  const latestByStep = new Map<string, AssetJob>();
  for (const j of jobs.data ?? []) {
    if (!latestByStep.has(j.job_type)) latestByStep.set(j.job_type, j);
  }
  const stepStatus = (step: string): 'wait' | 'finish' | 'error' | 'process' => {
    const j = latestByStep.get(step);
    if (!j) return 'wait';
    if (j.status === 'succeeded') return 'finish';
    if (j.status === 'failed') return 'error';
    return 'process';
  };
  const failedStep = [...latestByStep.values()].find((j) => j.status === 'failed')?.job_type;

  const mainImageUrl = preview.data?.mode === 'image' ? (preview.data.url ?? null) : null;

  const setPage = (p: number) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('page', String(p));
        return next;
      },
      { replace: true },
    );
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base break-all">{a.name}</CardTitle>
          <CardAction>
            <div className="flex flex-wrap items-center gap-2">
              {a.status === 'failed' && (
                <Button size="sm" onClick={() => retry.mutate(failedStep)}>
                  从失败步骤重跑{failedStep ? `（${JOB_TYPE_LABEL[failedStep]}）` : ''}
                </Button>
              )}
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  void asset.refetch();
                  void jobs.refetch();
                }}
              >
                <RefreshCw /> 刷新
              </Button>
              <ConfirmAction
                trigger={
                  <Button variant="destructive" size="sm" disabled={remove.isPending}>
                    <Trash2 /> 删除
                  </Button>
                }
                title="删除资产？"
                description="将级联清理语义单元与向量，不可恢复。"
                confirmText="删除"
                danger
                onConfirm={() => remove.mutate()}
              />
            </div>
          </CardAction>
        </CardHeader>
        <CardContent className="space-y-4">
          <DescriptionList cols={3}>
            <DescriptionItem label="类型">{a.mime_type}</DescriptionItem>
            <DescriptionItem label="大小">{(a.size_bytes / 1024).toFixed(1)} KB</DescriptionItem>
            <DescriptionItem label="页数">{a.page_count ?? '-'}</DescriptionItem>
            <DescriptionItem label="状态">
              <StatusBadge tone={a.status === 'ready' ? 'green' : a.status === 'failed' ? 'red' : 'blue'}>
                {a.status}
              </StatusBadge>
            </DescriptionItem>
            <DescriptionItem label="审核">
              <span className="flex flex-wrap items-center gap-2">
                <ReviewStatusTag status={a.review_status} />
                {a.review_reason && <span className="text-xs text-amber-600">{a.review_reason}</span>}
              </span>
            </DescriptionItem>
            <DescriptionItem label="分片方式">{a.chunk_method ?? '-'}</DescriptionItem>
            <DescriptionItem label="创建时间">{new Date(a.created_at).toLocaleString()}</DescriptionItem>
            <DescriptionItem label="标签">
              {a.tags.length > 0 ? (
                <span className="flex flex-wrap gap-1">
                  {a.tags.map((t) => (
                    <StatusBadge key={t.id} tone="blue">
                      {t.name}
                    </StatusBadge>
                  ))}
                </span>
              ) : null}
            </DescriptionItem>
            <DescriptionItem label="上传者">{a.created_by ?? '-'}</DescriptionItem>
          </DescriptionList>
          {a.status_reason && <p className="text-sm text-destructive">失败原因：{a.status_reason}</p>}
          <StepTimeline
            steps={['parse', 'chunk', 'embed', 'index'].map((step) => ({
              key: step,
              label: JOB_TYPE_LABEL[step],
              state: stepStatus(step),
              description: latestByStep.get(step)?.error?.slice(0, 80),
            }))}
          />
        </CardContent>
      </Card>

      {/* 图片体验：缩略图网格 / 原图查看 / EXIF 面板 */}
      {a.is_image && <ImagePanel asset={a} mainImageUrl={mainImageUrl} />}

      {/* 编辑器（editor+ 可用；viewer 只读） */}
      <AssetEditor
        asset={a}
        categories={categories.data ?? []}
        metadataFields={metadataFields.data ?? []}
        canEdit={canEdit}
      />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">预览定位</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {isMedia && <MediaPlayer assetId={assetId!} seekTo={seekTo} />}
          {preview.data?.mode === 'pdf' && (
            <>
              {preview.data.page_count ? (
                <div className="flex flex-wrap gap-1.5">
                  {[...Array(preview.data.page_count)].map((_, i) => (
                    <Button
                      key={i}
                      size="sm"
                      variant={page === i + 1 ? 'default' : 'outline'}
                      onClick={() => setPage(i + 1)}
                    >
                      第 {i + 1} 页
                    </Button>
                  ))}
                </div>
              ) : null}
              <iframe
                key={page}
                src={`${preview.data.url}${page ? `#page=${page}` : ''}`}
                className="h-[640px] w-full rounded-md border"
                title="PDF 预览"
              />
            </>
          )}
          {preview.data?.mode === 'markdown' && markdown !== null && (
            <pre className="max-h-[640px] overflow-auto whitespace-pre-wrap text-sm">
              {markdown}
            </pre>
          )}
          {preview.isError ? (
            <p className="text-sm text-destructive">{(preview.error as Error).message}</p>
          ) : (
            !preview.data && <p className="text-sm text-muted-foreground">解析完成后可预览。</p>
          )}
        </CardContent>
      </Card>

      <JobsCard
        jobs={jobs.data ?? []}
        loading={jobs.isLoading}
        error={jobs.isError ? (jobs.error as Error) : null}
        onRetry={(step) => retry.mutate(step)}
      />
    </div>
  );
}
