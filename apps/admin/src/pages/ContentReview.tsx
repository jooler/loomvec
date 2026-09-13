import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { ArrowLeft } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
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
  return (
    <div className="max-w-[480px] space-y-0.5">
      <p className={cn('whitespace-pre-wrap break-words text-sm', !expanded && 'line-clamp-3')}>
        {content}
      </p>
      <button
        className="text-xs text-muted-foreground hover:underline"
        onClick={() => setExpanded((v) => !v)}
      >
        {expanded ? '收起' : '展开'}
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
  const [takedownOpen, setTakedownOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unitsPage, setUnitsPage] = useState(1);

  const { data, isLoading } = useQuery({
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
    { accessorKey: 'title', header: '标题', cell: ({ row }) => row.original.title ?? '-' },
    {
      accessorKey: 'unit_type',
      header: '类型',
      cell: ({ row }) => <Badge variant="outline">{row.original.unit_type}</Badge>,
    },
    {
      accessorKey: 'chunk_method',
      header: '分片来源',
      cell: ({ row }) =>
        row.original.chunk_method === 'llm' ? (
          <StatusBadge tone="blue">LLM 分片</StatusBadge>
        ) : (
          <StatusBadge tone="amber">结构化兜底</StatusBadge>
        ),
    },
    { accessorKey: 'char_count', header: '字符数' },
    { accessorKey: 'locator', header: '定位', cell: ({ row }) => row.original.locator ?? '-' },
    {
      accessorKey: 'embed_model_version',
      header: '嵌入版本',
      cell: ({ row }) =>
        row.original.embed_model_version ?? <StatusBadge tone="red">未嵌入</StatusBadge>,
    },
    {
      accessorKey: 'content',
      header: '内容预览',
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
          <ArrowLeft /> 返回审核队列
        </Button>
      </div>
      <PageHeader
        title={asset?.name ?? 'chunk 复核'}
        description={asset ? `空间：${asset.space_slug ?? '-'}` : undefined}
        actions={
          <Button variant="destructive" disabled={!canWrite} onClick={() => setTakedownOpen(true)}>
            下架资产
          </Button>
        }
      />

      <Card>
        <CardContent>
          <DescriptionList cols={2}>
            <DescriptionItem label="资产 ID">{asset?.id ?? assetId}</DescriptionItem>
            <DescriptionItem label="类型">{asset?.mime_type ?? '-'}</DescriptionItem>
            <DescriptionItem label="大小">{formatBytes(asset?.size_bytes ?? 0)}</DescriptionItem>
            <DescriptionItem label="资产状态">{asset?.status ?? '-'}</DescriptionItem>
            <DescriptionItem label="审核状态">
              {asset?.review_status ? <Badge variant="outline">{asset.review_status}</Badge> : '-'}
            </DescriptionItem>
            <DescriptionItem label="提交时间">{formatDateTime(asset?.created_at)}</DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>语义单元切片（{data?.items.length ?? 0}）</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={columns}
            data={pagedUnits}
            getRowId={(u) => u.id}
            loading={isLoading}
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
        title={`下架资产「${asset?.name ?? ''}」`}
        description="下架 = 驳回 + 软删 + 向量连带清理（含全部语义单元），操作不可逆。"
        okText="确认下架"
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
            toast.success('已下架');
            setTakedownOpen(false);
            void queryClient.invalidateQueries({ queryKey: ['admin-reviews'] });
            void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
            navigate('/reviews');
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      />
    </div>
  );
}
