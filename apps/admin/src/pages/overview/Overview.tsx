import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { ExternalLink } from 'lucide-react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { DataTable } from '@loomvec/ui/components/data-table';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatCard } from '@loomvec/ui/components/stat-card';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';
import { Tooltip, TooltipContent, TooltipTrigger } from '@loomvec/ui/components/ui/tooltip';
import type { SystemStatus } from '@/types';
import { COMPONENT_LABEL, QUEUE_LABEL } from '@/constants';
import { formatBytes, formatDateTime, formatSeconds } from '@/utils';
import { cn } from 'cn';

const GRAFANA_URL = import.meta.env.VITE_GRAFANA_URL ?? 'http://localhost:3002';

type StageLatencyRow = { stage: string; p50: number; p95: number };

/** 总览：健康卡片 / 管线态势 / 平台规模 / 待办聚合 / 备份状态 / Grafana 外链（docs/04 §5.1）。 */
export function OverviewPage() {
  const navigate = useNavigate();
  const { t } = useTranslation('overview');

  const stageColumns: ColumnDef<StageLatencyRow, unknown>[] = [
    { accessorKey: 'stage', header: t('colStage') },
    { accessorKey: 'p50', header: 'P50', cell: ({ row }) => formatSeconds(row.original.p50) },
    { accessorKey: 'p95', header: 'P95', cell: ({ row }) => formatSeconds(row.original.p95) },
  ];

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['admin-system-status'],
    queryFn: () => unwrap<SystemStatus>(api.GET('/api/v1/admin/system/status')),
    refetchInterval: 15_000,
  });

  const todo = data?.todo;

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('title')}
        description={
          data ? t('generatedAt', { time: formatDateTime(data.generated_at) }) : undefined
        }
        actions={
          <Button onClick={() => window.open(GRAFANA_URL, '_blank')}>
            <ExternalLink /> {t('openGrafana')}
          </Button>
        }
      />

      {/* 健康卡片：七组件 ok 绿 / 异常红；详情过长走 Tooltip，避免撑高卡片 */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
        {isLoading
          ? Array.from({ length: 7 }).map((_, i) => (
              <Skeleton key={i} className="h-20 rounded-xl" />
            ))
          : (data?.components ?? []).map((c) => {
              const detailText = c.ok
                ? t('componentOk')
                : String(c.detail.error ?? t('probeFailed')).slice(0, 80);
              return (
                <Card key={c.name} className="py-4">
                  <CardContent className="space-y-2 px-4">
                    <StatusBadge tone={c.ok ? 'green' : 'red'}>
                      {COMPONENT_LABEL[c.name] ?? c.name}
                    </StatusBadge>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <p
                          className={cn(
                            'truncate text-xs',
                            c.ok ? 'text-muted-foreground' : 'text-destructive',
                          )}
                        >
                          {detailText}
                        </p>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-xs break-all">
                        {JSON.stringify(c.detail)}
                      </TooltipContent>
                    </Tooltip>
                  </CardContent>
                </Card>
              );
            })}
      </div>
      {isError && <p className="text-sm text-destructive">{(error as Error).message}</p>}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 管线态势 */}
        <Card>
          <CardHeader>
            <CardTitle>{t('pipelineTitle')}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid grid-cols-3 gap-3">
              <StatCard title={t('totalTasks24h')} value={data?.pipeline.last_24h.total ?? 0} />
              <StatCard title={t('succeeded')} value={data?.pipeline.last_24h.succeeded ?? 0} />
              <StatCard
                title={t('failureRate')}
                value={`${((data?.pipeline.last_24h.failure_rate ?? 0) * 100).toFixed(1)}%`}
              />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {Object.entries(data?.pipeline.queue_depths ?? {}).map(([q, depth]) => (
                <Badge key={q} variant="outline">
                  {QUEUE_LABEL[q] ?? q}：{depth}
                </Badge>
              ))}
              <StatusBadge tone={data?.pipeline.dead_letter ? 'red' : 'gray'}>
                {t('deadLetter', { count: data?.pipeline.dead_letter ?? 0 })}
              </StatusBadge>
            </div>
            <DataTable
              columns={stageColumns}
              data={Object.entries(data?.pipeline.stage_latency_seconds ?? {}).map(
                ([stage, v]) => ({ stage, ...v }),
              )}
              loading={isLoading}
              getRowId={(r) => String(r.stage)}
            />
          </CardContent>
        </Card>

        {/* 右列：三张卡片纵向堆叠 */}
        <div className="space-y-4">
          {/* 平台规模 */}
          <Card>
            <CardHeader>
              <CardTitle>{t('scaleTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                <StatCard title={t('scale.tenants')} value={data?.scale.tenants ?? 0} />
                <StatCard title={t('scale.users')} value={data?.scale.users ?? 0} />
                <StatCard title={t('scale.spaces')} value={data?.scale.spaces ?? 0} />
                <StatCard title={t('scale.assets')} value={data?.scale.assets ?? 0} />
                <StatCard
                  title={t('scale.semanticUnits')}
                  value={data?.scale.semantic_units ?? 0}
                />
                <StatCard
                  title={t('scale.storage')}
                  value={formatBytes(data?.scale.storage_bytes ?? 0)}
                />
              </div>
            </CardContent>
          </Card>

          {/* 备份状态 */}
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
                <p className="text-sm text-muted-foreground">{t('backupEmpty')}</p>
              )}
            </CardContent>
          </Card>

          {/* 待办聚合：点击直达 */}
          <Card>
            <CardHeader>
              <CardTitle>{t('todoTitle')}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <StatCard
                  title={t('todo.pendingReview')}
                  value={todo?.pending_review_assets ?? 0}
                  valueClassName={(todo?.pending_review_assets ?? 0) > 0 ? 'text-amber-600' : undefined}
                  hint={
                    <button
                      className="hover:underline"
                      onClick={() => navigate('/reviews')}
                    >
                      {t('todo.goReview')}
                    </button>
                  }
                />
                <StatCard
                  title={t('todo.failedJobs')}
                  value={todo?.failed_jobs ?? 0}
                  valueClassName={(todo?.failed_jobs ?? 0) > 0 ? 'text-red-600' : undefined}
                  hint={
                    <button
                      className="hover:underline"
                      onClick={() => navigate('/pipeline')}
                    >
                      {t('todo.goProcess')}
                    </button>
                  }
                />
                <StatCard
                  title={t('todo.deadLetter')}
                  value={todo?.dead_letter ?? 0}
                  valueClassName={(todo?.dead_letter ?? 0) > 0 ? 'text-red-600' : undefined}
                  hint={
                    <button
                      className="hover:underline"
                      onClick={() => navigate('/pipeline')}
                    >
                      {t('todo.goView')}
                    </button>
                  }
                />
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
