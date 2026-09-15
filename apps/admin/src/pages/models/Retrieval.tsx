import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Card, CardContent } from '@loomvec/ui/components/ui/card';
import type { ReindexStatus } from '@/types';

const PAGE_SIZE = 20;

type SpaceIndexRow = ReindexStatus['spaces'][number];

/** 索引状态（/retrieval）：各空间未嵌入/多版本单元计数，索引维护决策依据（docs/04 §5.7）。 */
export function RetrievalPage() {
  const [page, setPage] = useState(1);
  const { t } = useTranslation('models');
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['admin-reindex-status'],
    queryFn: () => unwrap<ReindexStatus>(api.GET('/api/v1/admin/reindex/status')),
    refetchInterval: 15_000,
  });

  const spaces = data?.spaces ?? [];

  const columns: ColumnDef<SpaceIndexRow, unknown>[] = [
    { accessorKey: 'space_id', header: t('indexCol.spaceId') },
    { accessorKey: 'units', header: t('indexCol.units') },
    { accessorKey: 'embedded_units', header: t('indexCol.embedded') },
    {
      accessorKey: 'pending_units',
      header: t('indexCol.pending'),
      cell: ({ row }) =>
        row.original.pending_units > 0 ? (
          <StatusBadge tone="amber">{row.original.pending_units}</StatusBadge>
        ) : (
          <StatusBadge tone="green">0</StatusBadge>
        ),
    },
    {
      accessorKey: 'model_versions',
      header: t('indexCol.modelVersions'),
      cell: ({ row }) => (
        <div className="flex max-w-md flex-wrap gap-1">
          {row.original.model_versions.map((v) => (
            <StatusBadge key={v.model_version ?? 'none'} tone="purple">
              {v.model_version ?? t('notEmbedded')}：{v.units}
            </StatusBadge>
          ))}
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('indexTitle')} description={t('indexDescription')} />

      <Card>
        <CardContent>
          {/* 旧实现为客户端分页（pageSize 20），此处等价模拟 */}
          <DataTable
            columns={columns}
            data={spaces.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)}
            loading={isLoading}
            error={isError ? error : undefined}
            total={spaces.length}
            page={page}
            pageSize={PAGE_SIZE}
            onPageChange={setPage}
            getRowId={(r) => r.space_id}
            emptyTitle={t('emptyUnits')}
          />
        </CardContent>
      </Card>
    </div>
  );
}
