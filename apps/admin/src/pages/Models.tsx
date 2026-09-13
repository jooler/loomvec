import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { z } from 'zod';
import { cn } from 'cn';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { StatusBadge, type BadgeTone } from '@loomvec/ui/components/status-badge';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { Progress } from '@loomvec/ui/components/ui/progress';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { EMBEDDING_MODEL_OPTIONS } from '@/types';
import type { PagedResp, ReembedTask } from '@/types';
import { formatDateTime } from '@/utils';

const PAGE_SIZE = 20;

/** 筛选 Select 的「不过滤」哨兵值（shadcn Select 无 allowClear，语义等价映射）。 */
const ALL = '__all__';

const TASK_STATUS_META: Record<string, { tone: BadgeTone; text: string }> = {
  pending: { tone: 'gray', text: '排队中' },
  running: { tone: 'blue', text: '进行中' },
  succeeded: { tone: 'green', text: '完成' },
  failed: { tone: 'red', text: '失败' },
};

const createSchema = z.object({
  space_id: z.string().min(1, '请输入空间 ID（UUID）'),
  target_model: z.string().min(1, '请选择目标模型（白名单）'),
});

type CreateValues = z.infer<typeof createSchema>;

/** 模型与检索（/models）：重嵌入任务（模型切换过渡策略，进度可见，docs/04 §5.7）。 */
export function ModelsPage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();

  const [status, setStatus] = useState<string | undefined>();
  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [createOpen, setCreateOpen] = useState(false);
  const [cancelTarget, setCancelTarget] = useState<ReembedTask | null>(null);
  const [busy, setBusy] = useState(false);
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { space_id: '', target_model: '' },
  });

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching } = useQuery({
    queryKey: ['admin-reembed', status, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<ReembedTask>>(
        api.GET('/api/v1/admin/reembed', {
          params: { query: { status: status || undefined, limit, offset } },
        }),
      ),
    // 进行中的任务轮询刷新进度
    refetchInterval: (q) =>
      (q.state.data?.items ?? []).some((t) => t.status === 'running' || t.status === 'pending')
        ? 5_000
        : false,
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['admin-reembed'] });

  const create = form.handleSubmit(async (values) => {
    setBusy(true);
    try {
      await unwrap(api.POST('/api/v1/admin/reembed', { body: values }));
      toast.success('重嵌入任务已创建（检索期间按空间旧版本继续服务，完成后原子切换）');
      setCreateOpen(false);
      form.reset();
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '创建失败');
    } finally {
      setBusy(false);
    }
  });

  /** 取消任务（危险操作）：pending 直接取消；running 由 worker 协作停止。 */
  const cancel = async (reason: string) => {
    if (!cancelTarget) return;
    setBusy(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/reembed/{task_id}/cancel', {
          params: { path: { task_id: cancelTarget.id } },
        }),
      );
      toast.success(`已取消（理由：${reason}）`);
      setCancelTarget(null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<ReembedTask, unknown>[] = [
    {
      accessorKey: 'id',
      header: '任务 ID',
      cell: ({ row }) => `${row.original.id.slice(0, 8)}…`,
    },
    {
      accessorKey: 'space_id',
      header: '空间',
      cell: ({ row }) => row.original.space_id ?? '-',
    },
    {
      accessorKey: 'target_model',
      header: '目标模型',
      cell: ({ row }) => <StatusBadge tone="purple">{row.original.target_model}</StatusBadge>,
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) => {
        const m = TASK_STATUS_META[row.original.status] ?? {
          tone: 'gray' as const,
          text: row.original.status,
        };
        return <StatusBadge tone={m.tone}>{m.text}</StatusBadge>;
      },
    },
    {
      id: 'progress',
      header: '进度',
      cell: ({ row }) => {
        const r = row.original;
        const pct = r.total_assets ? Math.round((r.done_assets / r.total_assets) * 100) : 0;
        return (
          <div className="flex w-32 items-center gap-2">
            <Progress
              value={pct}
              className={cn(
                'h-1.5 flex-1',
                r.status === 'failed' && '[&>[data-slot=progress-indicator]]:bg-destructive',
                r.status === 'succeeded' && '[&>[data-slot=progress-indicator]]:bg-emerald-500',
              )}
            />
            <span className="text-xs tabular-nums text-muted-foreground">{pct}%</span>
          </div>
        );
      },
    },
    {
      id: 'assets',
      header: '资产（完成/失败/总数）',
      cell: ({ row }) => {
        const r = row.original;
        return `${r.done_assets} / ${r.failed_assets} / ${r.total_assets}`;
      },
    },
    {
      accessorKey: 'error',
      header: '错误',
      cell: ({ row }) => row.original.error ?? '-',
    },
    {
      accessorKey: 'updated_at',
      header: '更新时间',
      cell: ({ row }) => formatDateTime(row.original.updated_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) =>
        row.original.status === 'pending' || row.original.status === 'running' ? (
          <ConfirmAction
            trigger={
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-destructive hover:text-destructive"
                disabled={!canWrite}
              >
                取消
              </Button>
            }
            title="确认取消该重嵌入任务？"
            description="取消为危险操作，需填写理由确认。"
            danger
            onConfirm={() => setCancelTarget(row.original)}
          />
        ) : (
          '-'
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="重嵌入任务"
        description="模型切换后的空间级重嵌入；向量携带 model_version，过渡期检索按旧版本继续服务"
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            新建重嵌入任务
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={status ?? ALL}
          onValueChange={(v) => {
            setStatus(v === ALL ? undefined : v);
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="状态" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部</SelectItem>
            {Object.entries(TASK_STATUS_META).map(([value, m]) => (
              <SelectItem key={value} value={value}>
                {m.text}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
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

      <Dialog
        open={createOpen}
        onOpenChange={(o) => {
          if (!o) {
            setCreateOpen(false);
            form.reset();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建重嵌入任务</DialogTitle>
          </DialogHeader>
          <form onSubmit={create} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="reembed-space-id">空间 ID</Label>
              <Input
                id="reembed-space-id"
                placeholder="目标空间 UUID"
                {...form.register('space_id')}
              />
              {form.formState.errors.space_id && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.space_id.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label>目标模型（白名单）</Label>
              <Select
                value={form.watch('target_model') || undefined}
                onValueChange={(v) => form.setValue('target_model', v, { shouldValidate: true })}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="请选择目标模型（白名单）" />
                </SelectTrigger>
                <SelectContent>
                  {EMBEDDING_MODEL_OPTIONS.map((m) => (
                    <SelectItem key={m} value={m}>
                      {m}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {form.formState.errors.target_model && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.target_model.message}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                onClick={() => setCreateOpen(false)}
              >
                取消
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? '创建中…' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 取消任务（危险操作） */}
      <ReasonModal
        open={cancelTarget !== null}
        title={`取消重嵌入任务 ${cancelTarget?.id.slice(0, 8) ?? ''}`}
        description="取消后该任务不再继续执行（running 状态由 worker 协作停止）。"
        okText="确认取消任务"
        danger
        confirmLoading={busy}
        onCancel={() => setCancelTarget(null)}
        onOk={cancel}
      />
    </div>
  );
}
