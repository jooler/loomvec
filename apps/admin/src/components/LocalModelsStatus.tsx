/**
 * 本地模型推理状态卡（docs/16）：vLLM / Infinity 运行状态、served 模型、
 * 「配置指向本地但不可用」的通道警告。Overview 与 System 页共用；
 * 纯云端部署（system status.local_models.enabled=false）由调用方不渲染。
 */
import { useTranslation } from 'react-i18next';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import type { LocalModelServiceStatus, SystemStatus } from '@/types';
import { AI_CHANNEL_LABEL } from '@/constants';

type LocalModels = NonNullable<SystemStatus['local_models']>;

export function LocalModelsStatus({ data }: { data: LocalModels }) {
  const { t } = useTranslation('system');
  const services: { name: string; status: LocalModelServiceStatus }[] = [
    { name: 'vLLM', status: data.vllm },
    { name: 'Infinity', status: data.infinity },
  ];
  const degraded = Object.entries(data.channels)
    .filter(([, c]) => c.configured_local && !c.available)
    .map(([ch]) => AI_CHANNEL_LABEL[ch] ?? ch);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('localModels.title')}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {services.map(({ name, status }) => (
          <div key={name} className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={status.ok ? 'green' : 'red'}>
              {name} :{status.port}
            </StatusBadge>
            {status.ok ? (
              status.models.length ? (
                status.models.map((m) => (
                  <Badge key={m} variant="outline">
                    {m}
                  </Badge>
                ))
              ) : (
                <span className="text-xs text-muted-foreground">{t('localModels.noModels')}</span>
              )
            ) : (
              <span className="text-xs text-destructive">
                {status.error ?? t('localModels.notRunning')}
              </span>
            )}
          </div>
        ))}
        {degraded.length > 0 && (
          <p className="text-xs text-destructive">
            {t('localModels.degraded', { channels: degraded.join('、') })}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
