/** 投递日志抽屉：按订阅查看投递记录，失败/死信可重放（从 Webhooks 页拆出）。 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { api, unwrap } from '@/api';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { StatusBadge, type BadgeTone } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@loomvec/ui/components/ui/sheet';
import type { Delivery, PagedResp, Subscription } from '@/types';
import { formatDateTime } from '@/utils';

const DELIVERY_PAGE_SIZE = 10;

const DELIVERY_STATUS_META: Record<string, { tone: BadgeTone; text: string }> = {
  pending: { tone: 'gray', text: '待投递' },
  delivered: { tone: 'green', text: '已投递' },
  failed: { tone: 'red', text: '失败' },
  dead: { tone: 'red', text: '死信' },
};

export function DeliverySheet(props: { sub: Subscription; canWrite: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<string | undefined>();
  const [page, setPage] = useState({ current: 1, pageSize: DELIVERY_PAGE_SIZE });

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching } = useQuery({
    queryKey: ['admin-webhook-deliveries', props.sub.id, status, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<Delivery>>(
        api.GET('/api/v1/admin/webhooks/{sub_id}/deliveries', {
          params: {
            path: { sub_id: props.sub.id },
            query: { status: status || undefined, limit, offset },
          },
        }),
      ),
    refetchInterval: 10_000,
  });

  const replay = async (d: Delivery) => {
    try {
      await unwrap(
        api.POST('/api/v1/admin/webhooks/deliveries/{delivery_id}/replay', {
          params: { path: { delivery_id: d.id } },
        }),
      );
      toast.success('重放已受理');
      void queryClient.invalidateQueries({ queryKey: ['admin-webhook-deliveries'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  const columns: ColumnDef<Delivery, unknown>[] = [
    {
      accessorKey: 'event_type',
      header: '事件',
      cell: ({ row }) => <Badge variant="outline">{row.original.event_type}</Badge>,
    },
    {
      accessorKey: 'status',
      header: '状态',
      cell: ({ row }) => {
        const m = DELIVERY_STATUS_META[row.original.status] ?? {
          tone: 'gray' as BadgeTone,
          text: row.original.status,
        };
        return <StatusBadge tone={m.tone}>{m.text}</StatusBadge>;
      },
    },
    { accessorKey: 'attempt', header: '尝试' },
    {
      accessorKey: 'response_status',
      header: '响应码',
      cell: ({ row }) => row.original.response_status ?? '-',
    },
    {
      accessorKey: 'error',
      header: '错误',
      cell: ({ row }) => (
        <span className="block max-w-40 truncate" title={row.original.error ?? undefined}>
          {row.original.error ?? '-'}
        </span>
      ),
    },
    {
      accessorKey: 'delivered_at',
      header: '投递时间',
      cell: ({ row }) => (row.original.delivered_at ? formatDateTime(row.original.delivered_at) : '-'),
    },
    {
      accessorKey: 'created_at',
      header: '创建时间',
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) => {
        const d = row.original;
        return d.status === 'failed' || d.status === 'dead' ? (
          <ConfirmAction
            title="确认重放该投递？"
            trigger={
              <Button variant="link" size="sm" disabled={!props.canWrite}>
                重放
              </Button>
            }
            onConfirm={() => replay(d)}
          />
        ) : (
          '-'
        );
      },
    },
  ];

  return (
    <Sheet open onOpenChange={(o) => !o && props.onClose()}>
      <SheetContent className="w-full gap-4 sm:max-w-[880px]">
        <SheetHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <SheetTitle>投递日志：{props.sub.name}</SheetTitle>
          <div className="flex items-center gap-1">
            <Select
              value={status}
              onValueChange={(v) => {
                setStatus(v);
                setPage({ current: 1, pageSize: DELIVERY_PAGE_SIZE });
              }}
            >
              <SelectTrigger className="w-[130px]">
                <SelectValue placeholder="投递状态" />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(DELIVERY_STATUS_META).map(([value, m]) => (
                  <SelectItem key={value} value={value}>
                    {m.text}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {status && (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="清除筛选"
                title="清除筛选"
                onClick={() => {
                  setStatus(undefined);
                  setPage({ current: 1, pageSize: DELIVERY_PAGE_SIZE });
                }}
              >
                <X />
              </Button>
            )}
          </div>
        </SheetHeader>
        <div className="flex-1 overflow-y-auto pb-4">
          <DataTable
            columns={columns}
            data={data?.items}
            loading={isFetching}
            total={data?.total}
            page={page.current}
            pageSize={page.pageSize}
            onPageChange={(current) => setPage({ current, pageSize: DELIVERY_PAGE_SIZE })}
          />
        </div>
      </SheetContent>
    </Sheet>
  );
}
