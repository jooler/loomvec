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
import { SecretModal } from '@/components/ReasonModal';
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
import type { ApiKeyRow } from '@/types';
import { formatDateTime } from '@/utils';

const SCOPE_OPTIONS = [
  { value: 'read', label: 'read（读）' },
  { value: 'write', label: 'write（写）' },
];

const createSchema = z.object({
  name: z.string().min(1, '请输入名称'),
  scopes: z.array(z.string()).min(1, '至少选择一个 scope'),
  rate_limit_per_min: z.number({ message: '请输入限流（次/分钟）' }).min(1, '限流至少为 1'),
  expires_in_seconds: z.number().min(60, '有效期至少 60 秒').optional(),
});

type CreateValues = z.infer<typeof createSchema>;

/** API Key 管理（/open/api-keys）：签发（scopes 精确到权限点）、限流、过期、吊销（docs/04 §5.8）。 */
export function ApiKeysPage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();

  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [createdKey, setCreatedKey] = useState<ApiKeyRow | null>(null);
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: '', scopes: ['read'], rate_limit_per_min: 600 },
  });

  const { data, isFetching } = useQuery({
    queryKey: ['api-keys'],
    queryFn: () => unwrap<ApiKeyRow[]>(api.GET('/api/v1/api-keys')),
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['api-keys'] });

  const create = form.handleSubmit(async (v) => {
    setBusy(true);
    try {
      const created = await unwrap<ApiKeyRow>(
        api.POST('/api/v1/api-keys', {
          body: {
            name: v.name,
            scopes: v.scopes as ('read' | 'write')[],
            rate_limit_per_min: v.rate_limit_per_min,
            expires_in_seconds: v.expires_in_seconds ?? null,
          },
        }),
      );
      toast.success('API Key 已签发（明文仅此一次展示）');
      setCreateOpen(false);
      form.reset();
      setCreatedKey(created);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '签发失败');
    } finally {
      setBusy(false);
    }
  });

  const revoke = async (key: ApiKeyRow) => {
    try {
      await unwrap(
        api.DELETE('/api/v1/api-keys/{key_id}', { params: { path: { key_id: key.id } } }),
      );
      toast.success('已吊销');
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  const columns: ColumnDef<ApiKeyRow, unknown>[] = [
    { accessorKey: 'name', header: '名称' },
    {
      accessorKey: 'scopes',
      header: 'Scopes',
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-1">
          {row.original.scopes.map((s) => (
            <Badge key={s} variant="outline">
              {s}
            </Badge>
          ))}
        </div>
      ),
    },
    { accessorKey: 'rate_limit_per_min', header: '限流（次/分）' },
    {
      accessorKey: 'created_at',
      header: '创建时间',
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      accessorKey: 'expires_at',
      header: '过期时间',
      cell: ({ row }) =>
        row.original.expires_at ? formatDateTime(row.original.expires_at) : '永不过期',
    },
    {
      accessorKey: 'last_used_at',
      header: '最近使用',
      cell: ({ row }) =>
        row.original.last_used_at ? formatDateTime(row.original.last_used_at) : '从未使用',
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => (
        <ConfirmAction
          title="确认吊销该 API Key？"
          trigger={
            <Button variant="link" size="sm" className="text-destructive hover:text-destructive" disabled={!canWrite}>
              吊销
            </Button>
          }
          danger
          onConfirm={() => revoke(row.original)}
        />
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="API Key"
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            签发 API Key
          </Button>
        }
      />

      <DataTable columns={columns} data={data} loading={isFetching} />

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
            <DialogTitle>签发 API Key</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="api-key-name">名称</Label>
              <Input id="api-key-name" maxLength={255} {...form.register('name')} />
              {form.formState.errors.name && (
                <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label>权限点（scopes）</Label>
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
              <Label htmlFor="api-key-rate">限流（次/分钟）</Label>
              <Input
                id="api-key-rate"
                type="number"
                min={1}
                {...form.register('rate_limit_per_min', { valueAsNumber: true })}
              />
              {form.formState.errors.rate_limit_per_min && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.rate_limit_per_min.message}
                </p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="api-key-expires">有效期（秒，留空永不过期）</Label>
              <Input
                id="api-key-expires"
                type="number"
                min={60}
                {...form.register('expires_in_seconds', {
                  setValueAs: (v) => (v === '' || v == null ? undefined : Number(v)),
                })}
              />
              {form.formState.errors.expires_in_seconds && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.expires_in_seconds.message}
                </p>
              )}
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setCreateOpen(false);
                  form.reset();
                }}
                disabled={busy}
              >
                取消
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? '保存中…' : '签发'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <SecretModal
        open={createdKey !== null}
        title="API Key 签发成功"
        fields={createdKey?.key ? [{ label: 'API Key', value: createdKey.key }] : []}
        onClose={() => setCreatedKey(null)}
      />
    </div>
  );
}
