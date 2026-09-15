import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { ArrowLeft } from 'lucide-react';
import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
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
import { jobStatusTone } from '@/constants';
import { formatDateTime, formatSeconds } from '@/utils';

/** 任务详情：输入资产、各阶段耗时分解、错误信息、重试（docs/04 §5.6）。 */
export function PipelineDetailPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('pipeline');
  const [busy, setBusy] = useState(false);

  const jobKey = ['admin-pipeline-job', jobId];
  const { data: job, isLoading, isError, error } = useQuery({
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
      toast.success(t('retryAccepted'));
      void queryClient.invalidateQueries({ queryKey: jobKey });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  const stepsColumns: ColumnDef<JobRow, unknown>[] = [
    {
      accessorKey: 'job_type',
      header: t('col.stage'),
      cell: ({ row }) => <Badge variant="outline">{row.original.job_type}</Badge>,
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) => <Badge variant="outline">{row.original.status}</Badge>,
    },
    {
      id: 'duration',
      header: t('col.duration'),
      cell: ({ row }) =>
        row.original.started_at && row.original.finished_at
          ? formatSeconds(
              (new Date(row.original.finished_at).getTime() -
                new Date(row.original.started_at).getTime()) /
                1000,
            )
          : '-',
    },
    { accessorKey: 'attempts', header: t('col.attempts') },
    {
      accessorKey: 'started_at',
      header: t('col.startedAt'),
      cell: ({ row }) => formatDateTime(row.original.started_at),
    },
    {
      accessorKey: 'finished_at',
      header: t('col.finishedAt'),
      cell: ({ row }) => formatDateTime(row.original.finished_at),
    },
    {
      accessorKey: 'error',
      header: t('col.error'),
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
        <ArrowLeft /> {t('action.back')}
      </Button>

      <PageHeader
        title={t('taskTitle', { id: job?.id.slice(0, 8) ?? '' })}
        description={job?.job_type}
        actions={
          <ConfirmAction
            trigger={
              <Button disabled={!canWrite || !job || job.status !== 'failed'}>{t('retryTask')}</Button>
            }
            title={t('retryConfirmTitle')}
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
                <DescriptionItem label={t('col.taskId')}>{job?.id}</DescriptionItem>
                <DescriptionItem label={t('field.status')}>
                  {job ? (
                    (() => {
                      const tone = jobStatusTone(job.status);
                      return (
                        <StatusBadge tone={tone}>
                          {t(`jobStatus.${job.status}`, { defaultValue: job.status })}
                        </StatusBadge>
                      );
                    })()
                  ) : (
                    '-'
                  )}
                </DescriptionItem>
                <DescriptionItem label={t('col.asset')}>{job?.asset_name ?? '-'}</DescriptionItem>
                <DescriptionItem label={t('detailLabel.assetId')}>{job?.asset_id ?? '-'}</DescriptionItem>
                <DescriptionItem label={t('col.progress')}>
                  {((job?.progress ?? 0) * 100).toFixed(0)}%
                </DescriptionItem>
                <DescriptionItem label={t('detailLabel.attemptCount')}>{job?.attempts ?? 0}</DescriptionItem>
                <DescriptionItem label={t('detailLabel.startedAt')}>{formatDateTime(job?.started_at)}</DescriptionItem>
                <DescriptionItem label={t('detailLabel.finishedAt')}>
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
          <CardTitle>{t('stepsTitle')}</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={stepsColumns}
            data={job?.asset_steps}
            loading={isLoading}
            error={isError ? error : undefined}
          />
        </CardContent>
      </Card>
    </div>
  );
}
