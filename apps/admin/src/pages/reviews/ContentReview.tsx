import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { ArrowLeft } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { cn } from 'cn';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { DataTable } from '@loomvec/ui/components/data-table';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { formatBytes, formatDateTime } from '@/utils';
import type { ReviewAsset, UnitRow } from '@/types';

const UNITS_PAGE_SIZE = 20;

/** 内容预览：对应旧 Typography.Paragraph ellipsis rows=3 expandable。 */
function ContentPreview({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false);
  const { t } = useTranslation('reviews');
  return (
    <div className="max-w-[480px] space-y-0.5">
      <p className={cn('whitespace-pre-wrap break-words text-sm', !expanded && 'line-clamp-3')}>
        {content}
      </p>
      <button
        className="text-xs text-muted-foreground hover:underline"
        onClick={() => setExpanded((v) => !v)}
      >
        {expanded ? t('collapse') : t('expand')}
      </button>
    </div>
  );
}

/** chunk 复核页（/content/:assetId）：语义单元切片结果预览 + 资产下架（docs/04 §5.5）。 */
export function ContentReviewPage() {
  const { assetId } = useParams<{ assetId: string }>();
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('reviews');
  const [takedownOpen, setTakedownOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unitsPage, setUnitsPage] = useState(1);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['admin-asset-units', assetId],
    enabled: !!assetId,
    queryFn: () =>
      unwrap<{ asset: ReviewAsset; items: UnitRow[] }>(
        api.GET('/api/v1/admin/assets/{asset_id}/units', {
          params: { path: { asset_id: assetId! }, query: { limit: 500 } },
        }),
      ),
  });

  const asset = data?.asset;
  const units = data?.items ?? [];
  const pagedUnits = units.slice((unitsPage - 1) * UNITS_PAGE_SIZE, unitsPage * UNITS_PAGE_SIZE);

  const columns: ColumnDef<UnitRow, unknown>[] = [
    { accessorKey: 'order_index', header: '#' },
    {
      accessorKey: 'title',
      header: t('unitCol.title'),
      cell: ({ row }) => row.original.title ?? '-',
    },
    {
      accessorKey: 'unit_type',
      header: t('unitCol.type'),
      cell: ({ row }) => <Badge variant="outline">{row.original.unit_type}</Badge>,
    },
    {
      accessorKey: 'chunk_method',
      header: t('unitCol.chunkSource'),
      cell: ({ row }) =>
        row.original.chunk_method === 'llm' ? (
          <StatusBadge tone="blue">{t('chunks.sourceLlmMarkers')}</StatusBadge>
        ) : (
          <StatusBadge tone="amber">{t('chunks.sourceStructuralFallback')}</StatusBadge>
        ),
    },
    { accessorKey: 'char_count', header: t('unitCol.charCount') },
    {
      accessorKey: 'locator',
      header: t('unitCol.locator'),
      cell: ({ row }) => row.original.locator ?? '-',
    },
    {
      accessorKey: 'embed_model_version',
      header: t('unitCol.embedVersion'),
      cell: ({ row }) =>
        row.original.embed_model_version ?? (
          <StatusBadge tone="red">{t('notEmbedded')}</StatusBadge>
        ),
    },
    {
      accessorKey: 'content',
      header: t('unitCol.contentPreview'),
      cell: ({ row }) => <ContentPreview content={row.original.content} />,
    },
  ];

  return (
    <div className="space-y-4">
      <div>
        <Button
          variant="ghost"
          size="sm"
          className="-ml-2 text-muted-foreground"
          onClick={() => navigate('/reviews')}
        >
          <ArrowLeft /> {t('backToQueue')}
        </Button>
      </div>
      <PageHeader
        title={asset?.name ?? t('detailTitle')}
        description={asset ? t('spacePrefix', { slug: asset.space_slug ?? '-' }) : undefined}
        actions={
          <Button variant="destructive" disabled={!canWrite} onClick={() => setTakedownOpen(true)}>
            {t('takedownAsset')}
          </Button>
        }
      />

      <Card>
        <CardContent>
          <DescriptionList cols={2}>
            <DescriptionItem label={t('detailLabel.assetId')}>
              {asset?.id ?? assetId}
            </DescriptionItem>
            <DescriptionItem label={t('col.type')}>{asset?.mime_type ?? '-'}</DescriptionItem>
            <DescriptionItem label={t('col.size')}>
              {formatBytes(asset?.size_bytes ?? 0)}
            </DescriptionItem>
            <DescriptionItem label={t('col.assetStatus')}>{asset?.status ?? '-'}</DescriptionItem>
            <DescriptionItem label={t('detailLabel.reviewStatus')}>
              {asset?.review_status ? <Badge variant="outline">{asset.review_status}</Badge> : '-'}
            </DescriptionItem>
            <DescriptionItem label={t('col.submittedAt')}>
              {formatDateTime(asset?.created_at)}
            </DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('unitsTitle', { count: data?.items.length ?? 0 })}</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={columns}
            data={pagedUnits}
            getRowId={(u) => u.id}
            loading={isLoading}
            error={isError ? error : undefined}
            total={units.length}
            page={unitsPage}
            pageSize={UNITS_PAGE_SIZE}
            onPageChange={setUnitsPage}
          />
        </CardContent>
      </Card>

      {/* 下架（危险操作：连带向量清理） */}
      <ReasonModal
        open={takedownOpen}
        title={t('takedownTitle', { name: asset?.name ?? '' })}
        description={t('takedownDescriptionWithUnits')}
        okText={t('confirmTakedown')}
        danger
        confirmLoading={busy}
        onCancel={() => setTakedownOpen(false)}
        onOk={async (reason) => {
          setBusy(true);
          try {
            await unwrap(
              api.POST('/api/v1/admin/assets/{asset_id}/takedown', {
                params: { path: { asset_id: assetId! } },
                body: { reason },
              }),
            );
            toast.success(t('takedownToastShort'));
            setTakedownOpen(false);
            void queryClient.invalidateQueries({ queryKey: ['admin-reviews'] });
            void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
            navigate('/reviews');
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      />
    </div>
  );
}
