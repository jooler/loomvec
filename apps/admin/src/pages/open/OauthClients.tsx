import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal, SecretModal } from '@/components/ReasonModal';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Checkbox } from '@loomvec/ui/components/ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { Textarea } from '@loomvec/ui/components/ui/textarea';
import type { OauthClient, PagedResp } from '@/types';
import { formatDateTime } from '@/utils';

const PAGE_SIZE = 20;

const SCOPE_OPTIONS = [
  { value: 'read', label: 'read' },
  { value: 'write', label: 'write' },
];

const createSchema = z.object({
  name: z.string().min(1, '请输入应用名称'),
  redirect_uris: z.string().min(1, '至少填写一个回调地址'),
  scopes: z.array(z.string()).min(1, '至少选择一个 scope'),
  homepage_url: z.string().optional(),
  description: z.string().optional(),
});

type CreateValues = z.infer<typeof createSchema>;

/** OAuth 应用管理（/open/oauth）：redirect_uris、scopes、启停、secret 重置（docs/04 §5.8）。 */
export function OauthClientsPage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();

  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<OauthClient | null>(null);
  const [secretFields, setSecretFields] = useState<{ label: string; value: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: '', redirect_uris: '', scopes: ['read'] },
  });

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching } = useQuery({
    queryKey: ['admin-oauth-clients', limit, offset],
    queryFn: () =>
      unwrap<PagedResp<OauthClient>>(
        api.GET('/api/v1/admin/oauth/clients', { params: { query: { limit, offset } } }),
      ),
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['admin-oauth-clients'] });

  const closeCreate = () => {
    setCreateOpen(false);
    form.reset();
  };

  const create = form.handleSubmit(async (v) => {
    setBusy(true);
    try {
      const created = await unwrap<OauthClient>(
        api.POST('/api/v1/admin/oauth/clients', {
          body: {
            name: v.name,
            redirect_uris: v.redirect_uris.split('\n').map((s) => s.trim()).filter(Boolean),
            scopes: v.scopes,
            homepage_url: v.homepage_url ?? null,
            description: v.description ?? null,
          },
        }),
      );
      toast.success('OAuth 应用已创建（secret 仅此一次展示）');
      closeCreate();
      setSecretFields([
        { label: 'client_id', value: created.client_id },
        { label: 'client_secret', value: created.client_secret ?? '' },
      ]);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '创建失败');
    } finally {
      setBusy(false);
    }
  });

  const patchStatus = async (c: OauthClient, status: 'active' | 'suspended') => {
    try {
      await unwrap(
        api.PATCH('/api/v1/admin/oauth/clients/{client_db_id}', {
          params: { path: { client_db_id: c.id } },
          body: { status },
        }),
      );
      toast.success(status === 'active' ? '已启用' : '已停用');
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  const resetSecret = async (c: OauthClient) => {
    try {
      const resp = await unwrap<{ client_id: string; client_secret: string }>(
        api.POST('/api/v1/admin/oauth/clients/{client_db_id}/reset-secret', {
          params: { path: { client_db_id: c.id } },
        }),
      );
      setSecretFields([
        { label: 'client_id', value: resp.client_id },
        { label: 'client_secret', value: resp.client_secret },
      ]);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  const columns: ColumnDef<OauthClient, unknown>[] = [
    { accessorKey: 'name', header: '名称' },
    {
      accessorKey: 'client_id',
      header: 'client_id',
      cell: ({ row }) => (
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{row.original.client_id}</code>
      ),
    },
    {
      accessorKey: 'redirect_uris',
      header: 'redirect_uris',
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-1">
          {row.original.redirect_uris.map((u) => (
            <Badge key={u} variant="outline">
              {u}
            </Badge>
          ))}
        </div>
      ),
    },
    {
      accessorKey: 'scopes',
      header: 'scopes',
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-1">
          {row.original.scopes.map((s) => (
            <StatusBadge key={s} tone="blue">
              {s}
            </StatusBadge>
          ))}
        </div>
      ),
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) =>
        row.original.status === 'active' ? (
          <StatusBadge tone="green">启用</StatusBadge>
        ) : (
          <StatusBadge tone="red">停用</StatusBadge>
        ),
    },
    {
      accessorKey: 'created_at',
      header: '创建时间',
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => {
        const c = row.original;
        return (
          <div className="flex flex-wrap gap-1">
            {c.status === 'active' ? (
              <ConfirmAction
                title="确认停用该应用？"
                trigger={
                  <Button variant="link" size="sm" disabled={!canWrite}>
                    停用
                  </Button>
                }
                onConfirm={() => patchStatus(c, 'suspended')}
              />
            ) : (
              <Button variant="link" size="sm" disabled={!canWrite} onClick={() => void patchStatus(c, 'active')}>
                启用
              </Button>
            )}
            <ConfirmAction
              title="确认重置 client secret？"
              description="旧 secret 立即失效，新 secret 仅此一次展示。"
              trigger={
                <Button variant="link" size="sm" disabled={!canWrite}>
                  重置 secret
                </Button>
              }
              onConfirm={() => resetSecret(c)}
            />
            <Button
              variant="link"
              size="sm"
              className="text-destructive hover:text-destructive"
              disabled={!canWrite}
              onClick={() => setDeleteTarget(c)}
            >
              删除
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="OAuth 应用"
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            创建应用
          </Button>
        }
      />

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        total={data?.total}
        page={page.current}
        pageSize={page.pageSize}
        onPageChange={(current) => setPage({ current, pageSize: PAGE_SIZE })}
      />

      <Dialog open={createOpen} onOpenChange={(o) => !o && closeCreate()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>创建 OAuth 应用</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="oauth-name">应用名称</Label>
              <Input id="oauth-name" maxLength={255} {...form.register('name')} />
              {form.formState.errors.name && (
                <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="oauth-uris">Redirect URIs（每行一个）</Label>
              <Textarea
                id="oauth-uris"
                rows={3}
                placeholder={'https://app.example.com/callback'}
                {...form.register('redirect_uris')}
              />
              {form.formState.errors.redirect_uris && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.redirect_uris.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label>Scopes（OAuth 仅用户级读写）</Label>
              <div className="flex gap-4">
                {SCOPE_OPTIONS.map((opt) => (
                  <label key={opt.value} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={form.watch('scopes').includes(opt.value)}
                      onCheckedChange={(checked) => {
                        const cur = form.getValues('scopes');
                        form.setValue(
                          'scopes',
                          checked ? [...cur, opt.value] : cur.filter((s) => s !== opt.value),
                          { shouldValidate: true },
                        );
                      }}
                    />
                    {opt.label}
                  </label>
                ))}
              </div>
              {form.formState.errors.scopes && (
                <p className="text-sm text-destructive">{form.formState.errors.scopes.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="oauth-homepage">主页地址（可选）</Label>
              <Input id="oauth-homepage" {...form.register('homepage_url')} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="oauth-desc">描述（可选）</Label>
              <Textarea id="oauth-desc" rows={2} {...form.register('description')} />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeCreate} disabled={busy}>
                取消
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? '保存中…' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 删除应用（危险操作） */}
      <ReasonModal
        open={deleteTarget !== null}
        title={`删除 OAuth 应用「${deleteTarget?.name ?? ''}」`}
        description="删除后该应用立即不可用，操作不可逆。"
        okText="确认删除"
        danger
        confirmLoading={busy}
        onCancel={() => setDeleteTarget(null)}
        onOk={async (reason) => {
          if (!deleteTarget) return;
          setBusy(true);
          try {
            await unwrap(
              api.DELETE('/api/v1/admin/oauth/clients/{client_db_id}', {
                params: { path: { client_db_id: deleteTarget.id } },
              }),
            );
            toast.success(`已删除（理由：${reason}）`);
            setDeleteTarget(null);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 一次性 secret 展示 */}
      <SecretModal
        open={secretFields !== null}
        title="凭证（仅此一次展示）"
        fields={secretFields ?? []}
        onClose={() => setSecretFields(null)}
      />
    </div>
  );
}
