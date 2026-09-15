import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
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
import { WEBHOOK_EVENT_TYPES } from '@/types';
import type { PagedResp, Subscription } from '@/types';
import { formatDateTime } from '@/utils';
import { DeliverySheet } from './DeliverySheet';

const PAGE_SIZE = 20;

/** Webhook 管理（/open/webhooks）：事件订阅、投递日志、失败重放（docs/04 §5.8）。 */
export function WebhooksPage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('open');

  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Subscription | null>(null);
  const [secretValue, setSecretValue] = useState<string | null>(null);
  const [deliverySub, setDeliverySub] = useState<Subscription | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ name: '', url: '', eventTypes: [] as string[], description: '' });
  const [formError, setFormError] = useState<{ name?: string; url?: string; eventTypes?: string }>({});

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-webhook-subs', limit, offset],
    queryFn: () =>
      unwrap<PagedResp<Subscription>>(
        api.GET('/api/v1/admin/webhooks/subscriptions', {
          params: { query: { limit, offset } },
        }),
      ),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['admin-webhook-subs'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-webhook-deliveries'] });
  };

  const closeCreate = () => {
    setCreateOpen(false);
    setForm({ name: '', url: '', eventTypes: [], description: '' });
    setFormError({});
  };

  const create = async () => {
    const errors: typeof formError = {};
    if (!form.name.trim()) errors.name = t('webhook.nameRequired');
    if (!form.url.trim()) errors.url = t('webhook.urlRequired');
    if (form.eventTypes.length === 0) errors.eventTypes = t('webhook.eventsRequired');
    setFormError(errors);
    if (Object.keys(errors).length > 0) return;
    setBusy(true);
    try {
      const created = await unwrap<Subscription>(
        api.POST('/api/v1/admin/webhooks/subscriptions', {
          body: {
            name: form.name,
            url: form.url,
            event_types: form.eventTypes,
            description: form.description || null,
          },
        }),
      );
      toast.success(t('webhook.createdToast'));
      closeCreate();
      setSecretValue(created.secret ?? null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('webhook.createFailed'));
    } finally {
      setBusy(false);
    }
  };

  const patch = async (sub: Subscription, body: { paused?: boolean }) => {
    try {
      await unwrap(
        api.PATCH('/api/v1/admin/webhooks/subscriptions/{sub_id}', {
          params: { path: { sub_id: sub.id } },
          body,
        }),
      );
      toast.success(t('webhook.updatedToast'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  const resetSecret = async (sub: Subscription) => {
    try {
      const resp = await unwrap<{ secret: string }>(
        api.POST('/api/v1/admin/webhooks/subscriptions/{sub_id}/reset-secret', {
          params: { path: { sub_id: sub.id } },
        }),
      );
      setSecretValue(resp.secret);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  /** 删除订阅（危险操作）：理由入审计。 */
  const remove = async (sub: Subscription, reason: string) => {
    setBusy(true);
    try {
      await unwrap(
        api.DELETE('/api/v1/admin/webhooks/subscriptions/{sub_id}', {
          params: { path: { sub_id: sub.id } },
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

  const columns: ColumnDef<Subscription, unknown>[] = [
    { accessorKey: 'name', header: t('field.name') },
    {
      accessorKey: 'url',
      header: t('webhook.col.url'),
      cell: ({ row }) => (
        <code className="block max-w-56 truncate rounded bg-muted px-1 py-0.5 font-mono text-xs" title={row.original.url}>
          {row.original.url}
        </code>
      ),
    },
    {
      accessorKey: 'event_types',
      header: t('webhook.col.events'),
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-1">
          {row.original.event_types.map((e) => (
            <Badge key={e} variant="outline">
              {e}
            </Badge>
          ))}
        </div>
      ),
    },
    {
      accessorKey: 'paused',
      header: t('field.status'),
      cell: ({ row }) =>
        row.original.paused ? (
          <StatusBadge tone="amber">{t('webhook.status.paused')}</StatusBadge>
        ) : (
          <StatusBadge tone="green">{t('webhook.status.active')}</StatusBadge>
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
        const s = row.original;
        return (
          <div className="flex flex-wrap gap-1">
            <Button variant="link" size="sm" onClick={() => setDeliverySub(s)}>
              {t('webhook.deliveries')}
            </Button>
            {s.paused ? (
              <Button variant="link" size="sm" disabled={!canWrite} onClick={() => void patch(s, { paused: false })}>
                {t('webhook.resume')}
              </Button>
            ) : (
              <Button variant="link" size="sm" disabled={!canWrite} onClick={() => void patch(s, { paused: true })}>
                {t('webhook.pause')}
              </Button>
            )}
            <ConfirmAction
              title={t('webhook.resetSecretConfirm')}
              description={t('webhook.resetSecretDescription')}
              trigger={
                <Button variant="link" size="sm" disabled={!canWrite}>
                  {t('webhook.resetSecret')}
                </Button>
              }
              onConfirm={() => resetSecret(s)}
            />
            <Button
              variant="link"
              size="sm"
              className="text-destructive hover:text-destructive"
              disabled={!canWrite}
              onClick={() => setDeleteTarget(s)}
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
        title={t('webhook.title')}
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            {t('webhook.create')}
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
            <DialogTitle>{t('webhook.createTitle')}</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="webhook-name">{t('webhook.nameLabel')}</Label>
              <Input
                id="webhook-name"
                maxLength={255}
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              />
              {formError.name && <p className="text-sm text-destructive">{formError.name}</p>}
            </div>
            <div className="space-y-2">
              <Label htmlFor="webhook-url">{t('webhook.urlLabel')}</Label>
              <Input
                id="webhook-url"
                placeholder="https://example.com/hooks/loomvec"
                value={form.url}
                onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
              />
              {formError.url && <p className="text-sm text-destructive">{formError.url}</p>}
            </div>
            <div className="space-y-2">
              <Label>{t('webhook.eventsLabel')}</Label>
              <div className="max-h-48 space-y-2 overflow-y-auto rounded-md border p-3">
                {WEBHOOK_EVENT_TYPES.map((e) => (
                  <label key={e} className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={form.eventTypes.includes(e)}
                      onCheckedChange={(checked) => {
                        setForm((f) => ({
                          ...f,
                          eventTypes: checked
                            ? [...f.eventTypes, e]
                            : f.eventTypes.filter((ev) => ev !== e),
                        }));
                        if (formError.eventTypes) setFormError((p) => ({ ...p, eventTypes: undefined }));
                      }}
                    />
                    <code className="font-mono text-xs">{e}</code>
                  </label>
                ))}
              </div>
              {formError.eventTypes && (
                <p className="text-sm text-destructive">{formError.eventTypes}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="webhook-desc">{t('webhook.descriptionLabel')}</Label>
              <Textarea
                id="webhook-desc"
                rows={2}
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              />
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

      {/* 删除订阅（危险操作）：理由入审计 */}
      <ReasonModal
        open={deleteTarget !== null}
        title={t('webhook.deleteTitle', { name: deleteTarget?.name ?? '' })}
        description={t('webhook.deleteDescription')}
        okText={t('webhook.confirmDelete')}
        danger
        confirmLoading={busy}
        onCancel={() => setDeleteTarget(null)}
        onOk={(reason) => (deleteTarget ? remove(deleteTarget, reason) : Promise.resolve())}
      />

      <SecretModal
        open={secretValue !== null}
        title={t('webhook.secretTitle')}
        fields={secretValue ? [{ label: t('webhook.secretLabel'), value: secretValue }] : []}
        onClose={() => setSecretValue(null)}
      />

      {deliverySub && (
        <DeliverySheet sub={deliverySub} canWrite={canWrite} onClose={() => setDeliverySub(null)} />
      )}
    </div>
  );
}
