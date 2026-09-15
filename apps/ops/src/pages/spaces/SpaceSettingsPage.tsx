import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { formatBytes } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';
import { Switch } from '@loomvec/ui/components/ui/switch';
import { Textarea } from '@loomvec/ui/components/ui/textarea';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DescriptionList, DescriptionItem } from '@loomvec/ui/components/description-list';

/**
 * 设置页签（运营端）：名称/描述/审核开关/空间级配额 + 危险区（删除空间）。
 * 嵌入模型与分片预设创建时已定，此处不提供修改（与用户端口径一致）。
 */

interface OpsSpaceDetail {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  review_required: boolean;
  embedding_model: string | null;
  chunk_preset: string | null;
  quota_storage_bytes: number;
  quota_file_count: number;
  created_at: string;
}

interface UsageInfo {
  storage_bytes: number;
  file_count: number;
  quota_storage_bytes: number;
  quota_file_count: number;
}

export function SpaceSettingsPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('spaces');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [reviewRequired, setReviewRequired] = useState(false);
  const [quotaStorage, setQuotaStorage] = useState('0');
  const [quotaFiles, setQuotaFiles] = useState('0');

  const space = useQuery({
    queryKey: ['ops-space', spaceId],
    queryFn: () =>
      unwrap<OpsSpaceDetail>(
        api.GET('/api/v1/ops/spaces/{space_id}', { params: { path: { space_id: spaceId! } } }),
      ),
  });

  const usage = useQuery({
    queryKey: ['ops-space-usage', spaceId],
    queryFn: () =>
      unwrap<UsageInfo>(
        api.GET('/api/v1/ops/spaces/{space_id}/usage', {
          params: { path: { space_id: spaceId! } },
        }),
      ),
  });

  useEffect(() => {
    if (space.data) {
      setName(space.data.name);
      setDescription(space.data.description ?? '');
      setReviewRequired(space.data.review_required);
      setQuotaStorage(String(space.data.quota_storage_bytes));
      setQuotaFiles(String(space.data.quota_file_count));
    }
  }, [space.data]);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['ops-space', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['ops-spaces'] });
  };

  const save = useMutation({
    mutationFn: async () => {
      await unwrap(
        api.PATCH('/api/v1/ops/spaces/{space_id}', {
          params: { path: { space_id: spaceId! } },
          body: {
            name,
            description: description || null,
            review_required: reviewRequired,
            quota_storage_bytes: Number(quotaStorage) || 0,
            quota_file_count: Number(quotaFiles) || 0,
          },
        }),
      );
    },
    onSuccess: () => {
      toast.success(t('settings.saved'));
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE('/api/v1/ops/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(t('finderData.deleteFailed'));
    },
    onSuccess: () => {
      toast.success(t('settings.spaceDeleted'));
      void queryClient.invalidateQueries({ queryKey: ['ops-spaces'] });
      navigate('/overview', { replace: true });
    },
    onError: (e) => toast.error(e.message),
  });

  if (space.isLoading) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-64 rounded-xl" />
      </div>
    );
  }
  if (space.isError) {
    return <p className="text-sm text-destructive">{(space.error as Error).message}</p>;
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>{t('settings.title')}</CardTitle>
          <CardDescription>{t('settings.titleDesc')}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="name">{t('settings.nameLabel')}</Label>
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>{t('settings.slugLabel')}</Label>
              <p className="rounded-md border bg-muted/40 px-3 py-2 font-mono text-sm">
                {space.data?.slug}
              </p>
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="description">{t('field.description')}</Label>
            <Textarea
              id="description"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="flex items-center justify-between rounded-lg border p-3">
            <div className="space-y-0.5">
              <Label htmlFor="review_required">{t('settings.reviewLabel')}</Label>
              <p className="text-xs text-muted-foreground">{t('settings.reviewHint')}</p>
            </div>
            <Switch
              id="review_required"
              checked={reviewRequired}
              onCheckedChange={setReviewRequired}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="quota_storage">{t('settings.storageQuotaLabel')}</Label>
              <Input
                id="quota_storage"
                type="number"
                min={0}
                value={quotaStorage}
                onChange={(e) => setQuotaStorage(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="quota_files">{t('settings.fileCountQuotaLabel')}</Label>
              <Input
                id="quota_files"
                type="number"
                min={0}
                value={quotaFiles}
                onChange={(e) => setQuotaFiles(e.target.value)}
              />
            </div>
          </div>
          <div className="flex justify-end">
            <Button size="sm" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? t('action.saving') : t('settings.save')}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t('settings.usageTitle')}</CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList>
            <DescriptionItem label={t('settings.usedStorage')}>
              {formatBytes(usage.data?.storage_bytes ?? 0)}
            </DescriptionItem>
            <DescriptionItem label={t('settings.assetCount')}>
              {usage.data?.file_count ?? 0}
            </DescriptionItem>
            <DescriptionItem label={t('settings.storageQuota')}>
              {(usage.data?.quota_storage_bytes ?? 0) > 0
                ? formatBytes(usage.data?.quota_storage_bytes ?? 0)
                : t('settings.unlimited')}
            </DescriptionItem>
            <DescriptionItem label={t('settings.fileCountQuota')}>
              {(usage.data?.quota_file_count ?? 0) > 0 ? usage.data?.quota_file_count : t('settings.unlimited')}
            </DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      <Card className="border-destructive/40">
        <CardHeader>
          <CardTitle className="text-base text-destructive">{t('settings.dangerZone')}</CardTitle>
          <CardDescription>{t('settings.dangerDesc')}</CardDescription>
        </CardHeader>
        <CardContent>
          <ConfirmAction
            trigger={
              <Button variant="destructive" size="sm">
                {t('settings.deleteSpace')}
              </Button>
            }
            title={t('settings.deleteTitle', { name: space.data?.name ?? '' })}
            description={t('settings.deleteDesc')}
            confirmText={t('action.delete')}
            danger
            onConfirm={() => remove.mutate()}
          />
        </CardContent>
      </Card>
    </div>
  );
}
