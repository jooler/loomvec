import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { DataTable } from '@loomvec/ui/components/data-table';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import type { SystemStatus } from '@/types';
import { COMPONENT_LABEL } from '@/constants';
import { formatBytes, formatDateTime, formatSeconds } from '@/utils';

/** 组件 detail 中值得展示的键（version/error/age_seconds 等）。 */
function detailText(detail: Record<string, unknown>): string {
  return Object.entries(detail)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join('，');
}

type ComponentRow = SystemStatus['components'][number];

/** 系统状态：组件详情 / 队列深度 / worker 心跳 / 备份 / 平台版本（docs/04 §5.11）。 */
export function SystemPage() {
  const { t } = useTranslation('system');

  const componentColumns: ColumnDef<ComponentRow, unknown>[] = [
    {
      accessorKey: 'name',
      header: t('col.component'),
      cell: ({ row }) => COMPONENT_LABEL[row.original.name] ?? row.original.name,
    },
    {
      accessorKey: 'ok',
      header: t('field.status'),
      cell: ({ row }) =>
        row.original.ok ? (
          <StatusBadge tone="green">{t('status.ok')}</StatusBadge>
        ) : (
          <StatusBadge tone="red">{t('status.error')}</StatusBadge>
        ),
    },
    {
      accessorKey: 'detail',
      header: t('col.detail'),
      cell: ({ row }) => detailText(row.original.detail) || '-',
    },
  ];

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['admin-system-status'],
    queryFn: () => unwrap<SystemStatus>(api.GET('/api/v1/admin/system/status')),
    refetchInterval: 15_000,
  });

  const worker = data?.components.find((c) => c.name === 'worker');
  const apiComp = data?.components.find((c) => c.name === 'api');
  const workerAge = worker?.detail.age_seconds as number | undefined;

  return (
    <div className="space-y-4">
      <PageHeader title={t('title')} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>{t('componentsTitle')}</CardTitle>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={componentColumns}
              data={data?.components}
              loading={isLoading}
              error={isError ? error : undefined}
              getRowId={(r) => r.name}
            />
          </CardContent>
        </Card>

        {/* 右列：三张卡片纵向堆叠 */}
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>{t('queuesTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList cols={1}>
                {Object.entries(data?.pipeline.queue_depths ?? {}).map(([q, depth]) => (
                  <DescriptionItem key={q} label={q}>
                    <Badge variant="outline">{depth}</Badge>
                  </DescriptionItem>
                ))}
                <DescriptionItem label={t('deadLetterQueue')}>
                  <StatusBadge tone={data?.pipeline.dead_letter ? 'red' : 'gray'}>
                    {data?.pipeline.dead_letter ?? 0}
                  </StatusBadge>
                </DescriptionItem>
                <DescriptionItem label={t('workerHeartbeat')}>
                  {worker?.ok ? (
                    <span>age {formatSeconds(workerAge)}</span>
                  ) : (
                    <StatusBadge tone="red">{t('noHeartbeat')}</StatusBadge>
                  )}
                </DescriptionItem>
              </DescriptionList>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('backupTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              {Object.entries(data?.backup ?? {}).length ? (
                <DescriptionList cols={1}>
                  {Object.entries(data?.backup ?? {}).map(([k, v]) => (
                    <DescriptionItem key={k} label={k}>
                      {typeof v === 'object' ? JSON.stringify(v) : String(v)}
                    </DescriptionItem>
                  ))}
                </DescriptionList>
              ) : (
                <p className="text-sm text-muted-foreground">{t('noRecords')}</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t('platformTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList cols={1}>
                <DescriptionItem label={t('apiVersion')}>
                  {String(apiComp?.detail.version ?? t('unknown'))}
                </DescriptionItem>
                <DescriptionItem label={t('storage')}>
                  {t('storageUsage', {
                    size: formatBytes(data?.scale.storage_bytes ?? 0),
                    count: data?.scale.file_count ?? 0,
                  })}
                </DescriptionItem>
                <DescriptionItem label={t('generatedAt')}>
                  {data ? formatDateTime(data.generated_at) : '-'}
                </DescriptionItem>
              </DescriptionList>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
