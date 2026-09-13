import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge, type BadgeTone } from '@loomvec/ui/components/status-badge';
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
import { formatDateTime } from '@/utils';

const PAGE_SIZE = 20;

/** 筛选/步骤 Select 的「不过滤/默认」哨兵值（shadcn Select 无 allowClear，语义等价映射）。 */
const ALL = '__all__';
const DEFAULT_STEP = '__default__';

const JOB_STATUS_META: Record<string, { tone: BadgeTone; text: string }> = {
  pending: { tone: 'gray', text: '排队' },
  running: { tone: 'blue', text: '进行中' },
  succeeded: { tone: 'green', text: '成功' },
  failed: { tone: 'red', text: '失败' },
};

/** 管线监控：任务看板（状态/阶段/资产类型筛选）+ 失败原因聚合 + 单任务重试（docs/04 §5.6）。 */
export function PipelineListPage() {
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [status, setStatus] = useState<string | undefined>();
  const [jobType, setJobType] = useState<string | undefined>();
  const [mimeType, setMimeType] = useState<string | undefined>();
  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [retryTarget, setRetryTarget] = useState<JobRow | null>(null);
  const [retryStep, setRetryStep] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching } = useQuery({
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
      toast.success('重试已受理');
      setRetryTarget(null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<JobRow, unknown>[] = [
    {
      accessorKey: 'id',
      header: '任务 ID',
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
      header: '资产',
      cell: ({ row }) => row.original.asset_name ?? '-',
    },
    {
      accessorKey: 'job_type',
      header: '阶段',
      cell: ({ row }) => <Badge variant="outline">{row.original.job_type}</Badge>,
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) => {
        const m = JOB_STATUS_META[row.original.status] ?? {
          tone: 'gray' as const,
          text: row.original.status,
        };
        return <StatusBadge tone={m.tone}>{m.text}</StatusBadge>;
      },
    },
    {
      accessorKey: 'progress',
      header: '进度',
      cell: ({ row }) => `${(row.original.progress * 100).toFixed(0)}%`,
    },
    { accessorKey: 'attempts', header: '尝试' },
    {
      accessorKey: 'error',
      header: '错误',
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
      header: '开始',
      cell: ({ row }) => formatDateTime(row.original.started_at),
    },
    {
      accessorKey: 'finished_at',
      header: '结束',
      cell: ({ row }) => formatDateTime(row.original.finished_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => (
        <div className="flex gap-1">
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0"
            onClick={() => navigate(`/pipeline/${row.original.id}`)}
          >
            详情
          </Button>
          <ConfirmAction
            trigger={
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0"
                disabled={!canWrite || row.original.status !== 'failed'}
              >
                重试
              </Button>
            }
            title="确认重试该任务？"
            description="将从指定步骤（默认 parse）重新派发管线任务。"
            onConfirm={() => setRetryTarget(row.original)}
          />
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title="管线监控" />

      <Card>
        <CardHeader>
          <CardTitle>失败原因聚合 Top 10</CardTitle>
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
            <p className="text-sm text-muted-foreground">近 24h 无失败记录</p>
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
            <SelectValue placeholder="状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部</SelectItem>
            {Object.entries(JOB_STATUS_META).map(([value, m]) => (
              <SelectItem key={value} value={value}>
                {m.text}
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
            <SelectValue placeholder="管线阶段" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部</SelectItem>
            {['parse', 'chunk', 'extract', 'embed', 'index'].map((v) => (
              <SelectItem key={v} value={v}>
                {v}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          placeholder="资产 MIME 类型，如 application/pdf"
          className="w-[240px]"
          onChange={(e) => {
            setMimeType(e.target.value || undefined);
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        />
      </div>

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        total={data?.total}
        page={page.current}
        pageSize={PAGE_SIZE}
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
              {`重试任务 ${retryTarget?.id.slice(0, 8) ?? ''}（${retryTarget?.job_type ?? ''}）`}
            </DialogTitle>
          </DialogHeader>
          <Select
            value={retryStep ?? DEFAULT_STEP}
            onValueChange={(v) => setRetryStep(v === DEFAULT_STEP ? undefined : v)}
          >
            <SelectTrigger className="w-[200px]">
              <SelectValue placeholder="起始步骤（默认 parse）" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT_STEP}>默认（parse）</SelectItem>
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
              取消
            </Button>
            <Button disabled={busy} onClick={() => void retry(retryStep).then(() => setRetryStep(undefined))}>
              {busy ? '重试中…' : '确认重试'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
