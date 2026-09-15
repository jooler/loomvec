import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
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
      toast.success('设置已保存');
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: async () => {
      const { error } = await api.DELETE('/api/v1/ops/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error('删除失败');
    },
    onSuccess: () => {
      toast.success('公共空间已删除（含内容与向量清理）');
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
          <CardTitle>空间设置</CardTitle>
          <CardDescription>空间标识（slug）与模型/分片预设创建时确定，不可修改。</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="name">空间名称</Label>
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>空间标识</Label>
              <p className="rounded-md border bg-muted/40 px-3 py-2 font-mono text-sm">
                {space.data?.slug}
              </p>
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="description">描述</Label>
            <Textarea
              id="description"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="flex items-center justify-between rounded-lg border p-3">
            <div className="space-y-0.5">
              <Label htmlFor="review_required">先审后见</Label>
              <p className="text-xs text-muted-foreground">
                开启后新资产审核通过才可见、才参与检索
              </p>
            </div>
            <Switch
              id="review_required"
              checked={reviewRequired}
              onCheckedChange={setReviewRequired}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="quota_storage">存储配额（字节，0 = 不限）</Label>
              <Input
                id="quota_storage"
                type="number"
                min={0}
                value={quotaStorage}
                onChange={(e) => setQuotaStorage(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="quota_files">文件数配额（0 = 不限）</Label>
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
              {save.isPending ? '保存中…' : '保存设置'}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">用量</CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList>
            <DescriptionItem label="已用存储">{formatBytes(usage.data?.storage_bytes ?? 0)}</DescriptionItem>
            <DescriptionItem label="资产数">{usage.data?.file_count ?? 0}</DescriptionItem>
            <DescriptionItem label="存储配额">
              {(usage.data?.quota_storage_bytes ?? 0) > 0
                ? formatBytes(usage.data?.quota_storage_bytes ?? 0)
                : '不限'}
            </DescriptionItem>
            <DescriptionItem label="文件数配额">
              {(usage.data?.quota_file_count ?? 0) > 0 ? usage.data?.quota_file_count : '不限'}
            </DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      <Card className="border-destructive/40">
        <CardHeader>
          <CardTitle className="text-base text-destructive">危险区</CardTitle>
          <CardDescription>
            删除公共空间将级联清理资产、语义单元、向量与对象存储，用户端的链接随之失效。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ConfirmAction
            trigger={
              <Button variant="destructive" size="sm">
                删除公共空间
              </Button>
            }
            title={`删除公共空间「${space.data?.name ?? ''}」？`}
            description="该操作不可恢复：资产、向量与对象存储将一并清理。"
            confirmText="删除"
            danger
            onConfirm={() => remove.mutate()}
          />
        </CardContent>
      </Card>
    </div>
  );
}
