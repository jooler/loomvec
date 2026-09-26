import { useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { useTranslation } from 'react-i18next';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { DataTable } from '@loomvec/ui/components/data-table';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { JOB_PAGE_SIZE, JOB_STATUS_TONE } from './constants';
import type { AssetJob } from './use-asset-queries';

/** 任务记录卡：分页表 + 单步重跑（canRetry 时才露出重跑列）。 */
export function JobsCard(props: {
  jobs: AssetJob[];
  loading: boolean;
  error?: Error | null;
  /** 与后端 _require_asset_manager 对齐；虚拟 viewer / 无管理权时隐藏重跑。 */
  canRetry?: boolean;
  onRetry: (step: string) => void;
}) {
  const { t } = useTranslation('assetDetail');
  const [jobPage, setJobPage] = useState(1);
  const canRetry = props.canRetry ?? false;

  const jobColumns: ColumnDef<AssetJob, unknown>[] = [
    {
      accessorKey: 'job_type',
      header: t('jobs.step'),
      cell: ({ row }) => (
        <span>{t(`jobType.${row.original.job_type}`, { defaultValue: row.original.job_type })}</span>
      ),
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) => (
        <StatusBadge tone={JOB_STATUS_TONE[row.original.status] ?? 'gray'}>
          {row.original.status}
        </StatusBadge>
      ),
    },
    {
      accessorKey: 'progress',
      header: t('jobs.progress'),
      cell: ({ row }) => <span>{Math.round(row.original.progress * 100)}%</span>,
    },
    { accessorKey: 'attempts', header: t('jobs.attempts') },
    {
      id: 'duration',
      header: t('jobs.duration'),
      cell: ({ row }) => (
        <span>
          {row.original.started_at && row.original.finished_at
            ? `${((new Date(row.original.finished_at).getTime() - new Date(row.original.started_at).getTime()) / 1000).toFixed(1)}s`
            : '-'}
        </span>
      ),
    },
    {
      accessorKey: 'error',
      header: t('jobs.error'),
      cell: ({ row }) =>
        row.original.error ? (
          <span className="block max-w-48 truncate text-destructive" title={row.original.error}>
            {row.original.error}
          </span>
        ) : (
          <span>-</span>
        ),
    },
    ...(canRetry
      ? [
          {
            id: 'actions',
            header: t('field.actions'),
            cell: ({ row }: { row: { original: AssetJob } }) => (
              <Button
                variant="outline"
                size="xs"
                onClick={() => props.onRetry(row.original.job_type)}
              >
                {t('jobs.retryStep')}
              </Button>
            ),
          } satisfies ColumnDef<AssetJob, unknown>,
        ]
      : []),
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t('jobs.title')}</CardTitle>
      </CardHeader>
      <CardContent>
        {props.error ? (
          <p className="text-sm text-destructive">{props.error.message}</p>
        ) : (
          <DataTable
            columns={jobColumns}
            data={props.jobs.slice((jobPage - 1) * JOB_PAGE_SIZE, jobPage * JOB_PAGE_SIZE)}
            loading={props.loading}
            total={props.jobs.length}
            page={jobPage}
            pageSize={JOB_PAGE_SIZE}
            onPageChange={setJobPage}
            getRowId={(row) => row.id}
          />
        )}
      </CardContent>
    </Card>
  );
}
