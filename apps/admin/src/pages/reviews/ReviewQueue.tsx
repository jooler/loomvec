import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Checkbox } from '@loomvec/ui/components/ui/checkbox';
import { Input } from '@loomvec/ui/components/ui/input';
import { formatBytes, formatDateTime } from '@/utils';
import type { PagedResp, ReviewAsset } from '@/types';

const PAGE_SIZE = 20;

/** 内容审核：跨空间待审队列 / 批量通过驳回 / 下架 / chunk 复核入口（docs/04 §5.5）。 */
export function ReviewQueuePage() {
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('reviews');

  const [spaceId, setSpaceId] = useState('');
  const [draftSpaceId, setDraftSpaceId] = useState('');
  const [page, setPage] = useState(1);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [rejectTarget, setRejectTarget] = useState<ReviewAsset | null>(null);
  const [batchRejectOpen, setBatchRejectOpen] = useState(false);
  const [takedownTarget, setTakedownTarget] = useState<ReviewAsset | null>(null);
  const [busy, setBusy] = useState(false);

  const limit = PAGE_SIZE;
  const offset = (page - 1) * PAGE_SIZE;

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-reviews', spaceId, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<ReviewAsset>>(
        api.GET('/api/v1/admin/reviews', {
          params: {
            query: { space_id: spaceId || undefined, limit, offset },
          },
        }),
      ),
  });

  const items = data?.items ?? [];
  const pageIds = items.map((i) => i.id);
  const allChecked = pageIds.length > 0 && pageIds.every((id) => selectedKeys.includes(id));
  const someChecked = pageIds.some((id) => selectedKeys.includes(id));

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['admin-reviews'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
  };

  /** 审核决定（approve/reject）：驳回理由必填并通知 owner。 */
  const decide = async (assetId: string, action: 'approve' | 'reject', reason?: string) => {
    await unwrap(
      api.POST('/api/v1/admin/reviews/{asset_id}/decide', {
        params: { path: { asset_id: assetId } },
        body: { action, reason: reason ?? null },
      }),
    );
    // 决定成功即出队：同步从选中集移除，避免随后的批量操作携带已决定资产
    setSelectedKeys((prev) => prev.filter((id) => id !== assetId));
    invalidate();
  };

  /** 下架（连带清理）：rejected + 软删 + 向量删除。 */
  const takedown = async (reason: string) => {
    if (!takedownTarget) return;
    setBusy(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/assets/{asset_id}/takedown', {
          params: { path: { asset_id: takedownTarget.id } },
          body: { reason },
        }),
      );
      toast.success(t('takedownToast'));
      setSelectedKeys((prev) => prev.filter((id) => id !== takedownTarget.id));
      setTakedownTarget(null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  /** 批量重跑：任务化，进度经任务看板跟踪。 */
  const batchRerun = async () => {
    try {
      const resp = await unwrap<{ accepted: number }>(
        api.POST('/api/v1/admin/assets/batch-rerun', {
          body: { asset_ids: selectedKeys, from_step: 'parse' },
        }),
      );
      toast.success(t('rerunAcceptedToast', { count: resp.accepted }));
      setSelectedKeys([]);
      void queryClient.invalidateQueries({ queryKey: ['admin-pipeline-jobs'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  const columns: ColumnDef<ReviewAsset, unknown>[] = [
    {
      id: 'select',
      header: () => (
        <Checkbox
          aria-label={t('chunks.selectAllPage')}
          checked={allChecked ? true : someChecked ? 'indeterminate' : false}
          onCheckedChange={(v) => {
            setSelectedKeys((prev) =>
              v === true
                ? Array.from(new Set([...prev, ...pageIds]))
                : prev.filter((id) => !pageIds.includes(id)),
            );
          }}
        />
      ),
      cell: ({ row }) => (
        <Checkbox
          aria-label={t('selectItem', { name: row.original.name })}
          checked={selectedKeys.includes(row.original.id)}
          onCheckedChange={(v) => {
            setSelectedKeys((prev) =>
              v === true
                ? [...prev, row.original.id]
                : prev.filter((id) => id !== row.original.id),
            );
          }}
        />
      ),
    },
    {
      accessorKey: 'name',
      header: t('col.asset'),
      cell: ({ row }) => (
        <button
          className="text-sm font-medium hover:underline"
          onClick={() => navigate(`/content/${row.original.id}`)}
        >
          {row.original.name}
        </button>
      ),
    },
    { accessorKey: 'space_slug', header: t('col.space'), cell: ({ row }) => row.original.space_slug ?? '-' },
    {
      accessorKey: 'mime_type',
      header: t('col.type'),
      cell: ({ row }) => <Badge variant="outline">{row.original.mime_type}</Badge>,
    },
    {
      accessorKey: 'size_bytes',
      header: t('col.size'),
      cell: ({ row }) => formatBytes(row.original.size_bytes),
    },
    {
      accessorKey: 'status',
      header: t('col.assetStatus'),
      cell: ({ row }) => <Badge variant="outline">{row.original.status}</Badge>,
    },
    {
      accessorKey: 'review_reason',
      header: t('col.reviewReason'),
      cell: ({ row }) => row.original.review_reason ?? '-',
    },
    {
      accessorKey: 'created_at',
      header: t('col.submittedAt'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => (
        <div className="flex items-center gap-1">
          <ConfirmAction
            title={t('approveConfirm')}
            onConfirm={async () => {
              try {
                await decide(row.original.id, 'approve');
                toast.success(t('approvedToast'));
              } catch (e) {
                toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
              }
            }}
            trigger={
              <Button variant="link" size="xs" className="px-0" disabled={!canWrite}>
                {t('approve')}
              </Button>
            }
          />
          <Button
            variant="link"
            size="xs"
            className="px-0 text-destructive hover:text-destructive"
            disabled={!canWrite}
            onClick={() => setRejectTarget(row.original)}
          >
            {t('reject')}
          </Button>
          <Button
            variant="link"
            size="xs"
            className="px-0 text-destructive hover:text-destructive"
            disabled={!canWrite}
            onClick={() => setTakedownTarget(row.original)}
          >
            {t('takedown')}
          </Button>
          <Button
            variant="link"
            size="xs"
            className="px-0"
            onClick={() => navigate(`/content/${row.original.id}`)}
          >
            {t('chunkReview')}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('title')} description={t('description')} />
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-72"
          placeholder={t('searchPlaceholder')}
          value={draftSpaceId}
          onChange={(e) => setDraftSpaceId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              setSpaceId(draftSpaceId.trim());
              setPage(1);
            }
          }}
        />
        <Button
          variant="outline"
          size="icon"
          aria-label={t('action.search')}
          onClick={() => {
            setSpaceId(draftSpaceId.trim());
            setPage(1);
          }}
        >
          <Search />
        </Button>
        <Button
          disabled={!canWrite || selectedKeys.length === 0 || busy}
          onClick={async () => {
            setBusy(true);
            try {
              for (const id of selectedKeys) await decide(id, 'approve');
              toast.success(t('batchApprovedToast'));
              setSelectedKeys([]);
            } catch (e) {
              toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
            } finally {
              setBusy(false);
            }
          }}
        >
          {t('batchApprove', { count: selectedKeys.length })}
        </Button>
        <Button
          variant="destructive"
          disabled={!canWrite || selectedKeys.length === 0}
          onClick={() => setBatchRejectOpen(true)}
        >
          {t('batchReject', { count: selectedKeys.length })}
        </Button>
        <Button
          variant="outline"
          disabled={!canWrite || selectedKeys.length === 0}
          onClick={() => void batchRerun()}
        >
          {t('batchRerun', { count: selectedKeys.length })}
        </Button>
      </div>
      <DataTable
        columns={columns}
        data={items}
        loading={isFetching}
        error={isError ? error : undefined}
        total={data?.total}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
      />

      {/* 单个驳回（危险操作：驳回必填理由并通知 owner） */}
      <ReasonModal
        open={rejectTarget !== null}
        title={t('rejectTitle', { name: rejectTarget?.name ?? '' })}
        description={t('rejectDescription')}
        okText={t('confirmReject')}
        danger
        confirmLoading={busy}
        onCancel={() => setRejectTarget(null)}
        onOk={async (reason) => {
          if (!rejectTarget) return;
          setBusy(true);
          try {
            await decide(rejectTarget.id, 'reject', reason);
            toast.success(t('rejectedToast'));
            setRejectTarget(null);
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 批量驳回 */}
      <ReasonModal
        open={batchRejectOpen}
        title={t('batchRejectTitle', { count: selectedKeys.length })}
        description={t('batchRejectDescription')}
        okText={t('confirmReject')}
        danger
        confirmLoading={busy}
        onCancel={() => setBatchRejectOpen(false)}
        onOk={async (reason) => {
          setBusy(true);
          try {
            for (const id of selectedKeys) await decide(id, 'reject', reason);
            toast.success(t('batchRejectedToast'));
            setBatchRejectOpen(false);
            setSelectedKeys([]);
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 下架（危险操作：rejected + 软删 + 向量删除） */}
      <ReasonModal
        open={takedownTarget !== null}
        title={t('takedownTitle', { name: takedownTarget?.name ?? '' })}
        description={t('takedownDescription')}
        okText={t('confirmTakedown')}
        danger
        confirmLoading={busy}
        onCancel={() => setTakedownTarget(null)}
        onOk={takedown}
      />
      <p className="text-sm text-muted-foreground">{t('queueHint')}</p>
    </div>
  );
}
