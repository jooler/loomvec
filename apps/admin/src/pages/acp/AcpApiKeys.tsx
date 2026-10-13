import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Link, useSearchParams } from 'react-router';
import { useState } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ReasonModal } from '@/components/ReasonModal';
import { SecretModal } from '@/components/SecretModal';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge, type BadgeTone } from '@loomvec/ui/components/status-badge';
import { Alert, AlertDescription } from '@loomvec/ui/components/ui/alert';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Checkbox } from '@loomvec/ui/components/ui/checkbox';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import type { AcpApiKey, AcpIssuedApiKey, PagedResp } from '@/types';
import { formatDateTime } from '@/utils';
import { expiryFromDate, keyStatus, missingScopes, type KeyStatus } from './helpers';
import { useAcpConfig, useAcpTenantOptions } from './useAcp';

/** 网关支持的全部 scope（网关 docs/api.md「Scope」）。 */
const ALL_SCOPES = [
  'sessions:read',
  'sessions:write',
  'files:read',
  'files:write',
  'chat:completions',
  'acp:connect',
] as const;
type Scope = (typeof ALL_SCOPES)[number];

const STATUS_TONE: Record<KeyStatus, BadgeTone> = {
  active: 'green',
  revoked: 'red',
  expired: 'gray',
};

/** ACP API Key：按租户列出 / 签发（明文仅 SecretModal 展示一次）/ 吊销。写操作仅 super_admin。 */
export function AcpApiKeysPage() {
  const { isSuperAdmin } = usePerm();
  const queryClient = useQueryClient();
  const { t } = useTranslation('acp');
  const [params, setParams] = useSearchParams();
  const { data: cfg } = useAcpConfig();
  const { data: tenants } = useAcpTenantOptions();

  const tenantId = params.get('tenant') ?? tenants?.items[0]?.id ?? '';
  const required = cfg?.required_key_scopes ?? [];

  const [page, setPage] = useState(1);
  const [issueOpen, setIssueOpen] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<AcpApiKey | null>(null);
  const [busy, setBusy] = useState(false);
  // 签发表单
  const [keyName, setKeyName] = useState('');
  const [scopes, setScopes] = useState<string[]>([]);
  const [expiresDate, setExpiresDate] = useState('');
  const [rate, setRate] = useState('');
  // 明文仅保存在这个 state，关闭 SecretModal 即丢弃；不进 react-query 缓存
  const [issued, setIssued] = useState<AcpIssuedApiKey | null>(null);

  const PAGE_SIZE = 20;
  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['acp-keys', tenantId, page],
    enabled: tenantId !== '',
    queryFn: () =>
      unwrap<PagedResp<AcpApiKey>>(
        api.GET('/api/v1/admin/acp-gateway/tenants/{tenant_id}/api-keys', {
          params: {
            path: { tenant_id: tenantId },
            query: { limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE },
          },
        }),
      ),
  });

  const writeHint = isSuperAdmin ? undefined : t('readOnlyHint');
  const lacking = missingScopes(scopes, required);

  const openIssue = () => {
    setKeyName('');
    setScopes([...required]);
    setExpiresDate('');
    setRate('');
    setIssueOpen(true);
  };

  const issue = async (reason: string) => {
    if (scopes.length === 0) {
      toast.error(t('keys.scopesRequired'));
      return;
    }
    const rps = rate.trim() === '' ? undefined : Number(rate);
    if (rps !== undefined && !(rps > 0 && rps <= 10000)) {
      toast.error(t('keys.rateInvalid'));
      return;
    }
    setBusy(true);
    try {
      const created = await unwrap<AcpIssuedApiKey>(
        api.POST('/api/v1/admin/acp-gateway/tenants/{tenant_id}/api-keys', {
          params: { path: { tenant_id: tenantId } },
          body: {
            name: keyName.trim() || undefined,
            scopes: scopes as Scope[],
            expires_at: expiryFromDate(expiresDate),
            rate_limit_rps: rps,
            reason,
          },
        }),
      );
      toast.success(t('keys.issued'));
      setIssueOpen(false);
      setIssued(created);
      void queryClient.invalidateQueries({ queryKey: ['acp-keys'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('keys.issueFailed'));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (reason: string) => {
    if (!revokeTarget) return;
    setBusy(true);
    try {
      await unwrap(
        api.POST('/api/v1/admin/acp-gateway/api-keys/{key_id}/revoke', {
          params: { path: { key_id: revokeTarget.id } },
          body: { reason },
        }),
      );
      toast.success(t('keys.revoked'));
      setRevokeTarget(null);
      void queryClient.invalidateQueries({ queryKey: ['acp-keys'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  const columns: ColumnDef<AcpApiKey, unknown>[] = [
    {
      accessorKey: 'name',
      header: t('field.name'),
      cell: ({ row }) => row.original.name || '-',
    },
    {
      accessorKey: 'prefix',
      header: t('keys.col.prefix'),
      cell: ({ row }) => (
        <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
          {row.original.prefix}…
        </code>
      ),
    },
    {
      accessorKey: 'scopes',
      header: t('keys.col.scopes'),
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-1">
          {row.original.scopes.map((s) => (
            <Badge key={s} variant="outline">
              {s}
            </Badge>
          ))}
        </div>
      ),
    },
    {
      id: 'status',
      header: t('field.status'),
      cell: ({ row }) => {
        const s = keyStatus(row.original);
        return <StatusBadge tone={STATUS_TONE[s]}>{t(`status.${s}`)}</StatusBadge>;
      },
    },
    {
      accessorKey: 'rate_limit_rps',
      header: t('keys.col.rate'),
      cell: ({ row }) => row.original.rate_limit_rps ?? '-',
    },
    {
      accessorKey: 'expires_at',
      header: t('keys.col.expiresAt'),
      cell: ({ row }) =>
        row.original.expires_at ? formatDateTime(row.original.expires_at) : t('keys.neverExpires'),
    },
    {
      accessorKey: 'last_used_at',
      header: t('keys.col.lastUsedAt'),
      cell: ({ row }) =>
        row.original.last_used_at ? formatDateTime(row.original.last_used_at) : t('keys.neverUsed'),
    },
    {
      accessorKey: 'created_at',
      header: t('field.createdAt'),
      cell: ({ row }) => formatDateTime(row.original.created_at),
    },
    {
      id: 'actions',
      header: t('field.actions'),
      cell: ({ row }) => (
        <Button
          variant="link"
          size="sm"
          className="text-destructive hover:text-destructive"
          disabled={!isSuperAdmin || keyStatus(row.original) === 'revoked'}
          title={writeHint}
          onClick={() => setRevokeTarget(row.original)}
        >
          {t('keys.revoke')}
        </Button>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('keys.title')}
        description={t('keys.description')}
        actions={
          <Button disabled={!isSuperAdmin || !tenantId} title={writeHint} onClick={openIssue}>
            {t('keys.issue')}
          </Button>
        }
      />

      {cfg && !cfg.admin_token_configured && (
        <Alert variant="destructive">
          <AlertDescription>{t('notConfigured')}</AlertDescription>
        </Alert>
      )}
      {/* 合同 §5.3：Key 需要的 scope */}
      <Alert>
        <AlertDescription className="space-y-1">
          <p>{t('keys.scopeHint', { scopes: required.join(' / ') })}</p>
          <p>
            {t('keys.fillHint')}{' '}
            <Link to="/settings/agent" className="underline">
              /settings/agent
            </Link>
          </p>
        </AlertDescription>
      </Alert>

      <div className="flex items-center gap-2">
        <Label>{t('keys.tenantLabel')}</Label>
        <Select
          value={tenantId}
          onValueChange={(v) => {
            setPage(1);
            setParams({ tenant: v });
          }}
        >
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
        {tenants && tenants.items.length === 0 && (
          <span className="text-sm text-muted-foreground">{t('keys.noTenants')}</span>
        )}
      </div>

      <DataTable
        columns={columns}
        data={data?.items}
        loading={isFetching}
        error={isError ? error : undefined}
        total={data?.total}
        page={page}
        pageSize={PAGE_SIZE}
        onPageChange={setPage}
      />

      <ReasonModal
        open={issueOpen}
        title={t('keys.issueTitle')}
        description={t('keys.issueDescription')}
        okText={t('keys.issue')}
        confirmLoading={busy}
        onOk={issue}
        onCancel={() => setIssueOpen(false)}
      >
        <div className="space-y-4 pb-4">
          <div className="space-y-1.5">
            <Label htmlFor="acp-key-name">{t('keys.nameLabel')}</Label>
            <Input
              id="acp-key-name"
              maxLength={256}
              placeholder={t('keys.namePlaceholder')}
              value={keyName}
              onChange={(e) => setKeyName(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('keys.scopesLabel')}</Label>
            <div className="grid grid-cols-2 gap-2">
              {ALL_SCOPES.map((s) => (
                <label key={s} className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={scopes.includes(s)}
                    onCheckedChange={(on) =>
                      setScopes((cur) => (on ? [...cur, s] : cur.filter((x) => x !== s)))
                    }
                  />
                  <code className="font-mono text-xs">{s}</code>
                  {required.includes(s) && <Badge variant="secondary">{t('keys.required')}</Badge>}
                </label>
              ))}
            </div>
            {lacking.length > 0 && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                {t('keys.scopesMissing', { scopes: lacking.join(' / ') })}
              </p>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="acp-key-expires">{t('keys.expiresLabel')}</Label>
              <Input
                id="acp-key-expires"
                type="date"
                value={expiresDate}
                onChange={(e) => setExpiresDate(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="acp-key-rate">{t('keys.rateLabel')}</Label>
              <Input
                id="acp-key-rate"
                type="number"
                min={0}
                value={rate}
                onChange={(e) => setRate(e.target.value)}
              />
            </div>
          </div>
        </div>
      </ReasonModal>

      <ReasonModal
        open={revokeTarget !== null}
        title={t('keys.revokeTitle', { name: revokeTarget?.name || revokeTarget?.prefix || '' })}
        description={t('keys.revokeDescription')}
        danger
        confirmLoading={busy}
        onOk={revoke}
        onCancel={() => setRevokeTarget(null)}
      />

      <SecretModal
        open={issued !== null}
        title={t('keys.secretTitle', { name: issued?.name || issued?.prefix || '' })}
        fields={issued ? [{ label: t('keys.secretLabel'), value: issued.api_key }] : []}
        onClose={() => setIssued(null)}
      />
    </div>
  );
}
