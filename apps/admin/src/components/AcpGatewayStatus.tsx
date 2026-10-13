/**
 * ACP 网关运行时状态卡（参考 LocalModelsStatus）：可达 / 健康 / 运行方式 / 可用 agent / 运行中回合。
 * 数据来自 GET /admin/acp-gateway/status，网关故障时后端降级为 error 文案而不是报错。
 */
import { useTranslation } from 'react-i18next';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import type { AcpStatus } from '@/types';

export function AcpGatewayStatus({ data }: { data: AcpStatus }) {
  const { t } = useTranslation('acp');
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {t('runtime.cardTitle')}
          <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs font-normal">
            {data.url}
          </code>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge tone={data.reachable ? 'green' : 'red'}>
            {data.reachable ? t('runtime.reachable') : t('runtime.unreachable')}
          </StatusBadge>
          {data.reachable && (
            <StatusBadge tone={data.healthy ? 'green' : 'amber'}>
              {data.healthy ? t('runtime.healthy') : t('runtime.unhealthy')}
            </StatusBadge>
          )}
          {data.provider && (
            <span className="text-xs text-muted-foreground">
              {t('runtime.provider')}：{data.provider}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{t('runtime.agents')}：</span>
          {data.agents.length ? (
            data.agents.map((a) => (
              <Badge key={a} variant="outline">
                {a}
              </Badge>
            ))
          ) : (
            <span>{t('runtime.noAgents')}</span>
          )}
          <span className="ml-4">
            {t('runtime.runningTurns')}：{data.running_turns}
          </span>
        </div>
        {data.error && <p className="text-xs text-destructive">{data.error}</p>}
      </CardContent>
    </Card>
  );
}
