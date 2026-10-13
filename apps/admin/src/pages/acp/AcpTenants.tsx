import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { ReasonModal } from '@/components/ReasonModal';
import { DataTable } from '@loomvec/ui/components/data-table';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Alert, AlertDescription } from '@loomvec/ui/components/ui/alert';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { Textarea } from '@loomvec/ui/components/ui/textarea';
import type { AcpTenant, PagedResp } from '@/types';
import { formatDateTime } from '@/utils';
import {
  QUOTA_KEYS,
  formToQuotas,
  mcpHostAllowed,
  parseHostLines,
  quotasToForm,
  type QuotaFormValues,
} from './helpers';
import { useAcpConfig } from './useAcp';

const PAGE_SIZE = 20;
const AGENTS = ['claude', 'dsh'] as const;

type DialogState =
  | { kind: 'create' }
  | { kind: 'quota'; tenant: AcpTenant }
  | { kind: 'hosts'; tenant: AcpTenant }
  | { kind: 'status'; tenant: AcpTenant; next: 'active' | 'suspended' };

/** ACP 租户与配额：创建 / 暂停恢复 / 配额设置 / MCP 白名单（写操作仅 super_admin，且必填理由入审计）。 */
export function AcpTenantsPage() {
  const { isSuperAdmin } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('acp');
  const { data: cfg } = useAcpConfig();

  const [searchText, setSearchText] = useState('');
  const [q, setQ] = useState('');
  const [page, setPage] = useState(1);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [busy, setBusy] = useState(false);
  // 弹窗内表单状态（关闭时随 dialog 重置）
  const [name, setName] = useState('');
  const [agent, setAgent] = useState<string>('claude');
  const [quotaForm, setQuotaForm] = useState<QuotaFormValues>(quotasToForm(undefined));
  const [hostsText, setHostsText] = useState('');

  const limit = PAGE_SIZE;
  const offset = (page - 1) * PAGE_SIZE;
  const { data, isFetching, isError, error } = useQuery({
    queryKey: ['acp-tenants', q, limit, offset],
    queryFn: () =>
      unwrap<PagedResp<AcpTenant>>(
        api.GET('/api/v1/admin/acp-gateway/tenants', {
          params: { query: { q: q || undefined, limit, offset } },
        }),
      ),
  });

  const sandbox = cfg?.mcp_sandbox;
  const sandboxHost = sandbox?.host ?? sandbox?.hostname ?? '';
  const writeHint = isSuperAdmin ? undefined : t('readOnlyHint');

  const open = (d: DialogState) => {
    if (d.kind === 'create') {
      setName('');
      setAgent('claude');
    } else if (d.kind === 'quota') {
      setQuotaForm(quotasToForm(d.tenant.quotas));
    } else if (d.kind === 'hosts') {
      setHostsText((d.tenant.settings.mcp?.allowed_hosts ?? []).join('\n'));
    }
    setDialog(d);
  };

  /** 统一的写操作壳：busy / 成功提示 / 失效缓存 / 错误 toast。 */
  const run = async (action: () => Promise<unknown>, okMessage: string) => {
    setBusy(true);
    try {
      await action();
      toast.success(okMessage);
      setDialog(null);
      void queryClient.invalidateQueries({ queryKey: ['acp-tenants'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  const patch = (tenant: AcpTenant, body: Record<string, unknown>, reason: string) =>
    unwrap(
      api.PATCH('/api/v1/admin/acp-gateway/tenants/{tenant_id}', {
        params: { path: { tenant_id: tenant.id } },
        body: { ...body, reason },
      }),
    );

  const submitCreate = async (reason: string) => {
    if (!name.trim()) {
      toast.error(t('tenants.nameRequired'));
      return;
    }
    await run(
      () =>
        unwrap(
          api.POST('/api/v1/admin/acp-gateway/tenants', {
            body: {
              name: name.trim(),
              default_agent: agent as (typeof AGENTS)[number],
              reason,
            },
          }),
        ),
      t('tenants.created'),
    );
  };

  const submitQuota = async (reason: string) => {
    if (dialog?.kind !== 'quota') return;
    const quotas = formToQuotas(quotaForm);
    if (quotas === null) {
      toast.error(t('tenants.quotaInvalid'));
      return;
    }
    await run(() => patch(dialog.tenant, { quotas }, reason), t('tenants.updated'));
  };

  const submitHosts = async (reason: string) => {
    if (dialog?.kind !== 'hosts') return;
    const { tenant } = dialog;
    const settings = {
      ...tenant.settings,
      mcp: { ...tenant.settings.mcp, allowed_hosts: parseHostLines(hostsText), allow_stdio: false },
    };
    await run(() => patch(tenant, { settings }, reason), t('tenants.updated'));
  };

  const submitStatus = async (reason: string) => {
    if (dialog?.kind !== 'status') return;
    await run(() => patch(dialog.tenant, { status: dialog.next }, reason), t('tenants.updated'));
  };

  const columns: ColumnDef<AcpTenant, unknown>[] = [
    {
      accessorKey: 'name',
      header: t('field.name'),
      cell: ({ row }) => (
        <div>
          <p className="text-sm font-medium">{row.original.name}</p>
          <p className="font-mono text-xs text-muted-foreground">{row.original.id}</p>
        </div>
      ),
    },
    {
      accessorKey: 'status',
      header: t('field.status'),
      cell: ({ row }) =>
        row.original.status === 'active' ? (
          <StatusBadge tone="green">{t('status.active')}</StatusBadge>
        ) : (
          <StatusBadge tone="red">{t('status.suspended')}</StatusBadge>
        ),
    },
    { accessorKey: 'default_agent', header: t('tenants.col.agent') },
    {
      id: 'quotas',
      header: t('tenants.col.quotas'),
      cell: ({ row }) => {
        const entries = QUOTA_KEYS.filter((k) => row.original.quotas[k] !== undefined);
        return entries.length ? (
          <div className="space-y-0.5 text-xs">
            {entries.map((k) => (
              <p key={k}>
                <span className="text-muted-foreground">{t(`quota.${k}`)}：</span>
                {row.original.quotas[k]?.toLocaleString()}
              </p>
            ))}
          </div>
        ) : (
          <span className="text-sm text-muted-foreground">{t('unlimited')}</span>
        );
      },
    },
    {
      id: 'mcp',
      header: t('tenants.col.mcp'),
      cell: ({ row }) => {
        if (!sandbox) return '-';
        return mcpHostAllowed(row.original.settings.mcp?.allowed_hosts, sandbox) ? (
          <StatusBadge tone="green">{t('tenants.mcpOk')}</StatusBadge>
        ) : (
          <StatusBadge tone="amber">{t('tenants.mcpMissing', { host: sandboxHost })}</StatusBadge>
        );
      },
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
        const r = row.original;
        return (
          <div className="flex flex-wrap gap-1">
            <Button
              variant="link"
              size="sm"
              disabled={!isSuperAdmin}
              title={writeHint}
              onClick={() => open({ kind: 'quota', tenant: r })}
            >
              {t('tenants.action.quota')}
            </Button>
            <Button
              variant="link"
              size="sm"
              disabled={!isSuperAdmin}
              title={writeHint}
              onClick={() => open({ kind: 'hosts', tenant: r })}
            >
              {t('tenants.action.hosts')}
            </Button>
            <Button
              variant="link"
              size="sm"
              onClick={() => navigate(`/acp/api-keys?tenant=${encodeURIComponent(r.id)}`)}
            >
              {t('tenants.action.keys')}
            </Button>
            <Button
              variant="link"
              size="sm"
              onClick={() => navigate(`/acp/usage?tenant=${encodeURIComponent(r.id)}`)}
            >
              {t('tenants.action.usage')}
            </Button>
            <Button
              variant="link"
              size="sm"
              className={r.status === 'active' ? 'text-destructive hover:text-destructive' : ''}
              disabled={!isSuperAdmin}
              title={writeHint}
              onClick={() =>
                open({
                  kind: 'status',
                  tenant: r,
                  next: r.status === 'active' ? 'suspended' : 'active',
                })
              }
            >
              {r.status === 'active' ? t('tenants.action.suspend') : t('tenants.action.activate')}
            </Button>
          </div>
        );
      },
    },
  ];

  const tenantName = dialog && 'tenant' in dialog ? dialog.tenant.name : '';

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('tenants.title')}
        description={t('tenants.description')}
        actions={
          <Button
            disabled={!isSuperAdmin}
            title={writeHint}
            onClick={() => open({ kind: 'create' })}
          >
            {t('tenants.create')}
          </Button>
        }
      />

      {cfg && !cfg.admin_token_configured && (
        <Alert variant="destructive">
          <AlertDescription>{t('notConfigured')}</AlertDescription>
        </Alert>
      )}
      {/* 合同 §5.3：租户 MCP 白名单必须覆盖 agent.mcp.url_sandbox 的主机 */}
      <Alert>
        <AlertDescription>
          {t('tenants.mcpHint', { host: sandboxHost || 'agent.mcp.url_sandbox' })}
        </AlertDescription>
      </Alert>

      <form
        className="flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setQ(searchText);
          setPage(1);
        }}
      >
        <Input
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          placeholder={t('tenants.searchPlaceholder')}
          className="w-72"
        />
        <Button type="submit" variant="outline" size="icon" aria-label={t('action.search')}>
          <Search />
        </Button>
      </form>

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
        open={dialog?.kind === 'create'}
        title={t('tenants.createTitle')}
        description={t('tenants.createDescription')}
        okText={t('action.create')}
        confirmLoading={busy}
        onOk={submitCreate}
        onCancel={() => setDialog(null)}
      >
        <div className="space-y-4 pb-4">
          <div className="space-y-1.5">
            <Label htmlFor="acp-tenant-name">{t('tenants.nameLabel')}</Label>
            <Input
              id="acp-tenant-name"
              maxLength={255}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label>{t('tenants.defaultAgentLabel')}</Label>
            <Select value={agent} onValueChange={setAgent}>
              <SelectTrigger className="w-60">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {AGENTS.map((a) => (
                  <SelectItem key={a} value={a}>
                    {t(`agent.${a}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
      </ReasonModal>

      <ReasonModal
        open={dialog?.kind === 'quota'}
        title={t('tenants.quotaTitle', { name: tenantName })}
        description={t('tenants.quotaDescription')}
        okText={t('action.save')}
        confirmLoading={busy}
        onOk={submitQuota}
        onCancel={() => setDialog(null)}
      >
        <div className="grid grid-cols-2 gap-3 pb-4">
          {QUOTA_KEYS.map((k) => (
            <div key={k} className="space-y-1">
              <Label htmlFor={`acp-quota-${k}`} className="text-xs">
                {t(`quota.${k}`)}
              </Label>
              <Input
                id={`acp-quota-${k}`}
                type="number"
                min={0}
                placeholder={t('unlimited')}
                value={quotaForm[k]}
                onChange={(e) => setQuotaForm((f) => ({ ...f, [k]: e.target.value }))}
              />
            </div>
          ))}
        </div>
      </ReasonModal>

      <ReasonModal
        open={dialog?.kind === 'hosts'}
        title={t('tenants.hostsTitle', { name: tenantName })}
        description={t('tenants.hostsDescription')}
        okText={t('action.save')}
        confirmLoading={busy}
        onOk={submitHosts}
        onCancel={() => setDialog(null)}
      >
        <div className="space-y-2 pb-4">
          <Label htmlFor="acp-hosts">{t('tenants.hostsLabel')}</Label>
          <Textarea
            id="acp-hosts"
            rows={5}
            className="font-mono text-xs"
            value={hostsText}
            onChange={(e) => setHostsText(e.target.value)}
          />
          {sandboxHost && !mcpHostAllowed(parseHostLines(hostsText), sandbox!) && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setHostsText((v) => [...parseHostLines(v), sandboxHost].join('\n'))}
            >
              {t('tenants.hostsFillRecommended', { host: sandboxHost })}
            </Button>
          )}
        </div>
      </ReasonModal>

      <ReasonModal
        open={dialog?.kind === 'status'}
        title={t(
          dialog?.kind === 'status' && dialog.next === 'active'
            ? 'tenants.activateTitle'
            : 'tenants.suspendTitle',
          { name: tenantName },
        )}
        description={t(
          dialog?.kind === 'status' && dialog.next === 'active'
            ? 'tenants.activateDescription'
            : 'tenants.suspendDescription',
        )}
        danger={dialog?.kind === 'status' && dialog.next === 'suspended'}
        confirmLoading={busy}
        onOk={submitStatus}
        onCancel={() => setDialog(null)}
      />
    </div>
  );
}
