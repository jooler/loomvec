import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
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

const createSchema = z.object({
  name: z.string().min(1, '请输入租户名称').max(255),
  plan: z.string().min(1, '请输入套餐'),
  quota_storage_bytes: z.number({ error: '请输入存储配额' }).min(0),
  quota_file_count: z.number({ error: '请输入文件数配额' }).min(0),
});

type CreateValues = z.infer<typeof createSchema>;

/** 租户管理列表：搜索 / 创建 / 停用（冻结）/ 启用（docs/04 §5.2）。 */
export function TenantListPage() {
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

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

  const { data, isFetching } = useQuery({
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
      toast.success('租户已创建');
      setCreateOpen(false);
      createForm.reset();
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '创建失败');
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
      toast.success('租户已停用（冻结登录与 API 访问，数据保留）');
      setSuspendTarget(null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setSuspending(false);
    }
  };

  const activate = async (t: TenantRow) => {
    try {
      await unwrap(
        api.POST('/api/v1/admin/tenants/{tenant_id}/activate', {
          params: { path: { tenant_id: t.id } },
        }),
      );
      toast.success('租户已启用');
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  const columns: ColumnDef<TenantRow, unknown>[] = [
    {
      accessorKey: 'name',
      header: '名称',
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
      header: '状态',
      cell: ({ row }) =>
        row.original.status === 'active' ? (
          <StatusBadge tone="green">正常</StatusBadge>
        ) : (
          <StatusBadge tone="red">已停用</StatusBadge>
        ),
    },
    { accessorKey: 'plan', header: '套餐' },
    {
      id: 'storage_usage',
      header: '存储用量',
      cell: ({ row }) =>
        formatQuota(row.original.used_storage_bytes, row.original.quota_storage_bytes),
    },
    {
      id: 'file_usage',
      header: '文件数',
      cell: ({ row }) =>
        row.original.quota_file_count
          ? `${row.original.used_file_count} / ${row.original.quota_file_count}`
          : `${row.original.used_file_count} / 不限`,
    },
    { accessorKey: 'space_count', header: '空间数' },
    { accessorKey: 'user_count', header: '用户数' },
    {
      accessorKey: 'created_at',
      header: '创建时间',
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => {
        const r = row.original;
        return (
          <div className="flex gap-1">
            <Button variant="link" size="sm" onClick={() => navigate(`/tenants/${r.id}`)}>
              详情
            </Button>
            {r.status === 'active' ? (
              <Button
                variant="link"
                size="sm"
                className="text-destructive hover:text-destructive"
                disabled={!canWrite}
                onClick={() => setSuspendTarget(r)}
              >
                停用
              </Button>
            ) : (
              <ConfirmAction
                title="确认启用该租户？"
                trigger={
                  <Button variant="link" size="sm" disabled={!canWrite}>
                    启用
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
        title="租户管理"
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            创建租户
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
          placeholder="按租户名称搜索"
          className="w-72"
        />
        <Button type="submit" variant="outline" size="icon" aria-label="搜索">
          <Search />
        </Button>
      </form>

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
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
            <DialogTitle>创建租户</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void submitCreate();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="tenant-name">租户名称</Label>
              <Input id="tenant-name" maxLength={255} {...createForm.register('name')} />
              {createForm.formState.errors.name && (
                <p className="text-sm text-destructive">
                  {createForm.formState.errors.name.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="tenant-plan">套餐</Label>
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
              <Label htmlFor="tenant-quota-storage">存储配额（字节，0 不限）</Label>
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
              <Label htmlFor="tenant-quota-files">文件数配额（0 不限）</Label>
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
                取消
              </Button>
              <Button type="submit" disabled={creating}>
                {creating ? '创建中…' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ReasonModal
        open={suspendTarget !== null}
        title={`停用租户「${suspendTarget?.name ?? ''}」`}
        description="停用后该租户所有用户登录与 API 访问被冻结，数据保留。此为危险操作，需二次确认并填写理由。"
        okText="确认停用"
        danger
        confirmLoading={suspending}
        onOk={suspend}
        onCancel={() => setSuspendTarget(null)}
      />
    </div>
  );
}
