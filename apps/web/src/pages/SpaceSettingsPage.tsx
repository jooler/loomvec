import { useEffect } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import {
  CHUNK_PRESETS,
  EMBEDDING_MODELS,
  extractApiError,
  formatBytes,
  percentOf,
} from '@/utils';
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

const settingsSchema = z.object({
  name: z.string().min(1, '请输入名称'),
  description: z.string().optional(),
  review_required: z.boolean(),
  embedding_model: z.string().optional(),
  chunk_preset: z.string().optional(),
  quota_storage_bytes: z.number({ error: '请输入数字' }).min(0, '不能小于 0'),
  quota_file_count: z.number({ error: '请输入数字' }).min(0, '不能小于 0'),
});

type SettingsValues = z.infer<typeof settingsSchema>;

export function SpaceSettingsPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
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
      if (error) throw new Error(extractApiError(error, '加载空间失败'));
      return data;
    },
  });

  const usage = useQuery({
    queryKey: ['space-usage', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/usage', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载用量失败'));
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
      if (error) throw new Error(extractApiError(error, '保存失败'));
    },
    onSuccess: () => {
      toast.success('设置已保存');
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
      if (error) throw new Error(extractApiError(error, '删除失败'));
    },
    onSuccess: () => {
      toast.success('空间已删除');
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
      <PageHeader title="空间设置" />

      <Card>
        <CardContent className="space-y-6">
          <form
            onSubmit={form.handleSubmit((values) => save.mutate(values))}
            className="max-w-[640px] space-y-4"
          >
            <div className="space-y-2">
              <Label htmlFor="space-name">空间名称</Label>
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
              <Label htmlFor="space-description">描述</Label>
              <Textarea
                id="space-description"
                rows={2}
                disabled={!isOwner}
                {...form.register('description')}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="space-review-required">上传审核</Label>
              <div>
                <Switch
                  id="space-review-required"
                  checked={form.watch('review_required')}
                  onCheckedChange={(v) => form.setValue('review_required', v)}
                  disabled={!isOwner}
                />
              </div>
              <p className="text-xs text-muted-foreground">
                开启后新上传资产需审核通过才对查看者可见
              </p>
            </div>

            <div className="space-y-2">
              <Label>嵌入模型</Label>
              <Select
                value={form.watch('embedding_model') ?? 'none'}
                onValueChange={(v) => form.setValue('embedding_model', v === 'none' ? undefined : v)}
                disabled={!isOwner}
              >
                <SelectTrigger className="w-[240px]">
                  <SelectValue placeholder="默认" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">默认</SelectItem>
                  {EMBEDDING_MODELS.map((m) => (
                    <SelectItem key={m.value} value={m.value}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>分片预设</Label>
              <Select
                value={form.watch('chunk_preset') ?? 'none'}
                onValueChange={(v) => form.setValue('chunk_preset', v === 'none' ? undefined : v)}
                disabled={!isOwner}
              >
                <SelectTrigger className="w-[240px]">
                  <SelectValue placeholder="默认" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">默认</SelectItem>
                  {CHUNK_PRESETS.map((m) => (
                    <SelectItem key={m.value} value={m.value}>
                      {m.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="flex flex-wrap gap-8">
              <div className="space-y-2">
                <Label htmlFor="quota-storage">存储配额（字节）</Label>
                <Input
                  id="quota-storage"
                  type="number"
                  min={0}
                  className="w-[180px]"
                  disabled={!isOwner}
                  {...form.register('quota_storage_bytes', { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">0 = 不限额</p>
                {form.formState.errors.quota_storage_bytes && (
                  <p className="text-sm text-destructive">
                    {form.formState.errors.quota_storage_bytes.message}
                  </p>
                )}
              </div>
              <div className="space-y-2">
                <Label htmlFor="quota-file-count">文件数配额</Label>
                <Input
                  id="quota-file-count"
                  type="number"
                  min={0}
                  className="w-[180px]"
                  disabled={!isOwner}
                  {...form.register('quota_file_count', { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">0 = 不限额</p>
                {form.formState.errors.quota_file_count && (
                  <p className="text-sm text-destructive">
                    {form.formState.errors.quota_file_count.message}
                  </p>
                )}
              </div>
            </div>

            {isOwner && (
              <Button type="submit" disabled={save.isPending}>
                {save.isPending ? '保存中…' : '保存设置'}
              </Button>
            )}
          </form>
          {!isOwner && (
            <p className="text-sm text-muted-foreground">
              仅空间所有者可修改设置（当前只读）。
            </p>
          )}

          <Card className="max-w-[640px]">
            <CardHeader>
              <CardTitle>用量与配额</CardTitle>
            </CardHeader>
            <CardContent>
              {usage.isLoading ? (
                <Spinner className="size-4 text-muted-foreground" />
              ) : usage.data ? (
                <div className="space-y-4">
                  <div className="space-y-1.5">
                    <p className="text-sm">
                      存储：{formatBytes(usage.data.storage_bytes)}
                      <span className="text-muted-foreground">
                        {' '}/{' '}
                        {usage.data.quota_storage_bytes > 0
                          ? formatBytes(usage.data.quota_storage_bytes)
                          : '不限额'}
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
                      文件数：{usage.data.file_count}
                      <span className="text-muted-foreground">
                        {' '}/{' '}
                        {usage.data.quota_file_count > 0 ? usage.data.quota_file_count : '不限额'}
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
                <p className="text-sm text-muted-foreground">无法加载用量。</p>
              )}
            </CardContent>
          </Card>

          <DescriptionList cols={2} className="max-w-[640px]">
            <DescriptionItem label="Slug">{s.slug}</DescriptionItem>
            <DescriptionItem label="类型">
              {s.space_type === 'personal' ? '个人空间' : '共享空间'}
            </DescriptionItem>
          </DescriptionList>

          {isOwner && (
            <div>
              <ConfirmAction
                trigger={
                  <Button variant="destructive" disabled={removeSpace.isPending}>
                    删除空间
                  </Button>
                }
                title="删除整个空间？"
                description="将级联清理资产、向量与对象存储，不可恢复。"
                confirmText="删除"
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
