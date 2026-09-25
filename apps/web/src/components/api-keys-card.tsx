/**
 * 个人中心「API Key」卡片（P5 开放授权）：用户自助签发/查看/吊销绑定本人的
 * API Key（PAT），对外授权第三方应用而不暴露登录 JWT。
 * 明文 key 仅创建响应返回一次，经 SecretOnceDialog 展示并提示复制保存。
 */
import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { z } from 'zod';
import { api } from '@loomvec/sdk-ts';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';
import { Checkbox } from '@loomvec/ui/components/ui/checkbox';
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
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@loomvec/ui/components/ui/table';
import { t as tt } from '@/i18n';
import { extractApiError, formatDateTime } from '@/utils';

/** 我的 API Key 行（GET /api/v1/api-keys 收敛后仅含本人绑定条目）。 */
interface ApiKeyRow {
  id: string;
  name: string;
  scopes: string[];
  expires_at: string | null;
  created_at: string;
  last_used_at: string | null;
  user_id: string | null;
}

/** 创建响应：明文 key 仅此一次返回。 */
interface ApiKeyCreated extends ApiKeyRow {
  key: string;
}

// 模块级文案与 schema：main.tsx 已先初始化 i18n，import 阶段取值安全（同 admin 惯例）
const SCOPE_OPTIONS = [
  { value: 'read', label: tt('profile:apiKey.scopeRead') },
  { value: 'write', label: tt('profile:apiKey.scopeWrite') },
];

/** 有效期预设（天）；'' = 永不过期。面向用户给预设而非裸秒数输入。 */
const EXPIRY_PRESETS = ['', '30', '90', '365'];

const DAY_SECONDS = 86_400;

const createSchema = z.object({
  name: z.string().min(1, tt('profile:apiKey.nameRequired')),
  scopes: z.array(z.string()).min(1, tt('profile:apiKey.scopesRequired')),
  expiryDays: z.string(),
});

type CreateValues = z.infer<typeof createSchema>;

function SecretOnceDialog(props: { open: boolean; value: string; onClose: () => void }) {
  const { t } = useTranslation('profile');
  const copy = () => {
    void navigator.clipboard
      .writeText(props.value)
      .then(() => toast.success(t('apiKey.copied')))
      .catch(() => toast.error(t('apiKey.copyFailed')));
  };
  return (
    <Dialog open={props.open} onOpenChange={(o) => !o && props.onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('apiKey.issuedTitle')}</DialogTitle>
          <DialogDescription className="text-amber-600 dark:text-amber-400">
            {t('apiKey.warning')}
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2 rounded-md border bg-muted/40 px-3 py-2">
          <code className="min-w-0 flex-1 break-all font-mono text-sm">{props.value}</code>
          <Button variant="ghost" size="icon" className="size-7 shrink-0" onClick={copy}>
            <Copy />
          </Button>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={props.onClose}>
            {t('apiKey.acknowledge')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ApiKeysCard() {
  const { t } = useTranslation('profile');
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [createdKey, setCreatedKey] = useState<ApiKeyCreated | null>(null);
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: '', scopes: ['read'], expiryDays: '' },
  });

  const list = useQuery({
    queryKey: ['my-api-keys'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/api-keys');
      if (error) throw new Error(extractApiError(error, t('apiKey.loadFailed')));
      return data as unknown as ApiKeyRow[];
    },
  });

  const create = useMutation({
    mutationFn: async (v: CreateValues) => {
      const { data, error } = await api.POST('/api/v1/api-keys', {
        body: {
          name: v.name,
          scopes: v.scopes as ('read' | 'write')[],
          // 契约必填：UI 不暴露限流编辑，固定服务端默认值
          rate_limit_per_min: 600,
          expires_in_seconds: v.expiryDays === '' ? null : Number(v.expiryDays) * DAY_SECONDS,
        },
      });
      if (error) throw new Error(extractApiError(error, t('apiKey.createFailed')));
      return data as unknown as ApiKeyCreated;
    },
    onSuccess: (created) => {
      toast.success(t('apiKey.issuedToast'));
      setCreateOpen(false);
      form.reset();
      setCreatedKey(created);
      void queryClient.invalidateQueries({ queryKey: ['my-api-keys'] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const revoke = useMutation({
    mutationFn: async (keyId: string) => {
      const { error } = await api.DELETE('/api/v1/api-keys/{key_id}', {
        params: { path: { key_id: keyId } },
      });
      if (error) throw new Error(extractApiError(error, t('apiKey.revokeFailed')));
    },
    onSuccess: () => {
      toast.success(t('apiKey.revokedToast'));
      void queryClient.invalidateQueries({ queryKey: ['my-api-keys'] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div className="space-y-1">
            <CardTitle>{t('apiKey.title')}</CardTitle>
            <CardDescription>{t('apiKey.description')}</CardDescription>
          </div>
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            {t('apiKey.create')}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {list.isLoading ? (
          <div className="grid place-items-center py-8">
            <Spinner className="size-5 text-muted-foreground" />
          </div>
        ) : list.isError ? (
          <p className="text-sm text-destructive">{(list.error as Error).message}</p>
        ) : (list.data?.length ?? 0) === 0 ? (
          <EmptyState description={t('apiKey.empty')} />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('apiKey.nameLabel')}</TableHead>
                <TableHead>{t('apiKey.colScopes')}</TableHead>
                <TableHead>{t('apiKey.colCreatedAt')}</TableHead>
                <TableHead>{t('apiKey.colExpiresAt')}</TableHead>
                <TableHead>{t('apiKey.colLastUsed')}</TableHead>
                <TableHead className="w-16" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {(list.data ?? []).map((k) => (
                <TableRow key={k.id}>
                  <TableCell className="font-medium">{k.name}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {k.scopes.map((s) => (
                        <Badge key={s} variant="outline">
                          {s}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>{formatDateTime(k.created_at)}</TableCell>
                  <TableCell>
                    {k.expires_at ? formatDateTime(k.expires_at) : t('apiKey.neverExpires')}
                  </TableCell>
                  <TableCell>
                    {k.last_used_at ? formatDateTime(k.last_used_at) : t('apiKey.neverUsed')}
                  </TableCell>
                  <TableCell>
                    <ConfirmAction
                      title={t('apiKey.revokeConfirm')}
                      danger
                      onConfirm={() => revoke.mutateAsync(k.id)}
                      trigger={
                        <Button
                          variant="link"
                          size="sm"
                          className="text-destructive hover:text-destructive"
                        >
                          {t('apiKey.revoke')}
                        </Button>
                      }
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

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
            <DialogTitle>{t('apiKey.createTitle')}</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={form.handleSubmit((v) => create.mutate(v))}
            className="space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="api-key-name">{t('apiKey.nameLabel')}</Label>
              <Input
                id="api-key-name"
                maxLength={255}
                placeholder={t('apiKey.namePlaceholder')}
                {...form.register('name')}
              />
              {form.formState.errors.name && (
                <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label>{t('apiKey.scopesLabel')}</Label>
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
              <Label>{t('apiKey.expiryLabel')}</Label>
              <Select
                value={form.watch('expiryDays')}
                onValueChange={(v) => form.setValue('expiryDays', v)}
              >
                <SelectTrigger className="w-[200px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {EXPIRY_PRESETS.map((days) => (
                    <SelectItem key={days || 'never'} value={days}>
                      {days === '' ? t('apiKey.expiryNever') : t('apiKey.expiryDays', { days })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setCreateOpen(false);
                  form.reset();
                }}
                disabled={create.isPending}
              >
                {t('apiKey.cancel')}
              </Button>
              <Button type="submit" disabled={create.isPending}>
                {create.isPending ? t('apiKey.creating') : t('apiKey.create')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <SecretOnceDialog
        open={createdKey !== null}
        value={createdKey?.key ?? ''}
        onClose={() => setCreatedKey(null)}
      />
    </Card>
  );
}
