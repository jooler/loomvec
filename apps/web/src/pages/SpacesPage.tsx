import { useEffect, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { Plus, RefreshCw } from 'lucide-react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useMySpaces } from '@/hooks';
import { CHUNK_PRESETS, EMBEDDING_MODELS, extractApiError, formatBytes, ROLE_META, ROLE_TONE } from '@/utils';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Card,
  CardAction,
  CardContent,
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
 */

/** Radix Select 不允许空串 value：此哨兵表示「默认（不指定）」。 */
const UNSET = '__unset__';

const createSchema = z.object({
  name: z.string().min(1, '请输入名称'),
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
      if (error) throw new Error(extractApiError(error, '创建空间失败'));
      return data;
    },
    onSuccess: (space) => {
      toast.success(`空间「${space.name}」已创建`);
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
          <DialogTitle>创建空间</DialogTitle>
        </DialogHeader>
        <form onSubmit={form.handleSubmit((v) => create.mutate(v))} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="space-name">空间名称</Label>
            <Input
              id="space-name"
              placeholder="例如：产品知识库"
              maxLength={64}
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
              placeholder="空间用途说明（可选）"
              {...form.register('description')}
            />
          </div>

          <div className="space-y-2">
            <Label>空间类型</Label>
            <Select
              value={form.watch('space_type')}
              onValueChange={(v) => form.setValue('space_type', v as CreateValues['space_type'])}
            >
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="shared">共享空间（可邀请成员协作）</SelectItem>
                <SelectItem value="personal">个人空间</SelectItem>
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
              <Label htmlFor="space-review">上传审核</Label>
            </div>
            <p className="text-xs text-muted-foreground">
              开启后新上传的资产需所有者审核通过后才对查看者可见
            </p>
          </div>

          <div className="space-y-2">
            <Label>嵌入模型</Label>
            <Select
              value={form.watch('embedding_model')}
              onValueChange={(v) => form.setValue('embedding_model', v)}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="默认" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={UNSET}>默认</SelectItem>
                {EMBEDDING_MODELS.map((m) => (
                  <SelectItem key={m.value} value={m.value}>
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">留空使用租户默认配置</p>
          </div>

          <div className="space-y-2">
            <Label>分片预设</Label>
            <Select
              value={form.watch('chunk_preset')}
              onValueChange={(v) => form.setValue('chunk_preset', v)}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="默认" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={UNSET}>默认</SelectItem>
                {CHUNK_PRESETS.map((p) => (
                  <SelectItem key={p.value} value={p.value}>
                    {p.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">留空使用租户默认配置</p>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              取消
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? '保存中…' : '确定'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function SpacesPage() {
  const navigate = useNavigate();
  const spaces = useMySpaces();
  const [createOpen, setCreateOpen] = useState(false);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>我的空间</CardTitle>
          <CardAction className="flex items-center gap-2">
            <Button variant="outline" onClick={() => spaces.refetch()}>
              <RefreshCw /> 刷新
            </Button>
            <Button onClick={() => setCreateOpen(true)}>
              <Plus /> 创建空间
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
            <EmptyState description="还没有空间，点击右上角「创建空间」开始" />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {(spaces.data ?? []).map((s) => {
                const roleMeta = ROLE_META[s.my_role ?? ''];
                return (
                  <Card
                    key={s.id}
                    className="cursor-pointer gap-3 py-4 transition-shadow hover:shadow-md"
                    onClick={() => navigate(`/s/${s.id}/assets`)}
                  >
                    <CardHeader>
                      <CardTitle className="truncate">{s.name}</CardTitle>
                      <CardAction>
                        <StatusBadge tone={ROLE_TONE[s.my_role ?? '']}>
                          {roleMeta?.text ?? s.my_role}
                        </StatusBadge>
                      </CardAction>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">
                        {s.description ?? '（无描述）'}
                      </p>
                      <div className="flex flex-wrap items-center gap-1">
                        {s.review_required && <StatusBadge tone="amber">需审核</StatusBadge>}
                        {s.space_type === 'personal' && (
                          <Badge variant="outline">个人</Badge>
                        )}
                        <Badge variant="outline">{s.member_count ?? '-'} 名成员</Badge>
                        <Badge variant="outline">
                          存储配额{' '}
                          {s.quota_storage_bytes > 0 ? formatBytes(s.quota_storage_bytes) : '不限'}
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
      <CreateSpaceModal open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}
