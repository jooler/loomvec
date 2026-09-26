import { useEffect, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { Plus, RefreshCw } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate, Link } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { useMySpaces, usePublicSpaces, useSpaceLinkToggle } from '@/hooks';
import { t } from '@/i18n';
import { CHUNK_PRESETS, EMBEDDING_MODELS, extractApiError, formatBytes, ROLE_TONE } from '@/utils';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
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
import { Switch } from '@loomvec/ui/components/ui/switch';
import { Textarea } from '@loomvec/ui/components/ui/textarea';

/**
 * P2-WEB-01 空间管理：我的空间卡片（角色/审核/成员数）+ 创建空间向导。
 * P5「公共空间」区：可点击进入只读浏览；链接开关仅控制问答检索源。
 */

/** Radix Select 不允许空串 value：此哨兵表示「默认（不指定）」。 */
const UNSET = '__unset__';

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const createSchema = z.object({
  name: z.string().min(1, t('spaces:nameRequired')),
  description: z.string().optional(),
  space_type: z.enum(['shared', 'personal']),
  review_required: z.boolean(),
  embedding_model: z.string(),
  chunk_preset: z.string(),
});

type CreateValues = z.infer<typeof createSchema>;

function CreateSpaceModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { t } = useTranslation('spaces');
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: {
      name: '',
      description: '',
      space_type: 'shared',
      review_required: false,
      embedding_model: UNSET,
      chunk_preset: UNSET,
    },
  });

  // destroyOnHidden 语义：关闭时重置表单
  useEffect(() => {
    if (!open) form.reset();
  }, [open, form]);

  const create = useMutation({
    mutationFn: async (values: CreateValues) => {
      const { data, error } = await api.POST('/api/v1/spaces', {
        body: {
          name: values.name,
          description: values.description || undefined,
          space_type: values.space_type,
          review_required: values.review_required,
          embedding_model: values.embedding_model === UNSET ? undefined : values.embedding_model,
          chunk_preset: values.chunk_preset === UNSET ? undefined : values.chunk_preset,
        },
      });
      if (error) throw new Error(extractApiError(error, t('createFailed')));
      return data;
    },
    onSuccess: (space) => {
      toast.success(t('created', { name: space.name }));
      void queryClient.invalidateQueries({ queryKey: ['spaces'] });
      onClose();
      navigate(`/s/${space.id}/assets`);
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('createSpace')}</DialogTitle>
        </DialogHeader>
        <form onSubmit={form.handleSubmit((v) => create.mutate(v))} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="space-name">{t('fieldName')}</Label>
            <Input
              id="space-name"
              placeholder={t('namePlaceholder')}
              maxLength={64}
              {...form.register('name')}
            />
            {form.formState.errors.name && (
              <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="space-description">{t('field.description')}</Label>
            <Textarea
              id="space-description"
              rows={2}
              placeholder={t('descriptionPlaceholder')}
              {...form.register('description')}
            />
          </div>

          <div className="space-y-2">
            <Label>{t('spaceType')}</Label>
            <Select
              value={form.watch('space_type')}
              onValueChange={(v) => form.setValue('space_type', v as CreateValues['space_type'])}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="shared">{t('typeShared')}</SelectItem>
                <SelectItem value="personal">{t('typePersonal')}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Switch
                id="space-review"
                checked={form.watch('review_required')}
                onCheckedChange={(v) => form.setValue('review_required', v)}
              />
              <Label htmlFor="space-review">{t('uploadReview')}</Label>
            </div>
            <p className="text-xs text-muted-foreground">{t('reviewHint')}</p>
          </div>

          <div className="space-y-2">
            <Label>{t('embeddingModelLabel')}</Label>
            <Select
              value={form.watch('embedding_model')}
              onValueChange={(v) => form.setValue('embedding_model', v)}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder={t('optionDefault')} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={UNSET}>{t('optionDefault')}</SelectItem>
                {EMBEDDING_MODELS.map((m) => (
                  <SelectItem key={m} value={m}>
                    {t(`embeddingModel.${m}`, { defaultValue: m })}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{t('leaveBlankHint')}</p>
          </div>

          <div className="space-y-2">
            <Label>{t('chunkPresetLabel')}</Label>
            <Select
              value={form.watch('chunk_preset')}
              onValueChange={(v) => form.setValue('chunk_preset', v)}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder={t('optionDefault')} />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={UNSET}>{t('optionDefault')}</SelectItem>
                {CHUNK_PRESETS.map((p) => (
                  <SelectItem key={p} value={p}>
                    {t(`chunkPreset.${p}`, { defaultValue: p })}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">{t('leaveBlankHint')}</p>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t('action.cancel')}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? t('action.saving') : t('ok')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function SpacesPage() {
  const { t } = useTranslation('spaces');
  const spaces = useMySpaces();
  const publicSpaces = usePublicSpaces();
  const linkToggle = useSpaceLinkToggle();
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('mySpaces')}</CardTitle>
          <CardAction className="flex items-center gap-2">
            <Button variant="outline" onClick={() => spaces.refetch()}>
              <RefreshCw /> {t('action.refresh')}
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus /> {t('createSpace')}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          {spaces.isLoading ? (
            <div className="grid place-items-center py-10">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : spaces.isError ? (
            <p className="text-sm text-destructive">{(spaces.error as Error).message}</p>
          ) : (spaces.data?.length ?? 0) === 0 ? (
            <EmptyState description={t('emptyMySpaces')} />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(spaces.data ?? []).map((s) => {
                const role = s.my_role ?? '';
                return (
                  <Card key={s.id} className="gap-3 py-4 transition-shadow hover:shadow-md">
                    <CardHeader>
                      <CardTitle className="truncate">
                        <Link
                          to={`/s/${s.id}/assets`}
                          className="text-inherit hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        >
                          {s.name}
                        </Link>
                      </CardTitle>
                      <CardAction>
                        <StatusBadge tone={ROLE_TONE[role]}>
                          {t(`role.${role}`, { defaultValue: role })}
                        </StatusBadge>
                      </CardAction>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">
                        {s.description ?? t('noDescription')}
                      </p>
                      <div className="flex flex-wrap items-center gap-1">
                        {s.review_required && <StatusBadge tone="amber">{t('reviewRequired')}</StatusBadge>}
                        {s.space_type === 'personal' && (
                          <Badge variant="outline">{t('personal')}</Badge>
                        )}
                        <Badge variant="outline">
                          {t('memberCount', { count: s.member_count ?? '-' })}
                        </Badge>
                        <Badge variant="outline">
                          {t('storageQuota')}{' '}
                          {s.quota_storage_bytes > 0 ? formatBytes(s.quota_storage_bytes) : t('unlimited')}
                        </Badge>
                      </div>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('publicSpaces')}</CardTitle>
          <CardAction>
            <Button variant="outline" onClick={() => publicSpaces.refetch()}>
              <RefreshCw /> {t('action.refresh')}
            </Button>
          </CardAction>
          <CardDescription>{t('publicSpacesDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          {publicSpaces.isLoading ? (
            <div className="grid place-items-center py-10">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : publicSpaces.isError ? (
            <p className="text-sm text-destructive">{(publicSpaces.error as Error).message}</p>
          ) : (publicSpaces.data?.length ?? 0) === 0 ? (
            <EmptyState description={t('emptyPublicSpaces')} />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(publicSpaces.data ?? []).map((s) => (
                <Card key={s.id} className="gap-3 py-4 transition-shadow hover:shadow-md">
                  <CardHeader>
                    <CardTitle className="truncate">
                      <Link
                        to={`/s/${s.id}/assets`}
                        className="text-inherit hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      >
                        {s.name}
                      </Link>
                    </CardTitle>
                    <CardAction>
                      <StatusBadge tone="purple">{t('publicBadge')}</StatusBadge>
                    </CardAction>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">
                      {s.description ?? t('noDescription')}
                    </p>
                    <p className="text-xs text-muted-foreground">{t('browseHint')}</p>
                    <div className="flex items-center justify-between rounded-lg border p-2.5">
                      <div className="space-y-0.5">
                        <Label htmlFor={`link-${s.id}`} className="text-sm">
                          {t('linkToSpace')}
                        </Label>
                        <p className="text-xs text-muted-foreground">
                          {s.linked ? t('linkedState') : t('notLinked')}
                        </p>
                      </div>
                      <Switch
                        id={`link-${s.id}`}
                        checked={s.linked}
                        disabled={linkToggle.isPending}
                        onCheckedChange={(v) =>
                          linkToggle.mutate(
                            { spaceId: s.id, linked: v },
                            {
                              onSuccess: () =>
                                toast.success(
                                  v ? t('linkedToast', { name: s.name }) : t('unlinkedToast', { name: s.name }),
                                ),
                            },
                          )
                        }
                      />
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <CreateSpaceModal open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}
