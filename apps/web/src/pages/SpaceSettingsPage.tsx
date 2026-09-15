import { useEffect } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import {
  CHUNK_PRESETS,
  EMBEDDING_MODELS,
  extractApiError,
  formatBytes,
  percentOf,
} from '@/utils';
import { t } from '@/i18n';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { Progress } from '@loomvec/ui/components/ui/progress';
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
 * P2-WEB-01 空间设置：owner 可编辑（名称/描述/审核开关/模型预设/空间级配额），其余角色只读。
 * 含用量与配额进度展示；owner 可删除空间（危险操作：级联清理资产、向量与对象存储）。
 */

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const settingsSchema = z.object({
  name: z.string().min(1, t('spaces:nameRequired')),
  description: z.string().optional(),
  review_required: z.boolean(),
  embedding_model: z.string().optional(),
  chunk_preset: z.string().optional(),
  quota_storage_bytes: z.number({ error: t('spaces:numberRequired') }).min(0, t('spaces:numberMinInvalid')),
  quota_file_count: z.number({ error: t('spaces:numberRequired') }).min(0, t('spaces:numberMinInvalid')),
});

type SettingsValues = z.infer<typeof settingsSchema>;

export function SpaceSettingsPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const { t } = useTranslation('spaces');
  const queryClient = useQueryClient();
  const form = useForm<SettingsValues>({
    resolver: zodResolver(settingsSchema),
    defaultValues: {
      name: '',
      description: undefined,
      review_required: false,
      embedding_model: undefined,
      chunk_preset: undefined,
      quota_storage_bytes: 0,
      quota_file_count: 0,
    },
  });

  const space = useQuery({
    queryKey: ['space', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, t('member.loadSpaceFailed')));
      return data;
    },
  });

  const usage = useQuery({
    queryKey: ['space-usage', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/usage', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, t('settings.loadUsageFailed')));
      return data;
    },
  });

  useEffect(() => {
    const s = space.data;
    if (!s) return;
    form.reset({
      name: s.name,
      description: s.description ?? undefined,
      review_required: s.review_required,
      embedding_model: s.embedding_model ?? undefined,
      chunk_preset: s.chunk_preset ?? undefined,
      quota_storage_bytes: s.quota_storage_bytes,
      quota_file_count: s.quota_file_count,
    });
  }, [space.data, form]);

  const save = useMutation({
    mutationFn: async (values: SettingsValues) => {
      const { error } = await api.PATCH('/api/v1/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
        body: {
          name: values.name,
          description: values.description || null,
          review_required: values.review_required,
          embedding_model: values.embedding_model ?? null,
          chunk_preset: values.chunk_preset ?? null,
          quota_storage_bytes: values.quota_storage_bytes,
          quota_file_count: values.quota_file_count,
        },
      });
      if (error) throw new Error(extractApiError(error, t('settings.saveFailed')));
    },
    onSuccess: () => {
      toast.success(t('settings.saved'));
      void queryClient.invalidateQueries({ queryKey: ['space', spaceId] });
      void queryClient.invalidateQueries({ queryKey: ['spaces'] });
    },
    onError: (e) => toast.error(e.message),
  });

  // 危险操作：级联清理资产、向量与对象存储，不可恢复
  const removeSpace = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE('/api/v1/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, t('finderData.deleteFailed')));
    },
    onSuccess: () => {
      toast.success(t('settings.spaceDeleted'));
      void queryClient.invalidateQueries({ queryKey: ['spaces'] });
      navigate('/spaces');
    },
    onError: (e) => toast.error(e.message),
  });

  if (space.isLoading)
    return (
      <div className="grid place-items-center py-10">
        <Spinner className="size-5 text-muted-foreground" />
      </div>
    );
  if (space.isError)
    return (
      <Card>
        <CardContent>
          <p className="text-sm text-destructive">{(space.error as Error).message}</p>
        </CardContent>
      </Card>
    );
  const s = space.data;
  if (!s) return null;

  const isOwner = s.my_role === 'owner';
  const quotaProgressCls =
    '[&_[data-slot=progress-indicator]]:bg-destructive';

  return (
    <div className="space-y-4">
      <PageHeader title={t('settings.title')} />

      <Card>
        <CardContent className="space-y-6">
          <form
            onSubmit={form.handleSubmit((values) => save.mutate(values))}
            className="max-w-[640px] space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="space-name">{t('fieldName')}</Label>
              <Input
                id="space-name"
                maxLength={64}
                disabled={!isOwner}
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
                disabled={!isOwner}
                {...form.register('description')}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="space-review-required">{t('uploadReview')}</Label>
              <div>
                <Switch
                  id="space-review-required"
                  checked={form.watch('review_required')}
                  onCheckedChange={(v) => form.setValue('review_required', v)}
                  disabled={!isOwner}
                />
              </div>
              <p className="text-xs text-muted-foreground">{t('reviewHintShort')}</p>
            </div>

            <div className="space-y-2">
              <Label>{t('embeddingModelLabel')}</Label>
              <Select
                value={form.watch('embedding_model') ?? 'none'}
                onValueChange={(v) => form.setValue('embedding_model', v === 'none' ? undefined : v)}
                disabled={!isOwner}
              >
                <SelectTrigger className="w-[240px]">
                  <SelectValue placeholder={t('optionDefault')} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">{t('optionDefault')}</SelectItem>
                  {EMBEDDING_MODELS.map((m) => (
                    <SelectItem key={m} value={m}>
                      {t(`embeddingModel.${m}`, { defaultValue: m })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>{t('chunkPresetLabel')}</Label>
              <Select
                value={form.watch('chunk_preset') ?? 'none'}
                onValueChange={(v) => form.setValue('chunk_preset', v === 'none' ? undefined : v)}
                disabled={!isOwner}
              >
                <SelectTrigger className="w-[240px]">
                  <SelectValue placeholder={t('optionDefault')} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">{t('optionDefault')}</SelectItem>
                  {CHUNK_PRESETS.map((m) => (
                    <SelectItem key={m} value={m}>
                      {t(`chunkPreset.${m}`, { defaultValue: m })}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="flex flex-wrap gap-8">
              <div className="space-y-2">
                <Label htmlFor="quota-storage">{t('settings.quotaStorageLabel')}</Label>
                <Input
                  id="quota-storage"
                  type="number"
                  min={0}
                  className="w-[180px]"
                  disabled={!isOwner}
                  {...form.register('quota_storage_bytes', { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">{t('settings.quotaZeroHint')}</p>
                {form.formState.errors.quota_storage_bytes && (
                  <p className="text-sm text-destructive">
                    {form.formState.errors.quota_storage_bytes.message}
                  </p>
                )}
              </div>
              <div className="space-y-2">
                <Label htmlFor="quota-file-count">{t('settings.quotaFileCountLabel')}</Label>
                <Input
                  id="quota-file-count"
                  type="number"
                  min={0}
                  className="w-[180px]"
                  disabled={!isOwner}
                  {...form.register('quota_file_count', { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">{t('settings.quotaZeroHint')}</p>
                {form.formState.errors.quota_file_count && (
                  <p className="text-sm text-destructive">
                    {form.formState.errors.quota_file_count.message}
                  </p>
                )}
              </div>
            </div>

            {isOwner && (
              <Button type="submit" disabled={save.isPending}>
                {save.isPending ? t('action.saving') : t('settings.saveSettings')}
              </Button>
            )}
          </form>
          {!isOwner && (
            <p className="text-sm text-muted-foreground">{t('settings.ownerOnly')}</p>
          )}

          <Card className="max-w-[640px]">
            <CardHeader>
              <CardTitle>{t('settings.usageTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              {usage.isLoading ? (
                <Spinner className="size-4 text-muted-foreground" />
              ) : usage.data ? (
                <div className="space-y-4">
                  <div className="space-y-1.5">
                    <p className="text-sm">
                      {t('settings.storageLabel')}
                      {formatBytes(usage.data.storage_bytes)}
                      <span className="text-muted-foreground">
                        {' '}/{' '}
                        {usage.data.quota_storage_bytes > 0
                          ? formatBytes(usage.data.quota_storage_bytes)
                          : t('settings.unlimitedQuota')}
                      </span>
                    </p>
                    <Progress
                      value={percentOf(usage.data.storage_bytes, usage.data.quota_storage_bytes)}
                      className={
                        usage.data.quota_storage_bytes > 0 &&
                        percentOf(usage.data.storage_bytes, usage.data.quota_storage_bytes) >= 90
                          ? quotaProgressCls
                          : undefined
                      }
                    />
                  </div>
                  <div className="space-y-1.5">
                    <p className="text-sm">
                      {t('settings.fileCountLabel')}
                      {usage.data.file_count}
                      <span className="text-muted-foreground">
                        {' '}/{' '}
                        {usage.data.quota_file_count > 0 ? usage.data.quota_file_count : t('settings.unlimitedQuota')}
                      </span>
                    </p>
                    <Progress
                      value={percentOf(usage.data.file_count, usage.data.quota_file_count)}
                      className={
                        usage.data.quota_file_count > 0 &&
                        percentOf(usage.data.file_count, usage.data.quota_file_count) >= 90
                          ? quotaProgressCls
                          : undefined
                      }
                    />
                  </div>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">{t('settings.usageUnavailable')}</p>
              )}
            </CardContent>
          </Card>

          <DescriptionList cols={2} className="max-w-[640px]">
            <DescriptionItem label="Slug">{s.slug}</DescriptionItem>
            <DescriptionItem label={t('typeLabel')}>
              {s.space_type === 'personal' ? t('typePersonal') : t('typeSharedShort')}
            </DescriptionItem>
          </DescriptionList>

          {isOwner && (
            <div>
              <ConfirmAction
                trigger={
                  <Button variant="destructive" disabled={removeSpace.isPending}>
                    {t('settings.deleteSpace')}
                  </Button>
                }
                title={t('settings.deleteTitle')}
                description={t('settings.deleteDesc')}
                confirmText={t('action.delete')}
                danger
                onConfirm={() => removeSpace.mutate()}
              />
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
