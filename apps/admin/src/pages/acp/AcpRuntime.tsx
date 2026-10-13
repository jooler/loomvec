import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useState } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { AcpGatewayStatus } from '@/components/AcpGatewayStatus';
import { ReasonModal } from '@/components/ReasonModal';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { Button } from '@loomvec/ui/components/ui/button';
import type { AcpRuntime, AcpStatus } from '@/types';
import { formatDateTime } from '@/utils';

/** ACP 运行时状态：网关健康卡 + 活跃 runtime 表；super_admin / operator 可强制停止 workspace。 */
export function AcpRuntimePage() {
  const { canWrite } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('acp');
  const [stopTarget, setStopTarget] = useState<AcpRuntime | null>(null);
  const [busy, setBusy] = useState(false);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['acp-status'],
    queryFn: () => unwrap<AcpStatus>(api.GET('/api/v1/admin/acp-gateway/status')),
    refetchInterval: 15_000,
  });

  const stop = async (reason: string) => {
    if (!stopTarget) return;
    setBusy(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/acp-gateway/workspaces/{workspace_id}/stop', {
          params: { path: { workspace_id: stopTarget.workspace_id } },
          body: { reason },
        }),
      );
      toast.success(t('runtime.stopped'));
      setStopTarget(null);
      void queryClient.invalidateQueries({ queryKey: ['acp-status'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<AcpRuntime, unknown>[] = [
    {
      accessorKey: 'workspace_id',
      header: t('runtime.col.workspace'),
      cell: ({ row }) => (
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
          {row.original.workspace_id}
        </code>
      ),
    },
    { accessorKey: 'agent', header: t('runtime.col.agent') },
    {
      id: 'version',
      header: t('runtime.col.version'),
      cell: ({ row }) => {
        const info = row.original.agent_info;
        return info ? [info.name, info.version].filter(Boolean).join(' ') || '-' : '-';
      },
    },
    { accessorKey: 'active_turns', header: t('runtime.col.activeTurns') },
    {
      accessorKey: 'started_at',
      header: t('runtime.col.startedAt'),
      cell: ({ row }) => formatDateTime(row.original.started_at),
    },
    {
      accessorKey: 'last_activity',
      header: t('runtime.col.lastActivity'),
      cell: ({ row }) => formatDateTime(row.original.last_activity),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => (
        <Button
          variant="link"
          size="sm"
          className="text-destructive hover:text-destructive"
          disabled={!canWrite}
          title={canWrite ? undefined : t('readOnlyHint')}
          onClick={() => setStopTarget(row.original)}
        >
          {t('runtime.stop')}
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('runtime.title')} description={t('runtime.description')} />

      {isLoading ? (
        <p className="text-sm text-muted-foreground">{t('state.loading')}</p>
      ) : isError ? (
        <p className="text-sm text-destructive">{(error as Error).message}</p>
      ) : (
        data && <AcpGatewayStatus data={data} />
      )}

      <h2 className="text-base font-semibold">{t('runtime.listTitle')}</h2>
      <DataTable
        columns={columns}
        data={data?.runtimes}
        getRowId={(r) => r.workspace_id}
        loading={isLoading}
        emptyTitle={t('runtime.empty')}
      />

      <ReasonModal
        open={stopTarget !== null}
        title={t('runtime.stopTitle', { id: stopTarget?.workspace_id ?? '' })}
        description={t('runtime.stopDescription')}
        danger
        confirmLoading={busy}
        onOk={stop}
        onCancel={() => setStopTarget(null)}
      />
    </div>
  );
}
