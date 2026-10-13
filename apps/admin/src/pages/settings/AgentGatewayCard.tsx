/**
 * 「系统配置 → 智能体」顶部的 ACP 网关接入概览：网关地址只读、两个凭据是否已配置，
 * 以及 Key 需要的 scope / 租户 MCP 白名单提示（P2 合同 §5.2/§5.3）。
 * 凭据本身在下方配置表里只写不读；本卡只展示「是否已配置」。
 */
import { useTranslation } from 'react-i18next';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Alert, AlertDescription } from '@loomvec/ui/components/ui/alert';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';
import { useAcpConfig } from '@/pages/acp/useAcp';

export function AgentGatewayCard() {
  const { t } = useTranslation('settings');
  const { data, isLoading, isError } = useAcpConfig();

  const flag = (ok: boolean) =>
    ok ? (
      <StatusBadge tone="green">{t('agent.configured')}</StatusBadge>
    ) : (
      <StatusBadge tone="amber">{t('agent.unconfigured')}</StatusBadge>
    );

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('agent.gatewayTitle')}</CardTitle>
        <CardDescription>{t('agent.gatewayDescription')}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <p className="text-sm text-muted-foreground">{t('loading')}</p>
        ) : isError || !data ? (
          <p className="text-sm text-destructive">{t('agent.configLoadFailed')}</p>
        ) : (
          <>
            <DescriptionList cols={3}>
              <DescriptionItem label={t('agent.url')}>
                <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{data.url}</code>
                <p className="mt-1 text-xs text-muted-foreground">{t('agent.urlHint')}</p>
              </DescriptionItem>
              <DescriptionItem label={t('agent.apiKey')}>
                {flag(data.api_key_configured)}
              </DescriptionItem>
              <DescriptionItem label={t('agent.adminToken')}>
                {flag(data.admin_token_configured)}
              </DescriptionItem>
            </DescriptionList>
            <Alert>
              <AlertDescription className="space-y-1">
                <p>{t('agent.apiKeyHint', { scopes: data.required_key_scopes.join(' / ') })}</p>
                <p>{t('agent.adminTokenHint')}</p>
                <p>
                  {t('agent.mcpHint', {
                    host:
                      data.mcp_sandbox.host ?? data.mcp_sandbox.hostname ?? data.mcp_sandbox.url,
                  })}
                </p>
              </AlertDescription>
            </Alert>
          </>
        )}
      </CardContent>
    </Card>
  );
}
