import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { Plus } from 'lucide-react';
import { z } from 'zod';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { t } from '@/i18n';
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

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const createSchema = z.object({
  name: z.string().min(1, t('overview:nameRequired')).max(255),
  description: z.string().max(2000).optional(),
  review_required: z.boolean(),
});

type CreateValues = z.infer<typeof createSchema>;

function CreateSpaceDialog(props: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { t } = useTranslation('overview');
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
      toast.success(t('spaceCreated'));
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
          <DialogTitle>{t('createSpace')}</DialogTitle>
          <DialogDescription>{t('createDesc')}</DialogDescription>
        </DialogHeader>
        <form onSubmit={form.handleSubmit((v) => create.mutate(v))} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="name">{t('nameLabel')}</Label>
            <Input id="name" placeholder={t('namePlaceholder')} {...form.register('name')} />
            {form.formState.errors.name && (
              <p className="text-sm text-destructive">{form.formState.errors.name.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="description">{t('field.description')}</Label>
            <Textarea id="description" rows={3} {...form.register('description')} />
          </div>
          <div className="flex items-center justify-between rounded-lg border p-3">
            <div className="space-y-0.5">
              <Label htmlFor="review_required">{t('reviewLabel')}</Label>
              <p className="text-xs text-muted-foreground">{t('reviewHint')}</p>
            </div>
            <Switch
              id="review_required"
              checked={form.watch('review_required')}
              onCheckedChange={(v) => form.setValue('review_required', v)}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => props.onOpenChange(false)}>
              {t('action.cancel')}
            </Button>
            <Button type="submit" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? t('creating') : t('action.create')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function OverviewPage() {
  const { t } = useTranslation('overview');
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
        title={t('title')}
        description={t('description')}
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <Plus /> {t('createSpace')}
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
        <EmptyState title={t('emptyTitle')} description={t('emptyDesc')} className="mt-10" />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {spaces.data?.map((s) => (
            <Link key={s.id} to={`/s/${s.id}/assets`} className="group">
              <Card className="h-full transition-colors group-hover:border-primary/50">
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base">
                    {s.name}
                    {s.review_required && (
                      <StatusBadge tone="amber">{t('reviewBadge')}</StatusBadge>
                    )}
                  </CardTitle>
                  {s.description && (
                    <CardDescription className="line-clamp-2">{s.description}</CardDescription>
                  )}
                </CardHeader>
                <CardContent className="text-sm text-muted-foreground">
                  <div className="flex flex-wrap gap-x-4 gap-y-1">
                    <span>{t('assetCount', { count: s.file_count })}</span>
                    <span>{t('visibleGroupCount', { count: s.group_count })}</span>
                    <span>{t('memberCount', { count: s.member_count })}</span>
                  </div>
                  <p className="mt-2 text-xs">
                    {t('createdAtSuffix', { date: new Date(s.created_at).toLocaleDateString() })}
                  </p>
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
