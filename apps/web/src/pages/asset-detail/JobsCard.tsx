import { useState } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { DataTable } from '@loomvec/ui/components/data-table';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { JOB_PAGE_SIZE, JOB_STATUS_TONE, JOB_TYPE_LABEL } from './constants';
import type { AssetJob } from './use-asset-queries';

/** 任务记录卡：分页表 + 单步重跑。 */
export function JobsCard(props: {
  jobs: AssetJob[];
  loading: boolean;
  error?: Error | null;
  onRetry: (step: string) => void;
}) {
  const [jobPage, setJobPage] = useState(1);

  const jobColumns: ColumnDef<AssetJob, unknown>[] = [
    {
      accessorKey: 'job_type',
      header: '步骤',
      cell: ({ row }) => <span>{JOB_TYPE_LABEL[row.original.job_type] ?? row.original.job_type}</span>,
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) => (
        <StatusBadge tone={JOB_STATUS_TONE[row.original.status] ?? 'gray'}>
          {row.original.status}
        </StatusBadge>
      ),
    },
    {
      accessorKey: 'progress',
      header: '进度',
      cell: ({ row }) => <span>{Math.round(row.original.progress * 100)}%</span>,
    },
    { accessorKey: 'attempts', header: '尝试' },
    {
      id: 'duration',
      header: '耗时',
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
      header: '错误',
      cell: ({ row }) =>
        row.original.error ? (
          <span className="block max-w-48 truncate text-destructive" title={row.original.error}>
            {row.original.error}
          </span>
        ) : (
          <span>-</span>
        ),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => (
        <Button variant="outline" size="xs" onClick={() => props.onRetry(row.original.job_type)}>
          重跑此步
        </Button>
      ),
    },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">任务记录</CardTitle>
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
