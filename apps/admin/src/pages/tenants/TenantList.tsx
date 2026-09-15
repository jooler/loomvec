import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { t as tt } from '@/i18n';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
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
import type { PagedResp, TenantRow } from '@/types';
import { formatDateTime, formatQuota } from '@/utils';

const PAGE_SIZE = 20;

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const createSchema = z.object({
  name: z.string().min(1, tt('tenants:form.nameRequired')).max(255),
  plan: z.string().min(1, tt('tenants:form.planRequired')),
  quota_storage_bytes: z.number({ error: tt('tenants:form.quotaStorageRequired') }).min(0),
  quota_file_count: z.number({ error: tt('tenants:form.quotaFilesRequired') }).min(0),
});

type CreateValues = z.infer<typeof createSchema>;

/** 租户管理列表：搜索 / 创建 / 停用（冻结）/ 启用（docs/04 §5.2）。 */
export function TenantListPage() {
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('tenants');

  const [searchText, setSearchText] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [createOpen, setCreateOpen] = useState(false);
  const [suspendTarget, setSuspendTarget] = useState<TenantRow | null>(null);
  const [creating, setCreating] = useState(false);
  const [suspending, setSuspending] = useState(false);
  const createForm = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: '', plan: 'free', quota_storage_bytes: 0, quota_file_count: 0 },
  });

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-tenants', q, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<TenantRow>>(
        api.GET('/api/v1/admin/tenants', {
          params: { query: { q: q || undefined, limit, offset } },
        }),
      ),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['admin-tenants'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
  };

  const create = async (values: CreateValues) => {
    setCreating(true);
    try {
      await unwrap(api.POST('/api/v1/admin/tenants', { body: values }));
      toast.success(t('created'));
      setCreateOpen(false);
      createForm.reset();
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('createFailed'));
    } finally {
      setCreating(false);
    }
  };
  const submitCreate = createForm.handleSubmit(create);

  const suspend = async (reason: string) => {
    if (!suspendTarget) return;
    setSuspending(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/tenants/{tenant_id}/suspend', {
          params: { path: { tenant_id: suspendTarget.id } },
          body: { reason },
        }),
      );
      toast.success(t('suspendToast'));
      setSuspendTarget(null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setSuspending(false);
    }
  };

  const activate = async (tenant: TenantRow) => {
    try {
      await unwrap(
        api.POST('/api/v1/admin/tenants/{tenant_id}/activate', {
          params: { path: { tenant_id: tenant.id } },
        }),
      );
      toast.success(t('activated'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  const columns: ColumnDef<TenantRow, unknown>[] = [
    {
      accessorKey: 'name',
      header: t('field.name'),
      cell: ({ row }) => (
        <button
          className="text-sm font-medium hover:underline"
          onClick={() => navigate(`/tenants/${row.original.id}`)}
        >
          {row.original.name}
        </button>
      ),
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) =>
        row.original.status === 'active' ? (
          <StatusBadge tone="green">{t('status.active')}</StatusBadge>
        ) : (
          <StatusBadge tone="red">{t('status.suspended')}</StatusBadge>
        ),
    },
    { accessorKey: 'plan', header: t('col.plan') },
    {
      id: 'storage_usage',
      header: t('col.storageUsage'),
      cell: ({ row }) =>
        formatQuota(row.original.used_storage_bytes, row.original.quota_storage_bytes),
    },
    {
      id: 'file_usage',
      header: t('col.fileCount'),
      cell: ({ row }) =>
        row.original.quota_file_count
          ? `${row.original.used_file_count} / ${row.original.quota_file_count}`
          : `${row.original.used_file_count} / ${t('unlimited')}`,
    },
    { accessorKey: 'space_count', header: t('col.spaceCount') },
    { accessorKey: 'user_count', header: t('col.userCount') },
    {
      accessorKey: 'created_at',
      header: t('field.createdAt'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => {
        const r = row.original;
        return (
          <div className="flex gap-1">
            <Button variant="link" size="sm" onClick={() => navigate(`/tenants/${r.id}`)}>
              {t('viewDetail')}
            </Button>
            {r.status === 'active' ? (
              <Button
                variant="link"
                size="sm"
                className="text-destructive hover:text-destructive"
                disabled={!canWrite}
                onClick={() => setSuspendTarget(r)}
              >
                {t('suspend')}
              </Button>
            ) : (
              <ConfirmAction
                title={t('activateConfirm')}
                trigger={
                  <Button variant="link" size="sm" disabled={!canWrite}>
                    {t('action.enable')}
                  </Button>
                }
                onConfirm={() => activate(r)}
              />
            )}
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('title')}
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            {t('createTenant')}
          </Button>
        }
      />

      <form
        className="flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setQ(searchText);
          setPage({ current: 1, pageSize: PAGE_SIZE });
        }}
      >
        <Input
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          placeholder={t('searchPlaceholder')}
          className="w-72"
        />
        <Button type="submit" variant="outline" size="icon" aria-label={t('action.search')}>
          <Search />
        </Button>
      </form>

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

      <Dialog
        open={createOpen}
        onOpenChange={(o) => {
          setCreateOpen(o);
          if (!o) createForm.reset();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('createTenant')}</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submitCreate();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="tenant-name">{t('form.nameLabel')}</Label>
              <Input id="tenant-name" maxLength={255} {...createForm.register('name')} />
              {createForm.formState.errors.name && (
                <p className="text-sm text-destructive">
                  {createForm.formState.errors.name.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="tenant-plan">{t('form.planLabel')}</Label>
              <Input
                id="tenant-plan"
                placeholder="free / pro / enterprise"
                {...createForm.register('plan')}
              />
              {createForm.formState.errors.plan && (
                <p className="text-sm text-destructive">
                  {createForm.formState.errors.plan.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="tenant-quota-storage">{t('form.quotaStorageLabel')}</Label>
              <Input
                id="tenant-quota-storage"
                type="number"
                min={0}
                {...createForm.register('quota_storage_bytes', { valueAsNumber: true })}
              />
              {createForm.formState.errors.quota_storage_bytes && (
                <p className="text-sm text-destructive">
                  {createForm.formState.errors.quota_storage_bytes.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="tenant-quota-files">{t('form.quotaFilesLabel')}</Label>
              <Input
                id="tenant-quota-files"
                type="number"
                min={0}
                {...createForm.register('quota_file_count', { valueAsNumber: true })}
              />
              {createForm.formState.errors.quota_file_count && (
                <p className="text-sm text-destructive">
                  {createForm.formState.errors.quota_file_count.message}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setCreateOpen(false)}
                disabled={creating}
              >
                {t('action.cancel')}
              </Button>
              <Button type="submit" disabled={creating}>
                {creating ? t('creating') : t('action.create')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ReasonModal
        open={suspendTarget !== null}
        title={t('suspendTitle', { name: suspendTarget?.name ?? '' })}
        description={t('suspendDescription')}
        okText={t('confirmSuspend')}
        danger
        confirmLoading={suspending}
        onOk={suspend}
        onCancel={() => setSuspendTarget(null)}
      />
    </div>
  );
}
