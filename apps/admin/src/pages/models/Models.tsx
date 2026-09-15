import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { z } from 'zod';
import { cn } from 'cn';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { t as tt } from '@/i18n';
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

/** 任务状态 → 徽章色调（文案经 status.* 翻译）。 */
const TASK_STATUS_TONE: Record<string, BadgeTone> = {
  pending: 'gray',
  running: 'blue',
  succeeded: 'green',
  failed: 'red',
};

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const createSchema = z.object({
  space_id: z.string().min(1, tt('models:spaceIdRequired')),
  target_model: z.string().min(1, tt('models:targetModelRequired')),
});

type CreateValues = z.infer<typeof createSchema>;

/** 模型与检索（/models）：重嵌入任务（模型切换过渡策略，进度可见，docs/04 §5.7）。 */
export function ModelsPage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('models');

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

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-reembed', status, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<ReembedTask>>(
        api.GET('/api/v1/admin/reembed', {
          params: { query: { status: status || undefined, limit, offset } },
        }),
      ),
    // 进行中的任务轮询刷新进度
    refetchInterval: (q) =>
      (q.state.data?.items ?? []).some((task) => task.status === 'running' || task.status === 'pending')
        ? 5_000
        : false,
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['admin-reembed'] });

  const create = form.handleSubmit(async (values) => {
    setBusy(true);
    try {
      await unwrap(api.POST('/api/v1/admin/reembed', { body: values }));
      toast.success(t('createdToast'));
      setCreateOpen(false);
      form.reset();
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('createFailed'));
    } finally {
      setBusy(false);
    }
  });

  /** 取消任务（危险操作）：pending 直接取消；running 由 worker 协作停止。 */
  const cancel = async (task: ReembedTask, reason: string) => {
    setBusy(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/reembed/{task_id}/cancel', {
          params: { path: { task_id: task.id } },
          body: { reason },
        }),
      );
      toast.success(t('cancelToast'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<ReembedTask, unknown>[] = [
    {
      accessorKey: 'id',
      header: t('col.taskId'),
      cell: ({ row }) => `${row.original.id.slice(0, 8)}…`,
    },
    {
      accessorKey: 'space_id',
      header: t('col.space'),
      cell: ({ row }) => row.original.space_id ?? '-',
    },
    {
      accessorKey: 'target_model',
      header: t('col.targetModel'),
      cell: ({ row }) => <StatusBadge tone="purple">{row.original.target_model}</StatusBadge>,
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) => {
        const s = row.original.status;
        return (
          <StatusBadge tone={TASK_STATUS_TONE[s] ?? 'gray'}>
            {t(`status.${s}`, { defaultValue: s })}
          </StatusBadge>
        );
      },
    },
    {
      id: 'progress',
      header: t('col.progress'),
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
      header: t('col.assets'),
      cell: ({ row }) => {
        const r = row.original;
        return `${r.done_assets} / ${r.failed_assets} / ${r.total_assets}`;
      },
    },
    {
      accessorKey: 'error',
      header: t('col.error'),
      cell: ({ row }) => row.original.error ?? '-',
    },
    {
      accessorKey: 'updated_at',
      header: t('col.updatedAt'),
      cell: ({ row }) => formatDateTime(row.original.updated_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) =>
        row.original.status === 'pending' || row.original.status === 'running' ? (
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0 text-destructive hover:text-destructive"
            disabled={!canWrite}
            onClick={() => setCancelTarget(row.original)}
          >
            {t('action.cancel')}
          </Button>
        ) : (
          '-'
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('title')}
        description={t('description')}
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            {t('createTask')}
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
            <SelectValue placeholder={t('field.status')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t('action.all')}</SelectItem>
            {Object.keys(TASK_STATUS_TONE).map((value) => (
              <SelectItem key={value} value={value}>
                {t(`status.${value}`, { defaultValue: value })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        error={isError ? error : undefined}
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
            <DialogTitle>{t('createTask')}</DialogTitle>
          </DialogHeader>
          <form onSubmit={create} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="reembed-space-id">{t('spaceIdLabel')}</Label>
              <Input
                id="reembed-space-id"
                placeholder={t('spaceIdPlaceholder')}
                {...form.register('space_id')}
              />
              {form.formState.errors.space_id && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.space_id.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label>{t('targetModelLabel')}</Label>
              <Select
                value={form.watch('target_model') || undefined}
                onValueChange={(v) => form.setValue('target_model', v, { shouldValidate: true })}
              >
                <SelectTrigger className="w-full">
                  <SelectValue placeholder={t('targetModelPlaceholder')} />
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
                {t('action.cancel')}
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? t('creating') : t('action.create')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 取消任务（危险操作）：理由入审计 */}
      <ReasonModal
        open={cancelTarget !== null}
        title={t('cancelTitle', { id: cancelTarget?.id.slice(0, 8) ?? '' })}
        description={t('cancelDescription')}
        okText={t('confirmCancelTask')}
        danger
        confirmLoading={busy}
        onCancel={() => setCancelTarget(null)}
        onOk={(reason) => (cancelTarget ? cancel(cancelTarget, reason) : Promise.resolve())}
      />
    </div>
  );
}
