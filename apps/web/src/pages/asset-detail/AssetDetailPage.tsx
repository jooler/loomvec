import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams, useSearchParams } from 'react-router';
import { toast } from 'sonner';
import { RefreshCw, Trash2 } from 'lucide-react';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { useMe, useSpaceCategories } from '@/hooks';
import { extractApiError } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@loomvec/ui/components/ui/tabs';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { AssetChunksPanel } from '@loomvec/ui/components/asset-chunks-panel';
import { MarkdownView } from '@loomvec/ui/components/markdown-view';
import { OriginalFileViewer } from '@loomvec/ui/components/original-file-viewer';
import { MediaPlayer } from '@/components/media-player';
import { ReviewStatusTag } from '@/components/review-status-tag';
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
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('assetDetail');

  // P3-WEB-02/03：检索命中视频/音频单元时经 ?t=<秒> seek 到时间点
  const seekTo = numberParam(searchParams.get('t'));
  // PDF 预览页码：以 URL 为单一来源（检索命中 ?page=N 跳转时可同步更新）
  const page = numberParam(searchParams.get('page'));
  const [markdown, setMarkdown] = useState<string | null>(null);

  const asset = useAssetDetail(assetId);
  const jobs = useAssetJobs(assetId);
  const preview = useAssetPreview(assetId);
  const me = useMe();

  const spaceId = asset.data?.space_id ?? undefined;

  const space = useQuery({
    queryKey: ['space', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, t('loadSpaceFailed')));
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
      if (error) throw new Error(extractApiError(error, t('loadMetadataFailed')));
      return data;
    },
  });

  // MinerU 产物拉取：markdown 模式（content 或 presigned URL）+ PDF 的解析产物；
  // cancelled 标志防跨资产竞态（旧请求晚返回覆盖新资产内容），失败置 null 给出占位
  useEffect(() => {
    const p = preview.data;
    if (!p) return;
    let cancelled = false;
    const load = (u: string) =>
      fetch(u)
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.text();
        })
        .then((text) => {
          if (!cancelled) setMarkdown(text);
        })
        .catch(() => {
          if (!cancelled) setMarkdown(null);
        });
    if (p.mode === 'markdown') {
      if (p.content) setMarkdown(p.content);
      else if (p.url) void load(p.url);
    } else if (p.mode === 'pdf' && p.parsed_url) {
      void load(p.parsed_url);
    }
    return () => {
      cancelled = true;
    };
  }, [preview.data, assetId]);

  const retry = useMutation({
    mutationFn: async (step?: string) => {
      const { error } = await api.POST('/api/v1/assets/{asset_id}/retry', {
        params: { path: { asset_id: assetId! } },
        body: { step },
      });
      if (error) throw new Error(extractApiError(error, t('finderData.retryFailed')));
    },
    onSuccess: () => {
      toast.success(t('finderData.requeued'));
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
      if (error) throw new Error(extractApiError(error, t('finderData.deleteFailed')));
    },
    onSuccess: () => {
      toast.success(t('deletedCleaned'));
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
        <CardContent className="text-sm text-muted-foreground">{t('notFound')}</CardContent>
      </Card>
    );

  const isMedia = ['video/', 'audio/'].some((p) => (a.mime_type ?? '').startsWith(p));
  const myRole = space.data?.my_role;
  const canEdit = myRole !== 'viewer'; // 未知（空间加载失败）时放开，由后端兜底
  // chunk 管理边界与后端 can_manage_asset 一致：owner 全部；editor 仅本人上传
  // （created_by 为空 = API Key 摄取，人类 editor 无管理权，不放入口避免点击后 403）
  const canManageChunks =
    myRole === 'owner' ||
    (myRole === 'editor' &&
      a.created_by != null &&
      a.created_by === me.data?.user_id);

  // 原始文件入口：解析类资产由 preview.original_url 提供（与 MinerU 结果并存）
  const originalUrl =
    preview.data?.original_url ??
    (preview.data && (preview.data.mode === 'pdf' || preview.data.mode === 'image')
      ? preview.data.url
      : null);

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

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base break-all">{a.name}</CardTitle>
          <CardAction>
            <div className="flex flex-wrap items-center gap-2">
              {a.status === 'failed' && (
                <Button size="sm" onClick={() => retry.mutate(failedStep)}>
                  {t('rerunFromFailed')}
                  {failedStep ? `（${t(`jobType.${failedStep}`, { defaultValue: failedStep })}）` : ''}
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
                <RefreshCw /> {t('action.refresh')}
              </Button>
              <ConfirmAction
                trigger={
                  <Button variant="destructive" size="sm" disabled={remove.isPending}>
                    <Trash2 /> {t('action.delete')}
                  </Button>
                }
                title={t('deleteTitle')}
                description={t('deleteDesc')}
                confirmText={t('action.delete')}
                danger
                onConfirm={() => remove.mutate()}
              />
            </div>
          </CardAction>
        </CardHeader>
        <CardContent className="space-y-4">
          <DescriptionList cols={3}>
            <DescriptionItem label={t('typeLabel')}>{a.mime_type}</DescriptionItem>
            <DescriptionItem label={t('sizeLabel')}>{(a.size_bytes / 1024).toFixed(1)} KB</DescriptionItem>
            <DescriptionItem label={t('pagesLabel')}>{a.page_count ?? '-'}</DescriptionItem>
            <DescriptionItem label={t('field.status')}>
              <StatusBadge tone={a.status === 'ready' ? 'green' : a.status === 'failed' ? 'red' : 'blue'}>
                {a.status}
              </StatusBadge>
            </DescriptionItem>
            <DescriptionItem label={t('reviewLabel')}>
              <span className="flex flex-wrap items-center gap-2">
                <ReviewStatusTag status={a.review_status} />
                {a.review_reason && <span className="text-xs text-amber-600">{a.review_reason}</span>}
              </span>
            </DescriptionItem>
            <DescriptionItem label={t('chunkMethodLabel')}>{a.chunk_method ?? '-'}</DescriptionItem>
            <DescriptionItem label={t('field.createdAt')}>{new Date(a.created_at).toLocaleString()}</DescriptionItem>
            <DescriptionItem label={t('tagsLabel')}>
              {a.tags.length > 0 ? (
                <span className="flex flex-wrap gap-1">
                  {a.tags.map((tag) => (
                    <StatusBadge key={tag.id} tone="blue">
                      {tag.name}
                    </StatusBadge>
                  ))}
                </span>
              ) : null}
            </DescriptionItem>
            <DescriptionItem label={t('uploaderLabel')}>{a.created_by ?? '-'}</DescriptionItem>
          </DescriptionList>
          {a.status_reason && (
            <p className="text-sm text-destructive">
              {t('failReasonLabel')}
              {a.status_reason}
            </p>
          )}
          <StepTimeline
            steps={['parse', 'chunk', 'embed', 'index'].map((step) => ({
              key: step,
              label: t(`jobType.${step}`, { defaultValue: step }),
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

      {/* 内容查看：原始文件（embedpdf/office/图片/文本）× MinerU 解析结果（Markdown 渲染） */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t('contentTitle')}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {isMedia && <MediaPlayer assetId={assetId!} seekTo={seekTo} />}
          {preview.isError ? (
            <p className="text-sm text-destructive">{(preview.error as Error).message}</p>
          ) : !preview.data ? (
            <p className="text-sm text-muted-foreground">{t('previewPending')}</p>
          ) : (
            <Tabs defaultValue={preview.data.mode === 'markdown' ? 'parsed' : 'original'}>
              <TabsList>
                <TabsTrigger value="original">{t('viewer.tabOriginal')}</TabsTrigger>
                <TabsTrigger value="parsed">{t('viewer.tabParsed')}</TabsTrigger>
              </TabsList>
              <TabsContent value="original" className="mt-3">
                <OriginalFileViewer
                  url={originalUrl}
                  mime_type={a.mime_type}
                  ext={a.ext}
                  page={page}
                  className="h-[640px]"
                />
              </TabsContent>
              <TabsContent value="parsed" className="mt-3">
                {markdown !== null ? (
                  <MarkdownView
                    content={markdown}
                    className="max-h-[640px] overflow-auto rounded-md border p-4"
                  />
                ) : preview.data.parsed_url || preview.data.mode === 'markdown' ? (
                  <div className="grid place-items-center py-10">
                    <Spinner className="size-5 text-muted-foreground" />
                  </div>
                ) : (
                  <p className="py-6 text-center text-sm text-muted-foreground">{t('noParseHint')}</p>
                )}
              </TabsContent>
            </Tabs>
          )}
        </CardContent>
      </Card>

      {/* chunk 管理：查看 / 搜索 / 新增 / 编辑 / 批删 / 检索测试（RAGFlow 式） */}
      <AssetChunksPanel
        client={api}
        assetId={assetId!}
        spaceId={spaceId}
        canManage={canManageChunks}
      />

      <JobsCard
        jobs={jobs.data ?? []}
        loading={jobs.isLoading}
        error={jobs.isError ? (jobs.error as Error) : null}
        onRetry={(step) => retry.mutate(step)}
      />
    </div>
  );
}
