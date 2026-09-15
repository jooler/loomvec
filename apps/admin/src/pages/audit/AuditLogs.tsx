import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Download } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { DataTable } from '@loomvec/ui/components/data-table';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import type { AuditLogRow, PagedResp } from '@/types';
import { extractApiError, formatDateTime } from '@/utils';

const PAGE_SIZE = 20;

/** 审计日志（/audit）：全量写操作 + 登录事件，按操作者/对象/时间/租户筛选，JSONL 导出（docs/04 §5.10）。 */
export function AuditLogsPage() {
  const { t } = useTranslation('audit');
  // 筛选草稿态：输入过程不发请求，点「查询」（或回车）一次性提交，避免每键触发列表请求
  const [draft, setDraft] = useState({
    actor: '',
    action: '',
    objectType: '',
    tenantId: '',
    since: '',
    until: '',
  });
  const [filters, setFilters] = useState({
    actor: '',
    action: '',
    objectType: '',
    tenantId: '',
    since: null as string | null,
    until: null as string | null,
  });
  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [detail, setDetail] = useState<AuditLogRow | null>(null);

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['admin-audit-logs', filters, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<AuditLogRow>>(
        api.GET('/api/v1/admin/audit-logs', {
          params: {
            query: {
              actor: filters.actor || undefined,
              action: filters.action || undefined,
              object_type: filters.objectType || undefined,
              tenant_id: filters.tenantId || undefined,
              since: filters.since ?? undefined,
              until: filters.until ?? undefined,
              limit,
              offset,
            },
          },
        }),
      ),
  });

  const applyFilters = () => {
    setFilters({
      actor: draft.actor.trim(),
      action: draft.action.trim(),
      objectType: draft.objectType.trim(),
      tenantId: draft.tenantId.trim(),
      since: draft.since || null,
      until: draft.until || null,
    });
    setPage({ current: 1, pageSize: PAGE_SIZE });
  };

  const exportFilters = () => ({
    actor: filters.actor || undefined,
    action: filters.action || undefined,
    object_type: filters.objectType || undefined,
    tenant_id: filters.tenantId || undefined,
    since: filters.since ?? undefined,
    until: filters.until ?? undefined,
  });

  /** JSONL 导出下载（a link 下载；需携带 Bearer token，故经 api 取文本后落地 Blob）。 */
  const exportLogs = async () => {
    try {
      const { data: text, error } = await api.GET('/api/v1/admin/audit-logs/export', {
        parseAs: 'text',
        params: { query: { ...exportFilters(), limit: 10000 } },
      });
      if (error != null) throw new Error(extractApiError(error, t('exportFailed')));
      const blob = new Blob([String(text ?? '')], { type: 'application/x-ndjson' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'audit-logs.jsonl';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('exportFailed'));
    }
  };

  const columns: ColumnDef<AuditLogRow, unknown>[] = [
    {
      accessorKey: 'created_at',
      header: t('col.time'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      accessorKey: 'actor_user_id',
      header: t('col.actor'),
      cell: ({ row }) =>
        row.original.actor_user_id ? `${row.original.actor_user_id.slice(0, 8)}…` : '-',
    },
    {
      accessorKey: 'action',
      header: t('col.action'),
      cell: ({ row }) => (
        <StatusBadge tone="blue">{row.original.action}</StatusBadge>
      ),
    },
    { accessorKey: 'object_type', header: t('col.objectType') },
    {
      accessorKey: 'object_id',
      header: t('col.objectId'),
      cell: ({ row }) => (row.original.object_id ? `${row.original.object_id.slice(0, 8)}…` : '-'),
    },
    {
      accessorKey: 'reason',
      header: t('col.reason'),
      cell: ({ row }) => (
        <span className="block max-w-48 truncate" title={row.original.reason ?? undefined}>
          {row.original.reason ?? '-'}
        </span>
      ),
    },
    {
      accessorKey: 'request_id',
      header: 'Request ID',
      cell: ({ row }) =>
        row.original.request_id ? `${row.original.request_id.slice(0, 8)}…` : '-',
    },
    {
      id: 'actions',
      header: t('col.detail'),
      cell: ({ row }) => (
        <Button variant="link" size="sm" onClick={() => setDetail(row.original)}>
          {t('beforeAfter')}
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('title')}
        actions={
          <Button variant="outline" onClick={() => void exportLogs()}>
            <Download />
            {t('exportButton')}
          </Button>
        }
      />

      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          applyFilters();
        }}
      >
        <Input
          placeholder={t('actorPlaceholder')}
          className="w-60"
          value={draft.actor}
          onChange={(e) => setDraft((d) => ({ ...d, actor: e.target.value }))}
        />
        <Input
          placeholder={t('actionPlaceholder')}
          className="w-56"
          value={draft.action}
          onChange={(e) => setDraft((d) => ({ ...d, action: e.target.value }))}
        />
        <Input
          placeholder={t('objectTypePlaceholder')}
          className="w-44"
          value={draft.objectType}
          onChange={(e) => setDraft((d) => ({ ...d, objectType: e.target.value }))}
        />
        <Input
          placeholder={t('tenantIdPlaceholder')}
          className="w-60"
          value={draft.tenantId}
          onChange={(e) => setDraft((d) => ({ ...d, tenantId: e.target.value }))}
        />
        <Input
          type="date"
          aria-label={t('sinceLabel')}
          className="w-40"
          value={draft.since}
          onChange={(e) => setDraft((d) => ({ ...d, since: e.target.value }))}
        />
        <Input
          type="date"
          aria-label={t('untilLabel')}
          className="w-40"
          value={draft.until}
          onChange={(e) => setDraft((d) => ({ ...d, until: e.target.value }))}
        />
        <Button type="submit" variant="outline">
          {t('query')}
        </Button>
      </form>

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        error={isError ? error : undefined}
        total={data?.total}
        page={page.current}
        pageSize={page.pageSize}
        onPageChange={(current) => setPage({ current, pageSize: PAGE_SIZE })}
      />

      {/* 变更前后值详情 */}
      <Dialog open={detail !== null} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('detailTitle', { action: detail?.action ?? '' })}</DialogTitle>
          </DialogHeader>
          {detail && (
            <div className="space-y-3 text-sm">
              <p>
                <span className="font-medium">{t('objectLabel')}</span>
                {detail.object_type} / {detail.object_id ?? '-'}
              </p>
              <p>
                <span className="font-medium">{t('reasonLabel')}</span>
                {detail.reason ?? '-'}
              </p>
              <div>
                <p className="font-medium">{t('beforeLabel')}</p>
                <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">
                  {detail.before_value ? JSON.stringify(detail.before_value, null, 2) : '-'}
                </pre>
              </div>
              <div>
                <p className="font-medium">{t('afterLabel')}</p>
                <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">
                  {detail.after_value ? JSON.stringify(detail.after_value, null, 2) : '-'}
                </pre>
              </div>
              <p>
                <span className="font-medium">{t('ipLabel')}</span>
                {detail.ip ?? '-'}
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
