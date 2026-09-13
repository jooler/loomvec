import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { api, unwrap } from '@/api';
import { DataTable } from '@loomvec/ui/components/data-table';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import type { SystemStatus } from '@/types';
import { formatBytes, formatDateTime, formatSeconds } from '@/utils';

const COMPONENT_LABEL: Record<string, string> = {
  api: 'API',
  worker: 'Worker',
  postgres: 'PostgreSQL(+AGE)',
  milvus: 'Milvus',
  storage: 'RustFS',
  redis: 'Redis',
  mineru: 'MinerU',
};

/** 组件 detail 中值得展示的键（version/error/age_seconds 等）。 */
function detailText(detail: Record<string, unknown>): string {
  return Object.entries(detail)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join('，');
}

type ComponentRow = SystemStatus['components'][number];

const componentColumns: ColumnDef<ComponentRow, unknown>[] = [
  {
    accessorKey: 'name',
    header: '组件',
    cell: ({ row }) => COMPONENT_LABEL[row.original.name] ?? row.original.name,
  },
  {
    accessorKey: 'ok',
    header: '状态',
    cell: ({ row }) =>
      row.original.ok ? (
        <StatusBadge tone="green">正常</StatusBadge>
      ) : (
        <StatusBadge tone="red">异常</StatusBadge>
      ),
  },
  {
    accessorKey: 'detail',
    header: '版本 / 详情 / 错误',
    cell: ({ row }) => detailText(row.original.detail) || '-',
  },
];

/** 系统状态：组件详情 / 队列深度 / worker 心跳 / 备份 / 平台版本（docs/04 §5.11）。 */
export function SystemPage() {
  const { data, isLoading } = useQuery({
    queryKey: ['admin-system-status'],
    queryFn: () => unwrap<SystemStatus>(api.GET('/api/v1/admin/system/status')),
    refetchInterval: 15_000,
  });

  const worker = data?.components.find((c) => c.name === 'worker');
  const apiComp = data?.components.find((c) => c.name === 'api');
  const workerAge = worker?.detail.age_seconds as number | undefined;

  return (
    <div className="space-y-4">
      <PageHeader title="系统状态" />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>组件健康详情</CardTitle>
          </CardHeader>
          <CardContent>
            <DataTable
              columns={componentColumns}
              data={data?.components}
              loading={isLoading}
              getRowId={(r) => r.name}
            />
          </CardContent>
        </Card>

        {/* 右列：三张卡片纵向堆叠 */}
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>队列与心跳</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList cols={1}>
                {Object.entries(data?.pipeline.queue_depths ?? {}).map(([q, depth]) => (
                  <DescriptionItem key={q} label={q}>
                    <Badge variant="outline">{depth}</Badge>
                  </DescriptionItem>
                ))}
                <DescriptionItem label="死信队列">
                  <StatusBadge tone={data?.pipeline.dead_letter ? 'red' : 'gray'}>
                    {data?.pipeline.dead_letter ?? 0}
                  </StatusBadge>
                </DescriptionItem>
                <DescriptionItem label="worker 心跳">
                  {worker?.ok ? (
                    <span>age {formatSeconds(workerAge)}</span>
                  ) : (
                    <StatusBadge tone="red">无心跳（worker 未运行）</StatusBadge>
                  )}
                </DescriptionItem>
              </DescriptionList>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>备份状态</CardTitle>
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
                <p className="text-sm text-muted-foreground">暂无记录</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>平台信息</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList cols={1}>
                <DescriptionItem label="API 组件版本">
                  {String(apiComp?.detail.version ?? '未知')}
                </DescriptionItem>
                <DescriptionItem label="存储用量">
                  {formatBytes(data?.scale.storage_bytes ?? 0)}（{data?.scale.file_count ?? 0} 文件）
                </DescriptionItem>
                <DescriptionItem label="状态生成时间">
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
