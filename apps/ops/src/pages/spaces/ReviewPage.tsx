import { useState } from 'react';
import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import { Check, File, RefreshCw, X } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router';
import { toast } from 'sonner';
import { api, unwrap } from '@/api';
import { extractApiError, formatBytes } from '@/utils';
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
 * 审核页签（运营端，先审后见空间）：待审队列 + 通过 / 驳回（理由必填）。
 * 与用户端审核页同链路（POST /assets/{id}/review），运营者以 owner 成员身份操作。
 */

const rejectSchema = z.object({
  reason: z
    .string()
    .min(1, '请填写驳回理由')
    .refine((v) => v.trim().length > 0, { message: '理由不能为空白' }),
});

type RejectValues = z.infer<typeof rejectSchema>;

interface QueueItem {
  id: string;
  name: string;
  mime_type: string;
  size_bytes: number;
  created_by: string | null;
  created_at: string;
  review_reason: string | null;
}

export function ReviewPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const queryClient = useQueryClient();
  const [rejecting, setRejecting] = useState<{ assetId: string; name: string } | null>(null);
  const form = useForm<RejectValues>({
    resolver: zodResolver(rejectSchema),
    defaultValues: { reason: '' },
  });

  const queue = useQuery({
    queryKey: ['ops-review-queue', spaceId],
    queryFn: () =>
      unwrap<{ items: QueueItem[] }>(
        api.GET('/api/v1/spaces/{space_id}/review/queue', {
          params: { path: { space_id: spaceId! } },
        }),
      ).then((r) => r.items),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['ops-review-queue', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['ops-assets', spaceId] });
  };

  const decide = useMutation({
    mutationFn: async (vars: { assetId: string; action: 'approve' | 'reject'; reason?: string }) => {
      const { error } = await api.POST('/api/v1/assets/{asset_id}/review', {
        params: { path: { asset_id: vars.assetId } },
        body: { action: vars.action, reason: vars.reason || undefined },
      });
      if (error) throw new Error(extractApiError(error, '审核操作失败'));
    },
    onSuccess: (_, vars) => {
      toast.success(vars.action === 'approve' ? '已通过' : '已驳回');
      setRejecting(null);
      form.reset();
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const submitReject = form.handleSubmit((values) => {
    if (!rejecting) return;
    decide.mutate({ assetId: rejecting.assetId, action: 'reject', reason: values.reason.trim() });
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title="审核队列"
        actions={
          <Button variant="outline" onClick={() => queue.refetch()}>
            <RefreshCw /> 刷新
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
            <EmptyState title="没有待审核的资产" />
          ) : (
            <div className="divide-y">
              {queue.data?.map((item) => (
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
                          {new Date(item.created_at).toLocaleString()}
                        </span>
                        {item.review_reason && (
                          <span className="text-sm text-amber-600">备注：{item.review_reason}</span>
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
                      <Check /> 通过
                    </Button>
                    <Button
                      size="sm"
                      variant="destructive"
                      onClick={() => {
                        form.reset();
                        setRejecting({ assetId: item.id, name: item.name });
                      }}
                    >
                      <X /> 驳回
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
            <DialogTitle>驳回「{rejecting?.name}」</DialogTitle>
          </DialogHeader>
          <form onSubmit={submitReject} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="reason">驳回理由（必填，将通知上传者）</Label>
              <Textarea id="reason" rows={3} {...form.register('reason')} />
              {form.formState.errors.reason && (
                <p className="text-sm text-destructive">{form.formState.errors.reason.message}</p>
              )}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setRejecting(null)}>
                取消
              </Button>
              <Button type="submit" variant="destructive" disabled={decide.isPending}>
                {decide.isPending ? '提交中…' : '确认驳回'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
