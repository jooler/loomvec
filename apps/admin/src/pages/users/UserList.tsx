import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search, X } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { Textarea } from '@loomvec/ui/components/ui/textarea';
import type { PagedResp, UserRow } from '@/types';
import { formatDateTime } from '@/utils';

const PAGE_SIZE = 20;
const PLATFORM_ROLE_OPTIONS = [
  { value: 'super_admin', label: 'super_admin（全部功能）' },
  { value: 'operator', label: 'operator（日常运营）' },
  { value: 'auditor', label: 'auditor（只读+审计导出）' },
];

const roleSchema = z.object({
  role: z.string().min(1, '请选择平台角色'),
  reason: z.string().min(1, '请填写操作理由（将记入审计日志）'),
});

type RoleValues = z.infer<typeof roleSchema>;

/** 用户与权限：用户列表 / 禁用启用 / 平台角色分配（仅 super_admin，docs/04 §5.3）。 */
export function UserListPage() {
  const { canWrite, isSuperAdmin } = usePerm();
  const queryClient = useQueryClient();

  const [searchText, setSearchText] = useState('');
  const [q, setQ] = useState('');
  const [status, setStatus] = useState<string | undefined>();
  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [disableTarget, setDisableTarget] = useState<UserRow | null>(null);
  const [roleTarget, setRoleTarget] = useState<UserRow | null>(null);
  const [busy, setBusy] = useState(false);
  const roleForm = useForm<RoleValues>({
    resolver: zodResolver(roleSchema),
    defaultValues: { role: '', reason: '' },
  });

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-users', q, status, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<UserRow>>(
        api.GET('/api/v1/admin/users', {
          params: { query: { q: q || undefined, status: status || undefined, limit, offset } },
        }),
      ),
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['admin-users'] });

  const enable = async (u: UserRow) => {
    try {
      await unwrap(
        api.POST('/api/v1/admin/users/{user_id}/enable', {
          params: { path: { user_id: u.id } },
        }),
      );
      toast.success('已启用');
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  const assignRole = roleForm.handleSubmit(async (values) => {
    if (!roleTarget) return;
    setBusy(true);
    try {
      await unwrap(
        api.PUT('/api/v1/admin/users/{user_id}/platform-role', {
          params: { path: { user_id: roleTarget.id } },
          body: { role: values.role, reason: values.reason },
        }),
      );
      toast.success('平台角色已更新');
      setRoleTarget(null);
      roleForm.reset();
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  });

  const columns: ColumnDef<UserRow, unknown>[] = [
    {
      accessorKey: 'username',
      header: '用户名',
      cell: ({ row }) =>
        row.original.display_name
          ? `${row.original.username}（${row.original.display_name}）`
          : row.original.username,
    },
    {
      accessorKey: 'email',
      header: '邮箱',
      cell: ({ row }) => row.original.email ?? '-',
    },
    {
      accessorKey: 'tenant_name',
      header: '所属租户',
      cell: ({ row }) => row.original.tenant_name ?? '-',
    },
    {
      accessorKey: 'auth_source',
      header: '来源',
      cell: ({ row }) => (
        <Badge variant="outline">{row.original.auth_source === 'oidc' ? 'OIDC' : '本地'}</Badge>
      ),
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) =>
        row.original.status === 'active' ? (
          <StatusBadge tone="green">正常</StatusBadge>
        ) : (
          <StatusBadge tone="red">已禁用</StatusBadge>
        ),
    },
    {
      accessorKey: 'platform_roles',
      header: '平台角色',
      cell: ({ row }) => {
        const roles = row.original.platform_roles;
        return roles.length ? (
          <div className="flex flex-wrap gap-1">
            {roles.map((r) => (
              <StatusBadge key={r} tone="purple">
                {r}
              </StatusBadge>
            ))}
          </div>
        ) : (
          <Badge variant="outline">—</Badge>
        );
      },
    },
    {
      accessorKey: 'last_active_at',
      header: '最近活跃',
      cell: ({ row }) => formatDateTime(row.original.last_active_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => {
        const r = row.original;
        return (
          <div className="flex gap-1">
            {r.status === 'active' ? (
              <Button
                variant="link"
                size="sm"
                className="text-destructive hover:text-destructive"
                disabled={!canWrite}
                onClick={() => setDisableTarget(r)}
              >
                禁用
              </Button>
            ) : (
              <ConfirmAction
                title="确认启用该用户？"
                trigger={
                  <Button variant="link" size="sm" disabled={!canWrite}>
                    启用
                  </Button>
                }
                onConfirm={() => enable(r)}
              />
            )}
            <Button
              variant="link"
              size="sm"
              disabled={!isSuperAdmin}
              title={isSuperAdmin ? undefined : '仅 super_admin 可分配平台角色'}
              onClick={() => {
                setRoleTarget(r);
                roleForm.reset({ role: r.platform_roles[0] ?? '', reason: '' });
              }}
            >
              角色分配
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title="用户与权限" />

      <div className="flex flex-wrap items-center gap-2">
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
            placeholder="按用户名/邮箱搜索"
            className="w-64"
          />
          <Button type="submit" variant="outline" size="icon" aria-label="搜索">
            <Search />
          </Button>
        </form>
        <div className="flex items-center gap-1">
          <Select
            value={status}
            onValueChange={(v) => {
              setStatus(v);
              setPage({ current: 1, pageSize: PAGE_SIZE });
            }}
          >
            <SelectTrigger className="w-[140px]">
              <SelectValue placeholder="状态" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="active">正常</SelectItem>
              <SelectItem value="disabled">已禁用</SelectItem>
            </SelectContent>
          </Select>
          {status && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="清除筛选"
              title="清除筛选"
              onClick={() => {
                setStatus(undefined);
                setPage({ current: 1, pageSize: PAGE_SIZE });
              }}
            >
              <X />
            </Button>
          )}
        </div>
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

      {/* 禁用（危险操作）：必填理由 */}
      <ReasonModal
        open={disableTarget !== null}
        title={`禁用用户「${disableTarget?.username ?? ''}」`}
        description="禁用后该用户无法登录与访问 API。"
        okText="确认禁用"
        danger
        confirmLoading={busy}
        onCancel={() => setDisableTarget(null)}
        onOk={async (reason) => {
          if (!disableTarget) return;
          setBusy(true);
          try {
            await unwrap(
              api.POST('/api/v1/admin/users/{user_id}/disable', {
                params: { path: { user_id: disableTarget.id } },
                body: { reason },
              }),
            );
            toast.success('已禁用');
            setDisableTarget(null);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 平台角色分配（仅 super_admin）：必填理由 */}
      <Dialog
        open={roleTarget !== null}
        onOpenChange={(o) => {
          if (!o) {
            setRoleTarget(null);
            roleForm.reset();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>平台角色分配：{roleTarget?.username ?? ''}</DialogTitle>
            <DialogDescription>操作理由将记入审计日志。</DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void assignRole();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="role-assign">平台角色</Label>
              <Select
                value={roleForm.watch('role') || undefined}
                onValueChange={(v) => roleForm.setValue('role', v)}
              >
                <SelectTrigger className="w-full" id="role-assign">
                  <SelectValue placeholder="选择平台角色" />
                </SelectTrigger>
                <SelectContent>
                  {PLATFORM_ROLE_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {roleForm.formState.errors.role && (
                <p className="text-sm text-destructive">{roleForm.formState.errors.role.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="role-reason">操作理由</Label>
              <Textarea
                id="role-reason"
                rows={3}
                maxLength={500}
                {...roleForm.register('reason')}
              />
              <div className="flex items-center justify-end">
                <span className="text-xs text-muted-foreground">
                  {(roleForm.watch('reason') ?? '').length}/500
                </span>
              </div>
              {roleForm.formState.errors.reason && (
                <p className="text-sm text-destructive">
                  {roleForm.formState.errors.reason.message}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setRoleTarget(null);
                  roleForm.reset();
                }}
                disabled={busy}
              >
                取消
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? '保存中…' : '确认分配'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
