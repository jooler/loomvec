import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
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

  const { data, isFetching } = useQuery({
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
      toast.success('已下架（连带向量清理）');
      setTakedownTarget(null);
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
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
      toast.success(`已受理 ${resp.accepted} 个资产的重跑任务`);
      setSelectedKeys([]);
      void queryClient.invalidateQueries({ queryKey: ['admin-pipeline-jobs'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  const columns: ColumnDef<ReviewAsset, unknown>[] = [
    {
      id: 'select',
      header: () => (
        <Checkbox
          aria-label="全选本页"
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
          aria-label={`选择 ${row.original.name}`}
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
      header: '资产',
      cell: ({ row }) => (
        <button
          className="text-sm font-medium hover:underline"
          onClick={() => navigate(`/content/${row.original.id}`)}
        >
          {row.original.name}
        </button>
      ),
    },
    { accessorKey: 'space_slug', header: '空间', cell: ({ row }) => row.original.space_slug ?? '-' },
    {
      accessorKey: 'mime_type',
      header: '类型',
      cell: ({ row }) => <Badge variant="outline">{row.original.mime_type}</Badge>,
    },
    {
      accessorKey: 'size_bytes',
      header: '大小',
      cell: ({ row }) => formatBytes(row.original.size_bytes),
    },
    {
      accessorKey: 'status',
      header: '资产状态',
      cell: ({ row }) => <Badge variant="outline">{row.original.status}</Badge>,
    },
    {
      accessorKey: 'review_reason',
      header: '待审原因',
      cell: ({ row }) => row.original.review_reason ?? '-',
    },
    {
      accessorKey: 'created_at',
      header: '提交时间',
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => (
        <div className="flex items-center gap-1">
          <ConfirmAction
            title="确认通过该资产？"
            onConfirm={async () => {
              try {
                await decide(row.original.id, 'approve');
                toast.success('已通过');
              } catch (e) {
                toast.error(e instanceof Error ? e.message : '操作失败');
              }
            }}
            trigger={
              <Button variant="link" size="xs" className="px-0" disabled={!canWrite}>
                通过
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
            驳回
          </Button>
          <Button
            variant="link"
            size="xs"
            className="px-0 text-destructive hover:text-destructive"
            disabled={!canWrite}
            onClick={() => setTakedownTarget(row.original)}
          >
            下架
          </Button>
          <Button
            variant="link"
            size="xs"
            className="px-0"
            onClick={() => navigate(`/content/${row.original.id}`)}
          >
            chunk 复核
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title="内容审核" description="开启「先审后见」的共享空间待审资产" />
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-72"
          placeholder="按空间 ID 过滤"
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
          aria-label="搜索"
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
              toast.success('已批量通过');
              setSelectedKeys([]);
            } catch (e) {
              toast.error(e instanceof Error ? e.message : '操作失败');
            } finally {
              setBusy(false);
            }
          }}
        >
          批量通过（{selectedKeys.length}）
        </Button>
        <Button
          variant="destructive"
          disabled={!canWrite || selectedKeys.length === 0}
          onClick={() => setBatchRejectOpen(true)}
        >
          批量驳回（{selectedKeys.length}）
        </Button>
        <Button
          variant="outline"
          disabled={!canWrite || selectedKeys.length === 0}
          onClick={() => void batchRerun()}
        >
          批量重跑（{selectedKeys.length}）
        </Button>
      </div>
      <DataTable
        columns={columns}
        data={items}
        loading={isFetching}
        total={data?.total}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
      />

      {/* 单个驳回（危险操作：驳回必填理由并通知 owner） */}
      <ReasonModal
        open={rejectTarget !== null}
        title={`驳回资产「${rejectTarget?.name ?? ''}」`}
        description="驳回后 owner 将收到通知（含驳回理由）。"
        okText="确认驳回"
        danger
        confirmLoading={busy}
        onCancel={() => setRejectTarget(null)}
        onOk={async (reason) => {
          if (!rejectTarget) return;
          setBusy(true);
          try {
            await decide(rejectTarget.id, 'reject', reason);
            toast.success('已驳回');
            setRejectTarget(null);
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 批量驳回 */}
      <ReasonModal
        open={batchRejectOpen}
        title={`批量驳回（${selectedKeys.length} 个资产）`}
        description="驳回理由将通知各资产 owner。"
        okText="确认驳回"
        danger
        confirmLoading={busy}
        onCancel={() => setBatchRejectOpen(false)}
        onOk={async (reason) => {
          setBusy(true);
          try {
            for (const id of selectedKeys) await decide(id, 'reject', reason);
            toast.success('已批量驳回');
            setBatchRejectOpen(false);
            setSelectedKeys([]);
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 下架（危险操作：rejected + 软删 + 向量删除） */}
      <ReasonModal
        open={takedownTarget !== null}
        title={`下架资产「${takedownTarget?.name ?? ''}」`}
        description="下架 = 驳回 + 软删 + 向量连带清理，操作不可逆。此为危险操作。"
        okText="确认下架"
        danger
        confirmLoading={busy}
        onCancel={() => setTakedownTarget(null)}
        onOk={takedown}
      />
      <p className="text-sm text-muted-foreground">点击资产名或「chunk 复核」进入切片质量复核页。</p>
    </div>
  );
}
