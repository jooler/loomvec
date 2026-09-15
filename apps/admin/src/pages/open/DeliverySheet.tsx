/** 投递日志抽屉：按订阅查看投递记录，失败/死信可重放（从 Webhooks 页拆出）。 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
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

/** 投递状态 → 徽章色调（文案经 delivery.status.* 翻译）。 */
const DELIVERY_STATUS_TONE: Record<string, BadgeTone> = {
  pending: 'gray',
  delivered: 'green',
  failed: 'red',
  dead: 'red',
};

export function DeliverySheet(props: { sub: Subscription; canWrite: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { t } = useTranslation('open');
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
      toast.success(t('delivery.replayAccepted'));
      void queryClient.invalidateQueries({ queryKey: ['admin-webhook-deliveries'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  const columns: ColumnDef<Delivery, unknown>[] = [
    {
      accessorKey: 'event_type',
      header: t('webhook.col.events'),
      cell: ({ row }) => <Badge variant="outline">{row.original.event_type}</Badge>,
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) => {
        const s = row.original.status;
        return (
          <StatusBadge tone={DELIVERY_STATUS_TONE[s] ?? 'gray'}>
            {t(`delivery.status.${s}`, { defaultValue: s })}
          </StatusBadge>
        );
      },
    },
    { accessorKey: 'attempt', header: t('delivery.col.attempts') },
    {
      accessorKey: 'response_status',
      header: t('delivery.col.responseStatus'),
      cell: ({ row }) => row.original.response_status ?? '-',
    },
    {
      accessorKey: 'error',
      header: t('delivery.col.error'),
      cell: ({ row }) => (
        <span className="block max-w-40 truncate" title={row.original.error ?? undefined}>
          {row.original.error ?? '-'}
        </span>
      ),
    },
    {
      accessorKey: 'delivered_at',
      header: t('delivery.col.deliveredAt'),
      cell: ({ row }) => (row.original.delivered_at ? formatDateTime(row.original.delivered_at) : '-'),
    },
    {
      accessorKey: 'created_at',
      header: t('field.createdAt'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => {
        const d = row.original;
        return d.status === 'failed' || d.status === 'dead' ? (
          <ConfirmAction
            title={t('delivery.replayConfirm')}
            trigger={
              <Button variant="link" size="sm" disabled={!props.canWrite}>
                {t('delivery.replay')}
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
          <SheetTitle>{t('delivery.title', { name: props.sub.name })}</SheetTitle>
          <div className="flex items-center gap-1">
            <Select
              value={status}
              onValueChange={(v) => {
                setStatus(v);
                setPage({ current: 1, pageSize: DELIVERY_PAGE_SIZE });
              }}
            >
              <SelectTrigger className="w-[130px]">
                <SelectValue placeholder={t('delivery.statusPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {Object.keys(DELIVERY_STATUS_TONE).map((value) => (
                  <SelectItem key={value} value={value}>
                    {t(`delivery.status.${value}`, { defaultValue: value })}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {status && (
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={t('clearFilter')}
                title={t('clearFilter')}
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
