import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search, X } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
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
// 模块级文案（角色下拉选项）：main.tsx 已先初始化 i18n，import 阶段取值安全
const PLATFORM_ROLE_OPTIONS = [
  { value: 'super_admin', label: tt('users:roleOption.superAdmin') },
  { value: 'operator', label: tt('users:roleOption.operator') },
  { value: 'auditor', label: tt('users:roleOption.auditor') },
];

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const roleSchema = z.object({
  role: z.string().min(1, tt('users:roleRequired')),
  reason: z.string().min(1, tt('users:reasonRequired')),
});

type RoleValues = z.infer<typeof roleSchema>;

/** 用户与权限：用户列表 / 禁用启用 / 平台角色分配（仅 super_admin，docs/04 §5.3）。 */
export function UserListPage() {
  const { canWrite, isSuperAdmin } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('users');

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
      toast.success(t('enabled'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
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
      toast.success(t('roleUpdated'));
      setRoleTarget(null);
      roleForm.reset();
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  });

  const columns: ColumnDef<UserRow, unknown>[] = [
    {
      accessorKey: 'username',
      header: t('col.username'),
      cell: ({ row }) =>
        row.original.display_name
          ? `${row.original.username}（${row.original.display_name}）`
          : row.original.username,
    },
    {
      accessorKey: 'email',
      header: t('col.email'),
      cell: ({ row }) => row.original.email ?? '-',
    },
    {
      accessorKey: 'tenant_name',
      header: t('col.tenant'),
      cell: ({ row }) => row.original.tenant_name ?? '-',
    },
    {
      accessorKey: 'auth_source',
      header: t('col.source'),
      cell: ({ row }) => (
        <Badge variant="outline">
          {row.original.auth_source === 'oidc' ? t('source.oidc') : t('source.local')}
        </Badge>
      ),
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) =>
        row.original.status === 'active' ? (
          <StatusBadge tone="green">{t('status.active')}</StatusBadge>
        ) : (
          <StatusBadge tone="red">{t('status.disabled')}</StatusBadge>
        ),
    },
    {
      accessorKey: 'platform_roles',
      header: t('col.platformRole'),
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
      header: t('col.lastActive'),
      cell: ({ row }) => formatDateTime(row.original.last_active_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
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
                {t('action.disable')}
              </Button>
            ) : (
              <ConfirmAction
                title={t('enableConfirm')}
                trigger={
                  <Button variant="link" size="sm" disabled={!canWrite}>
                    {t('action.enable')}
                  </Button>
                }
                onConfirm={() => enable(r)}
              />
            )}
            <Button
              variant="link"
              size="sm"
              disabled={!isSuperAdmin}
              title={isSuperAdmin ? undefined : t('roleAssignOnly')}
              onClick={() => {
                setRoleTarget(r);
                roleForm.reset({ role: r.platform_roles[0] ?? '', reason: '' });
              }}
            >
              {t('roleAssign')}
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('title')} />

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
            placeholder={t('searchPlaceholder')}
            className="w-64"
          />
          <Button type="submit" variant="outline" size="icon" aria-label={t('action.search')}>
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
              <SelectValue placeholder={t('field.status')} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="active">{t('status.active')}</SelectItem>
              <SelectItem value="disabled">{t('status.disabled')}</SelectItem>
            </SelectContent>
          </Select>
          {status && (
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={t('clearFilter')}
              title={t('clearFilter')}
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
        title={t('disableTitle', { name: disableTarget?.username ?? '' })}
        description={t('disableDescription')}
        okText={t('confirmDisable')}
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
            toast.success(t('disabledToast'));
            setDisableTarget(null);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
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
            <DialogTitle>{t('roleTitle', { name: roleTarget?.username ?? '' })}</DialogTitle>
            <DialogDescription>{t('roleDescription')}</DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void assignRole();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="role-assign">{t('roleLabel')}</Label>
              <Select
                value={roleForm.watch('role') || undefined}
                onValueChange={(v) => roleForm.setValue('role', v)}
              >
                <SelectTrigger className="w-full" id="role-assign">
                  <SelectValue placeholder={t('rolePlaceholder')} />
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
              <Label htmlFor="role-reason">{t('reasonLabel')}</Label>
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
                {t('action.cancel')}
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? t('action.saving') : t('confirmAssign')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
