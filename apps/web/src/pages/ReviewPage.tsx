import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { Check, File, RefreshCw, X } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { extractApiError, formatBytes } from '@/utils';
import { t as sharedT } from '@/i18n';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { Button } from '@loomvec/ui/components/ui/button';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Card, CardContent } from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Label } from '@loomvec/ui/components/ui/label';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { Textarea } from '@loomvec/ui/components/ui/textarea';

/**
 * P2-WEB-05 审核页（owner）：待审队列 + 通过 / 驳回（理由必填，前后端双重拦截）。
 * 审核成功后刷新队列与通知（被审核者会收到结果通知）。
 */

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const rejectSchema = z.object({
  reason: z
    .string()
    .min(1, sharedT('review:reasonRequired'))
    .refine((v) => v.trim().length > 0, { message: sharedT('review:reasonBlank') }),
});

type RejectValues = z.infer<typeof rejectSchema>;

export function ReviewPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const queryClient = useQueryClient();
  const { t } = useTranslation('review');
  const [rejecting, setRejecting] = useState<{ assetId: string; name: string } | null>(null);
  const form = useForm<RejectValues>({
    resolver: zodResolver(rejectSchema),
    defaultValues: { reason: '' },
  });

  const queue = useQuery({
    queryKey: ['review-queue', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/review/queue', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, t('loadQueueFailed')));
      return data.items;
    },
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['review-queue', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['notifications'] });
    void queryClient.invalidateQueries({ queryKey: ['assets'] });
  };

  const decide = useMutation({
    mutationFn: async (vars: { assetId: string; action: 'approve' | 'reject'; reason?: string }) => {
      const { error } = await api.POST('/api/v1/assets/{asset_id}/review', {
        params: { path: { asset_id: vars.assetId } },
        body: { action: vars.action, reason: vars.reason || undefined },
      });
      if (error) throw new Error(extractApiError(error, t('decideFailed')));
    },
    onSuccess: (_, vars) => {
      toast.success(vars.action === 'approve' ? t('reviewStatus.approved') : t('reviewStatus.rejected'));
      setRejecting(null);
      form.reset();
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const openReject = (assetId: string, name: string) => {
    form.reset();
    setRejecting({ assetId, name });
  };

  // 前端先拦截空理由（后端 422 兜底）
  const submitReject = form.handleSubmit((values) => {
    if (!rejecting) return;
    decide.mutate({ assetId: rejecting.assetId, action: 'reject', reason: values.reason.trim() });
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('title')}
        actions={
          <Button variant="outline" onClick={() => queue.refetch()}>
            <RefreshCw /> {t('action.refresh')}
          </Button>
        }
      />

      <Card>
        <CardContent>
          {queue.isLoading ? (
            <div className="grid place-items-center py-10">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : queue.isError ? (
            <p className="text-sm text-destructive">{(queue.error as Error).message}</p>
          ) : (queue.data?.length ?? 0) === 0 ? (
            <EmptyState title={t('emptyQueue')} />
          ) : (
            <div className="divide-y">
              {(queue.data ?? []).map((item) => (
                <div key={item.id} className="flex flex-wrap items-start justify-between gap-3 py-4">
                  <div className="flex min-w-0 items-start gap-3">
                    <File className="mt-0.5 size-7 shrink-0 text-muted-foreground" />
                    <div className="min-w-0 space-y-1">
                      <p className="text-sm font-medium">{item.name}</p>
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                        <Badge variant="outline">{item.mime_type}</Badge>
                        <span className="text-sm text-muted-foreground">
                          {formatBytes(item.size_bytes)}
                        </span>
                        <span className="text-sm text-muted-foreground">
                          {t('submitterLabel')}
                          {item.created_by ?? '-'}
                        </span>
                        <span className="text-sm text-muted-foreground">
                          {new Date(item.created_at).toLocaleString()}
                        </span>
                        {item.review_reason && (
                          <span className="text-sm text-amber-600">
                            {t('remarkLabel')}
                            {item.review_reason}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    <Button
                      size="sm"
                      disabled={decide.isPending && decide.variables?.assetId === item.id}
                      onClick={() => decide.mutate({ assetId: item.id, action: 'approve' })}
                    >
                      <Check /> {t('approve')}
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      onClick={() => openReject(item.id, item.name)}
                    >
                      <X /> {t('reject')}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={rejecting !== null}
        onOpenChange={(open) => {
          if (!open) setRejecting(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('rejectTitle', { name: rejecting?.name ?? '' })}</DialogTitle>
          </DialogHeader>
          <form onSubmit={submitReject} className="space-y-2">
            <Label htmlFor="reject-reason">{t('reasonLabel')}</Label>
            <Textarea
              id="reject-reason"
              rows={3}
              placeholder={t('reasonPlaceholder')}
              {...form.register('reason')}
            />
            {form.formState.errors.reason && (
              <p className="text-sm text-destructive">{form.formState.errors.reason.message}</p>
            )}
            <DialogFooter className="pt-2">
              <Button type="button" variant="outline" onClick={() => setRejecting(null)}>
                {t('action.cancel')}
              </Button>
              <Button type="submit" variant="destructive" disabled={decide.isPending}>
                {decide.isPending ? t('action.submitting') : t('confirmReject')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
