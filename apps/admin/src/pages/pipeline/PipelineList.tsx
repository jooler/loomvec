import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Input } from '@loomvec/ui/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import type { FailureItem, JobRow, PagedResp } from '@/types';
import { JOB_STATUS_TONE, jobStatusTone } from '@/constants';
import { formatDateTime } from '@/utils';

const PAGE_SIZE = 20;

/** 筛选/步骤 Select 的「不过滤/默认」哨兵值（shadcn Select 无 allowClear，语义等价映射）。 */
const ALL = '__all__';
const DEFAULT_STEP = '__default__';

/** 管线监控：任务看板（状态/阶段/资产类型筛选）+ 失败原因聚合 + 单任务重试（docs/04 §5.6）。 */
export function PipelineListPage() {
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('pipeline');

  const [status, setStatus] = useState<string | undefined>();
  const [jobType, setJobType] = useState<string | undefined>();
  const [mimeType, setMimeType] = useState<string | undefined>();
  // 草稿态：MIME 输入按提交生效（Enter/搜索按钮），避免每键触发一次列表请求
  const [mimeDraft, setMimeDraft] = useState('');
  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [retryTarget, setRetryTarget] = useState<JobRow | null>(null);
  const [retryStep, setRetryStep] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-pipeline-jobs', status, jobType, mimeType, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<JobRow>>(
        api.GET('/api/v1/admin/pipeline/jobs', {
          params: {
            query: {
              status: status || undefined,
              job_type: jobType || undefined,
              mime_type: mimeType || undefined,
              limit,
              offset,
            },
          },
        }),
      ),
  });

  /** 失败原因聚合 Top N（按错误前缀聚类）。 */
  const { data: failures } = useQuery({
    queryKey: ['admin-pipeline-failures'],
    queryFn: () =>
      unwrap<{ items: FailureItem[] }>(
        api.GET('/api/v1/admin/pipeline/failures', { params: { query: { limit: 10 } } }),
      ),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['admin-pipeline-jobs'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-pipeline-failures'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
  };

  const retry = async (step?: string) => {
    if (!retryTarget) return;
    setBusy(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/pipeline/jobs/{job_id}/retry', {
          params: { path: { job_id: retryTarget.id } },
          body: { step: step ?? null },
        }),
      );
      toast.success(t('retryAccepted'));
      setRetryTarget(null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<JobRow, unknown>[] = [
    {
      accessorKey: 'id',
      header: t('col.taskId'),
      cell: ({ row }) => (
        <button
          className="text-sm font-medium hover:underline"
          onClick={() => navigate(`/pipeline/${row.original.id}`)}
        >
          {row.original.id.slice(0, 8)}…
        </button>
      ),
    },
    {
      accessorKey: 'asset_name',
      header: t('col.asset'),
      cell: ({ row }) => row.original.asset_name ?? '-',
    },
    {
      accessorKey: 'job_type',
      header: t('col.stage'),
      cell: ({ row }) => <Badge variant="outline">{row.original.job_type}</Badge>,
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) => {
        const s = row.original.status;
        return (
          <StatusBadge tone={jobStatusTone(s)}>
            {t(`jobStatus.${s}`, { defaultValue: s })}
          </StatusBadge>
        );
      },
    },
    {
      accessorKey: 'progress',
      header: t('col.progress'),
      cell: ({ row }) => `${(row.original.progress * 100).toFixed(0)}%`,
    },
    { accessorKey: 'attempts', header: t('col.attempts') },
    {
      accessorKey: 'error',
      header: t('col.error'),
      cell: ({ row }) =>
        row.original.error ? (
          <span className="block max-w-60 truncate text-destructive" title={row.original.error}>
            {row.original.error}
          </span>
        ) : (
          '-'
        ),
    },
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
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => (
        <div className="flex gap-1">
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0"
            onClick={() => navigate(`/pipeline/${row.original.id}`)}
          >
            {t('viewDetail')}
          </Button>
          <ConfirmAction
            trigger={
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0"
                disabled={!canWrite || row.original.status !== 'failed'}
              >
                {t('action.retry')}
              </Button>
            }
            title={t('retryConfirmTitle')}
            description={t('retryConfirmDescription')}
            onConfirm={() => setRetryTarget(row.original)}
          />
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('title')} />

      <Card>
        <CardHeader>
          <CardTitle>{t('failuresTitle')}</CardTitle>
        </CardHeader>
        <CardContent>
          {(failures?.items ?? []).length ? (
            <div className="space-y-2">
              {(failures?.items ?? []).map((f) => (
                <div key={f.error_prefix} className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-sm" title={f.error_prefix}>
                    {f.error_prefix}
                  </span>
                  <StatusBadge tone="red">×{f.count}</StatusBadge>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">{t('noFailures')}</p>
          )}
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={status ?? ALL}
          onValueChange={(v) => {
            setStatus(v === ALL ? undefined : v);
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        >
          <SelectTrigger className="w-[130px]">
            <SelectValue placeholder={t('field.status')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t('action.all')}</SelectItem>
            {Object.keys(JOB_STATUS_TONE).map((value) => (
              <SelectItem key={value} value={value}>
                {t(`jobStatus.${value}`, { defaultValue: value })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={jobType ?? ALL}
          onValueChange={(v) => {
            setJobType(v === ALL ? undefined : v);
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        >
          <SelectTrigger className="w-[130px]">
            <SelectValue placeholder={t('filterStagePlaceholder')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t('action.all')}</SelectItem>
            {['parse', 'chunk', 'extract', 'embed', 'index'].map((v) => (
              <SelectItem key={v} value={v}>
                {v}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setMimeType(mimeDraft.trim() || undefined);
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        >
          <Input
            placeholder={t('mimePlaceholder')}
            className="w-[240px]"
            value={mimeDraft}
            onChange={(e) => setMimeDraft(e.target.value)}
          />
          <Button type="submit" variant="outline" size="icon" aria-label={t('mimeFilter')}>
            <Search />
          </Button>
        </form>
      </div>

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        error={isError ? error : undefined}
        total={data?.total}
        page={page.current}
        pageSize={page.pageSize}
        onPageChange={(current) => setPage({ current, pageSize: PAGE_SIZE })}
      />

      {/* 重试（可选步骤）：受理后进度经任务看板跟踪 */}
      <Dialog
        open={retryTarget !== null}
        onOpenChange={(o) => {
          if (!o) {
            setRetryTarget(null);
            setRetryStep(undefined);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {t('retryTitle', {
                id: retryTarget?.id.slice(0, 8) ?? '',
                jobType: retryTarget?.job_type ?? '',
              })}
            </DialogTitle>
          </DialogHeader>
          <Select
            value={retryStep ?? DEFAULT_STEP}
            onValueChange={(v) => setRetryStep(v === DEFAULT_STEP ? undefined : v)}
          >
            <SelectTrigger className="w-[200px]">
              <SelectValue placeholder={t('stepPlaceholder')} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT_STEP}>{t('defaultStep')}</SelectItem>
              {['parse', 'chunk', 'embed', 'index'].map((v) => (
                <SelectItem key={v} value={v}>
                  {v}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => {
                setRetryTarget(null);
                setRetryStep(undefined);
              }}
            >
              {t('action.cancel')}
            </Button>
            <Button disabled={busy} onClick={() => void retry(retryStep).then(() => setRetryStep(undefined))}>
              {busy ? t('retrying') : t('confirmRetry')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
