import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatCard } from '@loomvec/ui/components/stat-card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { Progress } from '@loomvec/ui/components/ui/progress';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import type { AcpUsage, AcpUsageDay } from '@/types';
import { lastDaysRange } from './helpers';
import { useAcpTenantOptions } from './useAcp';

const fmt = (n: number) => n.toLocaleString();

/** 配额占用卡：已用 / 上限 + 进度条（无上限只显示已用）。 */
function QuotaCard(props: { title: string; used: number; limit: number | undefined }) {
  const { t } = useTranslation('acp');
  const pct = props.limit ? Math.min(100, Math.round((props.used / props.limit) * 100)) : null;
  return (
    <div className="space-y-2">
      <StatCard
        title={props.title}
        value={fmt(props.used)}
        hint={props.limit ? t('usage.of', { limit: fmt(props.limit) }) : t('unlimited')}
        valueClassName={pct !== null && pct >= 90 ? 'text-destructive' : undefined}
      />
      {pct !== null && <Progress value={pct} />}
    </div>
  );
}

/** ACP 用量报表：按租户 / 天（上游实测 token）+ 当前配额占用。 */
export function AcpUsagePage() {
  const { t } = useTranslation('acp');
  const [params, setParams] = useSearchParams();
  const { data: tenants } = useAcpTenantOptions();
  const tenantId = params.get('tenant') ?? tenants?.items[0]?.id ?? '';

  const initial = useMemo(() => lastDaysRange(30), []);
  const [from, setFrom] = useState(initial.from);
  // 页面上的截止日期含当天；网关的 to 是开区间上界，请求时 +1 天
  const [toIncl, setToIncl] = useState(
    new Date(Date.parse(initial.to) - 86_400_000).toISOString().slice(0, 10),
  );
  const rangeOk = from !== '' && toIncl !== '' && from <= toIncl;
  const toExcl = rangeOk
    ? new Date(Date.parse(toIncl) + 86_400_000).toISOString().slice(0, 10)
    : '';

  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['acp-usage', tenantId, from, toExcl],
    enabled: tenantId !== '' && rangeOk,
    refetchInterval: 60_000,
    queryFn: () =>
      unwrap<AcpUsage>(
        api.GET('/api/v1/admin/acp-gateway/tenants/{tenant_id}/usage', {
          params: { path: { tenant_id: tenantId }, query: { from, to: toExcl } },
        }),
      ),
  });

  const totals = useMemo(
    () =>
      (data?.daily ?? []).reduce(
        (acc, d) => ({
          requests: acc.requests + d.requests,
          tokens: acc.tokens + d.input_tokens + d.output_tokens,
        }),
        { requests: 0, tokens: 0 },
      ),
    [data],
  );

  const columns: ColumnDef<AcpUsageDay, unknown>[] = [
    { accessorKey: 'day', header: t('usage.col.day') },
    {
      accessorKey: 'requests',
      header: t('usage.col.requests'),
      cell: ({ row }) => fmt(row.original.requests),
    },
    {
      accessorKey: 'input_tokens',
      header: t('usage.col.input'),
      cell: ({ row }) => fmt(row.original.input_tokens),
    },
    {
      accessorKey: 'output_tokens',
      header: t('usage.col.output'),
      cell: ({ row }) => fmt(row.original.output_tokens),
    },
    {
      accessorKey: 'cache_read_tokens',
      header: t('usage.col.cacheRead'),
      cell: ({ row }) => fmt(row.original.cache_read_tokens),
    },
    {
      accessorKey: 'cache_write_tokens',
      header: t('usage.col.cacheWrite'),
      cell: ({ row }) => fmt(row.original.cache_write_tokens),
    },
    {
      id: 'total',
      header: t('usage.col.total'),
      cell: ({ row }) => fmt(row.original.input_tokens + row.original.output_tokens),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('usage.title')} description={t('usage.description')} />

      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label>{t('keys.tenantLabel')}</Label>
          <Select value={tenantId} onValueChange={(v) => setParams({ tenant: v })}>
            <SelectTrigger className="w-72">
              <SelectValue placeholder={t('keys.tenantPlaceholder')} />
            </SelectTrigger>
            <SelectContent>
              {(tenants?.items ?? []).map((tn) => (
                <SelectItem key={tn.id} value={tn.id}>
                  {tn.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="acp-usage-from">{t('usage.from')}</Label>
          <Input
            id="acp-usage-from"
            type="date"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="acp-usage-to">{t('usage.to')}</Label>
          <Input
            id="acp-usage-to"
            type="date"
            value={toIncl}
            onChange={(e) => setToIncl(e.target.value)}
          />
        </div>
        {!rangeOk && <p className="text-sm text-destructive">{t('usage.rangeInvalid')}</p>}
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <QuotaCard
          title={t('usage.today')}
          used={data?.current.day ?? 0}
          limit={data?.quotas.daily_tokens}
        />
        <QuotaCard
          title={t('usage.month')}
          used={data?.current.month ?? 0}
          limit={data?.quotas.monthly_tokens}
        />
        <StatCard title={t('usage.requests')} value={fmt(totals.requests)} />
        <StatCard title={t('usage.total')} value={fmt(totals.tokens)} />
      </div>
      <p className="text-xs text-muted-foreground">
        <Link to="/acp/tenants" className="underline">
          {t('usage.adjustQuota')}
        </Link>
      </p>

      <DataTable
        columns={columns}
        data={data?.daily}
        getRowId={(r) => r.day}
        loading={isFetching}
        error={isError ? error : undefined}
        emptyTitle={t('usage.empty')}
      />
    </div>
  );
}
