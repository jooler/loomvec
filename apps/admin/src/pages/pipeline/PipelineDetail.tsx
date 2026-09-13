import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { ArrowLeft } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import type { JobDetail, JobRow } from '@/types';
import { formatDateTime, formatSeconds } from '@/utils';

/** 任务详情：输入资产、各阶段耗时分解、错误信息、重试（docs/04 §5.6）。 */
export function PipelineDetailPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);

  const jobKey = ['admin-pipeline-job', jobId];
  const { data: job, isLoading } = useQuery({
    queryKey: jobKey,
    enabled: !!jobId,
    queryFn: () =>
      unwrap<JobDetail>(
        api.GET('/api/v1/admin/pipeline/jobs/{job_id}', {
          params: { path: { job_id: jobId! } },
        }),
      ),
  });

  const retry = async (step?: string) => {
    setBusy(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/pipeline/jobs/{job_id}/retry', {
          params: { path: { job_id: jobId! } },
          body: { step: step ?? null },
        }),
      );
      toast.success('重试已受理');
      void queryClient.invalidateQueries({ queryKey: jobKey });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const stepsColumns: ColumnDef<JobRow, unknown>[] = [
    {
      accessorKey: 'job_type',
      header: '阶段',
      cell: ({ row }) => <Badge variant="outline">{row.original.job_type}</Badge>,
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) => <Badge variant="outline">{row.original.status}</Badge>,
    },
    {
      id: 'duration',
      header: '耗时',
      cell: ({ row }) =>
        row.original.started_at && row.original.finished_at
          ? formatSeconds(
              (new Date(row.original.finished_at).getTime() -
                new Date(row.original.started_at).getTime()) /
                1000,
            )
          : '-',
    },
    { accessorKey: 'attempts', header: '尝试' },
    {
      accessorKey: 'started_at',
      header: '开始',
      cell: ({ row }) => formatDateTime(row.original.started_at),
    },
    {
      accessorKey: 'finished_at',
      header: '结束',
      cell: ({ row }) => formatDateTime(row.original.finished_at),
    },
    {
      accessorKey: 'error',
      header: '错误',
      cell: ({ row }) =>
        row.original.error ? (
          <span className="block max-w-80 truncate text-destructive" title={row.original.error}>
            {row.original.error}
          </span>
        ) : (
          '-'
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/pipeline')}>
        <ArrowLeft /> 返回
      </Button>

      <PageHeader
        title={`任务 ${job?.id.slice(0, 8) ?? ''}…`}
        description={job?.job_type}
        actions={
          <ConfirmAction
            trigger={
              <Button disabled={!canWrite || !job || job.status !== 'failed'}>重试任务</Button>
            }
            title="确认重试该任务？"
            loading={busy}
            onConfirm={() => void retry()}
          />
        }
      />

      <Card>
        <CardContent className="space-y-4">
          {isLoading ? (
            <div className="grid place-items-center py-10">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : (
            <>
              <DescriptionList cols={2}>
                <DescriptionItem label="任务 ID">{job?.id}</DescriptionItem>
                <DescriptionItem label="状态">
                  <StatusBadge
                    tone={
                      job?.status === 'failed'
                        ? 'red'
                        : job?.status === 'succeeded'
                          ? 'green'
                          : job?.status === 'running'
                            ? 'blue'
                            : 'gray'
                    }
                  >
                    {job?.status}
                  </StatusBadge>
                </DescriptionItem>
                <DescriptionItem label="资产">{job?.asset_name ?? '-'}</DescriptionItem>
                <DescriptionItem label="资产 ID">{job?.asset_id ?? '-'}</DescriptionItem>
                <DescriptionItem label="进度">
                  {((job?.progress ?? 0) * 100).toFixed(0)}%
                </DescriptionItem>
                <DescriptionItem label="尝试次数">{job?.attempts ?? 0}</DescriptionItem>
                <DescriptionItem label="开始时间">{formatDateTime(job?.started_at)}</DescriptionItem>
                <DescriptionItem label="结束时间">
                  {formatDateTime(job?.finished_at)}
                </DescriptionItem>
              </DescriptionList>
              {job?.error && (
                <p className="whitespace-pre-wrap text-sm text-destructive">{job.error}</p>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>同资产各阶段（耗时分解）</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable columns={stepsColumns} data={job?.asset_steps} loading={isLoading} />
        </CardContent>
      </Card>
    </div>
  );
}
