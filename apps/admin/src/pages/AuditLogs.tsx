import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Download } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
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
import { formatDateTime } from '@/utils';

const PAGE_SIZE = 20;

/** 审计日志（/audit）：全量写操作 + 登录事件，按操作者/对象/时间/租户筛选，JSONL 导出（docs/04 §5.10）。 */
export function AuditLogsPage() {
  const [actor, setActor] = useState('');
  const [action, setAction] = useState('');
  const [objectType, setObjectType] = useState('');
  const [tenantId, setTenantId] = useState('');
  const [since, setSince] = useState<string | null>(null);
  const [until, setUntil] = useState<string | null>(null);
  const [page, setPage] = useState({ current: 1, pageSize: PAGE_SIZE });
  const [detail, setDetail] = useState<AuditLogRow | null>(null);

  const limit = page.pageSize;
  const offset = (page.current - 1) * page.pageSize;

  const { data, isFetching } = useQuery({
    queryKey: ['admin-audit-logs', actor, action, objectType, tenantId, since, until, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<AuditLogRow>>(
        api.GET('/api/v1/admin/audit-logs', {
          params: {
            query: {
              actor: actor || undefined,
              action: action || undefined,
              object_type: objectType || undefined,
              tenant_id: tenantId || undefined,
              since: since ?? undefined,
              until: until ?? undefined,
              limit,
              offset,
            },
          },
        }),
      ),
  });

  const filters = () => ({
    actor: actor || undefined,
    action: action || undefined,
    object_type: objectType || undefined,
    tenant_id: tenantId || undefined,
    since: since ?? undefined,
    until: until ?? undefined,
  });

  /** JSONL 导出下载（a link 下载；需携带 Bearer token，故经 api 取文本后落地 Blob）。 */
  const exportLogs = async () => {
    try {
      const { data: text, error } = await api.GET('/api/v1/admin/audit-logs/export', {
        parseAs: 'text',
        params: { query: { ...filters(), limit: 10000 } },
      });
      if (error != null) throw new Error('导出失败');
      const blob = new Blob([String(text ?? '')], { type: 'application/x-ndjson' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'audit-logs.jsonl';
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '导出失败');
    }
  };

  const columns: ColumnDef<AuditLogRow, unknown>[] = [
    {
      accessorKey: 'created_at',
      header: '时间',
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      accessorKey: 'actor_user_id',
      header: '操作者',
      cell: ({ row }) =>
        row.original.actor_user_id ? `${row.original.actor_user_id.slice(0, 8)}…` : '-',
    },
    {
      accessorKey: 'action',
      header: '操作',
      cell: ({ row }) => (
        <StatusBadge tone="blue">{row.original.action}</StatusBadge>
      ),
    },
    { accessorKey: 'object_type', header: '对象类型' },
    {
      accessorKey: 'object_id',
      header: '对象 ID',
      cell: ({ row }) => (row.original.object_id ? `${row.original.object_id.slice(0, 8)}…` : '-'),
    },
    {
      accessorKey: 'reason',
      header: '理由',
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
      header: '详情',
      cell: ({ row }) => (
        <Button variant="link" size="sm" onClick={() => setDetail(row.original)}>
          变更前后
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title="审计日志"
        actions={
          <Button variant="outline" onClick={() => void exportLogs()}>
            <Download />
            导出 JSONL（≤10000 条）
          </Button>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="操作者 user_id（UUID）"
          className="w-60"
          onChange={(e) => {
            setActor(e.target.value.trim());
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        />
        <Input
          placeholder="操作（前缀匹配，如 admin.tenant）"
          className="w-56"
          onChange={(e) => {
            setAction(e.target.value.trim());
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        />
        <Input
          placeholder="对象类型，如 tenant / space"
          className="w-44"
          onChange={(e) => {
            setObjectType(e.target.value.trim());
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        />
        <Input
          placeholder="租户 ID（UUID）"
          className="w-60"
          onChange={(e) => {
            setTenantId(e.target.value.trim());
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        />
        <Input
          type="date"
          aria-label="起始时间"
          className="w-40"
          value={since ?? ''}
          onChange={(e) => {
            setSince(e.target.value || null);
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        />
        <Input
          type="date"
          aria-label="截止时间"
          className="w-40"
          value={until ?? ''}
          onChange={(e) => {
            setUntil(e.target.value || null);
            setPage({ current: 1, pageSize: PAGE_SIZE });
          }}
        />
      </div>

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        total={data?.total}
        page={page.current}
        pageSize={page.pageSize}
        onPageChange={(current) => setPage({ current, pageSize: PAGE_SIZE })}
      />

      {/* 变更前后值详情 */}
      <Dialog open={detail !== null} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>审计详情：{detail?.action ?? ''}</DialogTitle>
          </DialogHeader>
          {detail && (
            <div className="space-y-3 text-sm">
              <p>
                <span className="font-medium">对象：</span>
                {detail.object_type} / {detail.object_id ?? '-'}
              </p>
              <p>
                <span className="font-medium">理由：</span>
                {detail.reason ?? '-'}
              </p>
              <div>
                <p className="font-medium">变更前：</p>
                <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">
                  {detail.before_value ? JSON.stringify(detail.before_value, null, 2) : '-'}
                </pre>
              </div>
              <div>
                <p className="font-medium">变更后：</p>
                <pre className="mt-1 max-h-60 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">
                  {detail.after_value ? JSON.stringify(detail.after_value, null, 2) : '-'}
                </pre>
              </div>
              <p>
                <span className="font-medium">IP：</span>
                {detail.ip ?? '-'}
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
