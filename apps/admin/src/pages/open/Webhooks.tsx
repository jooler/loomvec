import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { toast } from 'sonner';
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
    if (!form.name.trim()) errors.name = '请输入订阅名称';
    if (!form.url.trim()) errors.url = '请输入回调地址（https://）';
    if (form.eventTypes.length === 0) errors.eventTypes = '至少订阅一个事件';
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
      toast.success('订阅已创建（signing secret 仅此一次展示）');
      closeCreate();
      setSecretValue(created.secret ?? null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '创建失败');
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
      toast.success('已更新');
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
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
      toast.error(e instanceof Error ? e.message : '操作失败');
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
      toast.success('已删除');
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<Subscription, unknown>[] = [
    { accessorKey: 'name', header: '名称' },
    {
      accessorKey: 'url',
      header: '回调地址',
      cell: ({ row }) => (
        <code className="block max-w-56 truncate rounded bg-muted px-1 py-0.5 font-mono text-xs" title={row.original.url}>
          {row.original.url}
        </code>
      ),
    },
    {
      accessorKey: 'event_types',
      header: '事件',
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
      header: '状态',
      cell: ({ row }) =>
        row.original.paused ? (
          <StatusBadge tone="amber">已暂停</StatusBadge>
        ) : (
          <StatusBadge tone="green">订阅中</StatusBadge>
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
        const s = row.original;
        return (
          <div className="flex flex-wrap gap-1">
            <Button variant="link" size="sm" onClick={() => setDeliverySub(s)}>
              投递日志
            </Button>
            {s.paused ? (
              <Button variant="link" size="sm" disabled={!canWrite} onClick={() => void patch(s, { paused: false })}>
                恢复
              </Button>
            ) : (
              <Button variant="link" size="sm" disabled={!canWrite} onClick={() => void patch(s, { paused: true })}>
                暂停
              </Button>
            )}
            <ConfirmAction
              title="确认重置 signing secret？"
              description="旧 secret 立即失效，新 secret 仅此一次展示。"
              trigger={
                <Button variant="link" size="sm" disabled={!canWrite}>
                  重置 secret
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
        title="Webhook"
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            新建订阅
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
            <DialogTitle>新建 Webhook 订阅</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="webhook-name">订阅名称</Label>
              <Input
                id="webhook-name"
                maxLength={255}
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              />
              {formError.name && <p className="text-sm text-destructive">{formError.name}</p>}
            </div>
            <div className="space-y-2">
              <Label htmlFor="webhook-url">回调地址（https://）</Label>
              <Input
                id="webhook-url"
                placeholder="https://example.com/hooks/loomvec"
                value={form.url}
                onChange={(e) => setForm((f) => ({ ...f, url: e.target.value }))}
              />
              {formError.url && <p className="text-sm text-destructive">{formError.url}</p>}
            </div>
            <div className="space-y-2">
              <Label>订阅事件</Label>
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
                            : f.eventTypes.filter((t) => t !== e),
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
              <Label htmlFor="webhook-desc">描述（可选）</Label>
              <Textarea
                id="webhook-desc"
                rows={2}
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              />
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

      {/* 删除订阅（危险操作）：理由入审计 */}
      <ReasonModal
        open={deleteTarget !== null}
        title={`删除 Webhook 订阅「${deleteTarget?.name ?? ''}」`}
        description="删除后该订阅立即失效，操作不可逆。"
        okText="确认删除"
        danger
        confirmLoading={busy}
        onCancel={() => setDeleteTarget(null)}
        onOk={(reason) => (deleteTarget ? remove(deleteTarget, reason) : Promise.resolve())}
      />

      <SecretModal
        open={secretValue !== null}
        title="Signing Secret（仅此一次展示）"
        fields={secretValue ? [{ label: 'secret', value: secretValue }] : []}
        onClose={() => setSecretValue(null)}
      />

      {deliverySub && (
        <DeliverySheet sub={deliverySub} canWrite={canWrite} onClose={() => setDeliverySub(null)} />
      )}
    </div>
  );
}
