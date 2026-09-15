/**
 * 资产查看覆盖层：占满 Finder 容器上层的全幅查看组件（替代右侧抽屉形态）。
 *
 * 原始文件（embedpdf/office/图片/文本）× MinerU 解析结果（Markdown 渲染）×
 * chunk 管理（可选）；上一个/下一个沿当前文件夹资产顺序导航（←/→ 键支持）。
 */
import { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, X } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { ApiClient } from '@loomvec/sdk-ts';
import { extractApiError, formatBytes } from '../../lib/format';
import { Button } from '../ui/button';
import { Spinner } from '../ui/spinner';
import { StatusBadge } from '../status-badge';
import { ReviewStatusTag } from '../review-status-tag';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../ui/tabs';
import { AssetChunksPanel } from '../asset-chunks-panel';
import { MarkdownView } from '../markdown-view';
import { OriginalFileViewer } from '../original-file-viewer';
import { STATUS_TONE } from './finder-views';

interface PreviewData {
  mode: 'pdf' | 'markdown' | 'image';
  url: string | null;
  content: string | null;
  page_count: number | null;
  original_url: string | null;
  parsed_url: string | null;
}

export interface AssetViewerOverlayProps {
  client: ApiClient;
  assetId: string | null;
  onClose: () => void;
  onPrev?: () => void;
  onNext?: () => void;
  hasPrev?: boolean;
  hasNext?: boolean;
  /** 是否渲染 chunk 管理面板（运营端 / 空间维护场景）。 */
  showChunks?: boolean;
  canManageChunks?: boolean;
  /** 媒体播放器插槽（web 的音视频消费）。 */
  renderMedia?: (assetId: string) => React.ReactNode;
}

export function AssetViewerOverlay(props: AssetViewerOverlayProps) {
  const { assetId } = props;
  const [markdown, setMarkdown] = useState<string | null>(null);
  const { t } = useTranslation();

  const detail = useQuery({
    queryKey: ['finder-asset-detail', assetId],
    enabled: !!assetId,
    queryFn: async () => {
      const { data, error } = await props.client.GET('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, t('viewer.loadAssetFailed')));
      return data;
    },
  });

  const preview = useQuery({
    queryKey: ['finder-asset-preview', assetId],
    enabled: !!assetId,
    queryFn: async () => {
      const { data, error } = await props.client.GET('/api/v1/assets/{asset_id}/preview', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, t('viewer.loadPreviewFailed')));
      return data as unknown as PreviewData;
    },
  });

  // MinerU 产物拉取：markdown 模式（content 或 presigned URL）+ PDF 的解析产物
  useEffect(() => {
    setMarkdown(null);
    const p = preview.data;
    if (!assetId || !p) return;
    if (p.mode === 'markdown') {
      if (p.content) setMarkdown(p.content);
      else if (p.url) void fetch(p.url).then((r) => r.text()).then(setMarkdown);
    } else if (p.mode === 'pdf' && p.parsed_url) {
      void fetch(p.parsed_url).then((r) => r.text()).then(setMarkdown);
    }
  }, [assetId, preview.data]);

  // Esc 关闭 / ←→ 导航（依赖收窄到具体回调，避免每渲染重挂监听）
  const onClose = props.onClose;
  const onPrev = props.onPrev;
  const onNext = props.onNext;
  const hasPrev = props.hasPrev;
  const hasNext = props.hasNext;
  useEffect(() => {
    if (!assetId) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
      )
        return;
      if (e.key === 'Escape') onClose();
      if (e.key === 'ArrowLeft' && hasPrev && onPrev) onPrev();
      if (e.key === 'ArrowRight' && hasNext && onNext) onNext();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [assetId, onClose, onPrev, onNext, hasPrev, hasNext]);

  if (!assetId) return null;

  const a = detail.data;
  const originalUrl =
    preview.data?.original_url ??
    (preview.data && (preview.data.mode === 'pdf' || preview.data.mode === 'image')
      ? preview.data.url
      : null);

  return (
    <div className="absolute inset-0 z-30 flex flex-col gap-0 rounded-lg border bg-background shadow-lg">
      {/* 头部：名称 + 元信息 + 导航/关闭 */}
      <div className="flex items-center gap-2 border-b px-4 py-2.5">
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium" title={a?.name}>
            {a?.name ?? t('viewer.defaultTitle')}
          </p>
          {a && (
            <p className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>{a.mime_type}</span>
              <span>{formatBytes(a.size_bytes)}</span>
              <StatusBadge tone={STATUS_TONE[a.status]?.tone}>
                {t(`assetStatus.${a.status}`, { defaultValue: a.status })}
              </StatusBadge>
              <ReviewStatusTag status={a.review_status} />
            </p>
          )}
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!props.hasPrev}
            onClick={props.onPrev}
            aria-label={t('viewer.prev')}
          >
            <ChevronLeft />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!props.hasNext}
            onClick={props.onNext}
            aria-label={t('viewer.next')}
          >
            <ChevronRight />
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={props.onClose} aria-label={t('viewer.close')}>
            <X />
          </Button>
        </div>
      </div>

      {/* 媒体播放器插槽（web 音视频消费），固定于 Tabs 之上不参与滚动 */}
      {a && props.renderMedia && (
        <div className="shrink-0 px-4 pt-3">{props.renderMedia(a.id)}</div>
      )}

      {/* 主体：Tabs 接管剩余高度，各内容区自行滚动（原始文件由查看器内部控制滚动） */}
      <div className="flex min-h-0 flex-1 flex-col px-4 py-3">
        {detail.isError ? (
          <p className="text-sm text-destructive">{(detail.error as Error).message}</p>
        ) : !a ? (
          <div className="grid flex-1 place-items-center">
            <Spinner className="size-5 text-muted-foreground" />
          </div>
        ) : preview.isError ? (
          <p className="text-sm text-destructive">{(preview.error as Error).message}</p>
        ) : !preview.data ? (
          <div className="grid flex-1 place-items-center">
            <Spinner className="size-5 text-muted-foreground" />
          </div>
        ) : (
          <Tabs
            defaultValue={
              preview.data.mode === 'markdown' ? 'parsed' : 'original'
            }
            className="flex min-h-0 flex-1 flex-col"
          >
            <TabsList className="shrink-0">
              <TabsTrigger value="original">{t('viewer.tabOriginal')}</TabsTrigger>
              <TabsTrigger value="parsed">{t('viewer.tabParsed')}</TabsTrigger>
              {props.showChunks && <TabsTrigger value="chunks">{t('viewer.tabChunks')}</TabsTrigger>}
            </TabsList>
            <TabsContent value="original" className="mt-3 min-h-0 flex-1">
              {/* 查看器占满剩余高度，滚动条由查看器内部控制（embedpdf/docx/文本均 h-full） */}
              <OriginalFileViewer url={originalUrl} mime_type={a.mime_type} ext={a.ext} />
            </TabsContent>
            <TabsContent value="parsed" className="mt-3 min-h-0 flex-1">
              {markdown !== null ? (
                <MarkdownView
                  content={markdown}
                  className="h-full overflow-auto rounded-md border p-4"
                />
              ) : preview.data.parsed_url || preview.data.mode === 'markdown' ? (
                <div className="grid h-full place-items-center">
                  <Spinner className="size-5 text-muted-foreground" />
                </div>
              ) : (
                <p className="grid h-full place-items-center text-sm text-muted-foreground">
                  {t('viewer.noParseHint')}
                </p>
              )}
            </TabsContent>
            {props.showChunks && (
              <TabsContent value="chunks" className="mt-3 min-h-0 flex-1 overflow-y-auto">
                <AssetChunksPanel
                  client={props.client}
                  assetId={a.id}
                  spaceId={a.space_id ?? undefined}
                  canManage={props.canManageChunks ?? false}
                />
              </TabsContent>
            )}
          </Tabs>
        )}
      </div>
    </div>
  );
}
