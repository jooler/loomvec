import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
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
import { SecretModal } from '@/components/SecretModal';
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

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const createSchema = z.object({
  name: z.string().min(1, tt('open:oauth.nameRequired')),
  redirect_uris: z.string().min(1, tt('open:oauth.redirectRequired')),
  scopes: z.array(z.string()).min(1, tt('open:oauth.scopesRequired')),
  homepage_url: z.string().optional(),
  description: z.string().optional(),
});

type CreateValues = z.infer<typeof createSchema>;

/** OAuth 应用管理（/open/oauth）：redirect_uris、scopes、启停、secret 重置（docs/04 §5.8）。 */
export function OauthClientsPage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('open');

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

  const { data, isFetching, isError, error } = useQuery({
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
      toast.success(t('oauth.createdToast'));
      closeCreate();
      setSecretFields([
        { label: 'client_id', value: created.client_id },
        { label: 'client_secret', value: created.client_secret ?? '' },
      ]);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('oauth.createFailed'));
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
      toast.success(status === 'active' ? t('oauth.activatedToast') : t('oauth.suspendedToast'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  /** 删除应用（危险操作）：理由入审计。 */
  const remove = async (c: OauthClient, reason: string) => {
    setBusy(true);
    try {
      await unwrap(
        api.DELETE('/api/v1/admin/oauth/clients/{client_db_id}', {
          params: { path: { client_db_id: c.id } },
          body: { reason },
        }),
      );
      toast.success(t('feedback.deleted'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
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
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  const columns: ColumnDef<OauthClient, unknown>[] = [
    { accessorKey: 'name', header: t('field.name') },
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
      header: t('field.status'),
      cell: ({ row }) =>
        row.original.status === 'active' ? (
          <StatusBadge tone="green">{t('oauth.status.active')}</StatusBadge>
        ) : (
          <StatusBadge tone="red">{t('oauth.status.suspended')}</StatusBadge>
        ),
    },
    {
      accessorKey: 'created_at',
      header: t('field.createdAt'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => {
        const c = row.original;
        return (
          <div className="flex flex-wrap gap-1">
            {c.status === 'active' ? (
              <ConfirmAction
                title={t('oauth.suspendConfirm')}
                trigger={
                  <Button variant="link" size="sm" disabled={!canWrite}>
                    {t('oauth.status.suspended')}
                  </Button>
                }
                onConfirm={() => patchStatus(c, 'suspended')}
              />
            ) : (
              <Button variant="link" size="sm" disabled={!canWrite} onClick={() => void patchStatus(c, 'active')}>
                {t('action.enable')}
              </Button>
            )}
            <ConfirmAction
              title={t('oauth.resetSecretConfirm')}
              description={t('oauth.resetSecretDescription')}
              trigger={
                <Button variant="link" size="sm" disabled={!canWrite}>
                  {t('oauth.resetSecret')}
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
              {t('action.delete')}
            </Button>
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('oauth.title')}
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            {t('oauth.create')}
          </Button>
        }
      />

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

      <Dialog open={createOpen} onOpenChange={(o) => !o && closeCreate()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('oauth.createTitle')}</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="oauth-name">{t('oauth.nameLabel')}</Label>
              <Input id="oauth-name" maxLength={255} {...form.register('name')} />
              {form.formState.errors.name && (
                <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="oauth-uris">{t('oauth.redirectLabel')}</Label>
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
              <Label>{t('oauth.scopesLabel')}</Label>
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
              <Label htmlFor="oauth-homepage">{t('oauth.homepageLabel')}</Label>
              <Input id="oauth-homepage" {...form.register('homepage_url')} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="oauth-desc">{t('oauth.descriptionLabel')}</Label>
              <Textarea id="oauth-desc" rows={2} {...form.register('description')} />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeCreate} disabled={busy}>
                {t('action.cancel')}
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? t('action.saving') : t('action.create')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 删除应用（危险操作）：理由入审计 */}
      <ReasonModal
        open={deleteTarget !== null}
        title={t('oauth.deleteTitle', { name: deleteTarget?.name ?? '' })}
        description={t('oauth.deleteDescription')}
        okText={t('oauth.confirmDelete')}
        danger
        confirmLoading={busy}
        onCancel={() => setDeleteTarget(null)}
        onOk={(reason) => (deleteTarget ? remove(deleteTarget, reason) : Promise.resolve())}
      />

      {/* 一次性 secret 展示 */}
      <SecretModal
        open={secretFields !== null}
        title={t('oauth.secretTitle')}
        fields={secretFields ?? []}
        onClose={() => setSecretFields(null)}
      />
    </div>
  );
}
