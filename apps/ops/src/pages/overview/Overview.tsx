import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { Plus } from 'lucide-react';
import { z } from 'zod';
import { toast } from 'sonner';
import { api, unwrap } from '@/api';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';
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
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';
import { Switch } from '@loomvec/ui/components/ui/switch';
import { Textarea } from '@loomvec/ui/components/ui/textarea';

/**
 * 运营端总览：公共空间卡片列表 + 创建入口。
 * 空间维护的完整界面在 /s/:spaceId（资产/审核/图谱/可见性/设置）。
 */

interface OpsSpaceItem {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  review_required: boolean;
  member_count: number;
  group_count: number;
  storage_bytes: number;
  file_count: number;
  created_at: string;
}

const createSchema = z.object({
  name: z.string().min(1, '请输入空间名称').max(255),
  description: z.string().max(2000).optional(),
  review_required: z.boolean(),
});

type CreateValues = z.infer<typeof createSchema>;

function CreateSpaceDialog(props: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: '', description: '', review_required: false },
  });

  const create = useMutation({
    mutationFn: async (values: CreateValues) => {
      const space = await unwrap<OpsSpaceItem>(
        api.POST('/api/v1/ops/spaces', {
          body: {
            name: values.name,
            description: values.description || null,
            review_required: values.review_required,
          },
        }),
      );
      return space;
    },
    onSuccess: (space) => {
      toast.success('公共空间已创建');
      void queryClient.invalidateQueries({ queryKey: ['ops-spaces'] });
      form.reset();
      props.onOpenChange(false);
      navigate(`/s/${space.id}/assets`);
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>创建公共空间</DialogTitle>
          <DialogDescription>
            公共空间由运营端创建与维护；创建后通过「可见性」勾选用户分组，用户端即可选择链接。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={form.handleSubmit((v) => create.mutate(v))} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="name">空间名称</Label>
            <Input id="name" placeholder="如：公司公共知识库" {...form.register('name')} />
            {form.formState.errors.name && (
              <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="description">描述</Label>
            <Textarea id="description" rows={3} {...form.register('description')} />
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
              checked={form.watch('review_required')}
              onCheckedChange={(v) => form.setValue('review_required', v)}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => props.onOpenChange(false)}>
              取消
            </Button>
            <Button type="submit" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? '创建中…' : '创建'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function OverviewPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [createOpen, setCreateOpen] = useState(searchParams.get('create') === '1');

  useEffect(() => {
    if (searchParams.get('create') === '1') {
      setCreateOpen(true);
      setSearchParams({}, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const spaces = useQuery({
    queryKey: ['ops-spaces'],
    queryFn: () =>
      unwrap<{ items: OpsSpaceItem[] }>(api.GET('/api/v1/ops/spaces')).then((r) => r.items),
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title="公共空间总览"
        description="维护公共知识库空间：上传与管理内容、配置分组可见性。点击空间进入管理界面。"
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <Plus /> 创建公共空间
          </Button>
        }
      />

      {spaces.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-36 rounded-xl" />
          ))}
        </div>
      ) : spaces.isError ? (
        <p className="text-sm text-destructive">{(spaces.error as Error).message}</p>
      ) : (spaces.data?.length ?? 0) === 0 ? (
        <EmptyState
          title="还没有公共空间"
          description="创建第一个公共空间，勾选可见分组后即可对用户端开放链接。"
          className="mt-10"
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {spaces.data?.map((s) => (
            <Link key={s.id} to={`/s/${s.id}/assets`} className="group">
              <Card className="h-full transition-colors group-hover:border-primary/50">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base">
                    {s.name}
                    {s.review_required && <StatusBadge tone="amber">需审核</StatusBadge>}
                  </CardTitle>
                  {s.description && (
                    <CardDescription className="line-clamp-2">{s.description}</CardDescription>
                  )}
                </CardHeader>
                <CardContent className="text-sm text-muted-foreground">
                  <div className="flex flex-wrap gap-x-4 gap-y-1">
                    <span>{s.file_count} 个资产</span>
                    <span>{s.group_count} 个可见分组</span>
                    <span>{s.member_count} 名成员</span>
                  </div>
                  <p className="mt-2 text-xs">{new Date(s.created_at).toLocaleDateString()} 创建</p>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}

      <CreateSpaceDialog open={createOpen} onOpenChange={setCreateOpen} />
    </div>
  );
}
