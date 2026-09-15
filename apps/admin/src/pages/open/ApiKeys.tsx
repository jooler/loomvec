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
import { SecretModal } from '@/components/SecretModal';
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

// 模块级文案（scope 选项）：main.tsx 已先初始化 i18n，import 阶段取值安全
const SCOPE_OPTIONS = [
  { value: 'read', label: tt('open:apiKey.scopeOption.read') },
  { value: 'write', label: tt('open:apiKey.scopeOption.write') },
];

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const createSchema = z.object({
  name: z.string().min(1, tt('open:apiKey.nameRequired')),
  scopes: z.array(z.string()).min(1, tt('open:apiKey.scopesRequired')),
  rate_limit_per_min: z
    .number({ message: tt('open:apiKey.rateRequired') })
    .min(1, tt('open:apiKey.rateMin')),
  expires_in_seconds: z.number().min(60, tt('open:apiKey.expiresMin')).optional(),
});

type CreateValues = z.infer<typeof createSchema>;

/** API Key 管理（/open/api-keys）：签发（scopes 精确到权限点）、限流、过期、吊销（docs/04 §5.8）。 */
export function ApiKeysPage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('open');

  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [createdKey, setCreatedKey] = useState<ApiKeyRow | null>(null);
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: '', scopes: ['read'], rate_limit_per_min: 600 },
  });

  const { data, isFetching, isError, error } = useQuery({
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
      toast.success(t('apiKey.issuedToast'));
      setCreateOpen(false);
      form.reset();
      setCreatedKey(created);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('apiKey.issueFailed'));
    } finally {
      setBusy(false);
    }
  });

  const revoke = async (key: ApiKeyRow) => {
    try {
      await unwrap(
        api.DELETE('/api/v1/api-keys/{key_id}', { params: { path: { key_id: key.id } } }),
      );
      toast.success(t('apiKey.revokedToast'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  const columns: ColumnDef<ApiKeyRow, unknown>[] = [
    { accessorKey: 'name', header: t('field.name') },
    {
      accessorKey: 'scopes',
      header: t('apiKey.col.scopes'),
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
    { accessorKey: 'rate_limit_per_min', header: t('apiKey.col.rateLimit') },
    {
      accessorKey: 'created_at',
      header: t('field.createdAt'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      accessorKey: 'expires_at',
      header: t('apiKey.col.expiresAt'),
      cell: ({ row }) =>
        row.original.expires_at ? formatDateTime(row.original.expires_at) : t('apiKey.neverExpires'),
    },
    {
      accessorKey: 'last_used_at',
      header: t('apiKey.col.lastUsedAt'),
      cell: ({ row }) =>
        row.original.last_used_at ? formatDateTime(row.original.last_used_at) : t('apiKey.neverUsed'),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => (
        <ConfirmAction
          title={t('apiKey.revokeConfirm')}
          trigger={
            <Button variant="link" size="sm" className="text-destructive hover:text-destructive" disabled={!canWrite}>
              {t('apiKey.revoke')}
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
        title={t('apiKey.title')}
        actions={
          <Button disabled={!canWrite} onClick={() => setCreateOpen(true)}>
            {t('apiKey.issue')}
          </Button>
        }
      />

      <DataTable
        columns={columns}
        data={data}
        loading={isFetching}
        error={isError ? error : undefined}
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
            <DialogTitle>{t('apiKey.issueTitle')}</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="api-key-name">{t('field.name')}</Label>
              <Input id="api-key-name" maxLength={255} {...form.register('name')} />
              {form.formState.errors.name && (
                <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label>{t('apiKey.scopeLabel')}</Label>
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
              <Label htmlFor="api-key-rate">{t('apiKey.rateLabel')}</Label>
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
              <Label htmlFor="api-key-expires">{t('apiKey.expiresLabel')}</Label>
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
                {t('action.cancel')}
              </Button>
              <Button type="submit" disabled={busy}>
                {busy ? t('action.saving') : t('apiKey.issueButton')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <SecretModal
        open={createdKey !== null}
        title={t('apiKey.issuedTitle')}
        fields={createdKey?.key ? [{ label: t('apiKey.keyLabel'), value: createdKey.key }] : []}
        onClose={() => setCreatedKey(null)}
      />
    </div>
  );
}
