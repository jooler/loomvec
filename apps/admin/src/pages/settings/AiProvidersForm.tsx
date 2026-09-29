/**
 * AI 供方分层表单（settings/ai 专用，docs/16-本地模型推理.md）：
 * - 每通道（对话 LLM / 向量化 / 重排 / VLM / CLIP）选「本地 GPU / 云端」运行位置；
 * - 本地档可用性来自 GET /admin/settings/ai/local-runtime 探测——服务未运行或
 *   未加载该通道模型时禁选；VLM 未本地化，仅云端；
 * - 选本地 = 一键写入本地标准配置（base_url/model/api_key=local/api_style），
 *   可改模型名；选云端展开 base_url/api_key/model（重排与 CLIP 另有 api_style）；
 * - 保存 = 对变更键逐个 PUT /admin/settings/{key}（理由入审计），重启后生效。
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Alert, AlertDescription } from '@loomvec/ui/components/ui/alert';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { ReasonModal } from '@/components/ReasonModal';
import { AI_CHANNEL_LABEL } from '@/constants';
import type { AiLocalRuntime, SettingItem } from '@/types';
import {
  AI_CHANNELS,
  buildChanges,
  CHANNELS_WITH_API_STYLE,
  CHANNEL_SERVICE,
  draftFromItems,
  isLocalBase,
  type AiChannel,
  type AiChannelDraft,
} from './aiProviders';

const CLOUD_BASE_URL_PLACEHOLDER: Record<AiChannel, string> = {
  llm: 'https://api.deepseek.com',
  embedding: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  rerank: 'https://dashscope.aliyuncs.com/api/v1',
  vlm: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  clip: 'https://dashscope.aliyuncs.com/api/v1',
};

export function AiProvidersForm({ items }: { items: SettingItem[] }) {
  const { t } = useTranslation('settings');
  const { isSuperAdmin } = usePerm();
  const queryClient = useQueryClient();

  const {
    data: runtime,
    isError: runtimeError,
    isFetching: runtimeFetching,
  } = useQuery({
    queryKey: ['ai-local-runtime'],
    queryFn: () => unwrap<AiLocalRuntime>(api.GET('/api/v1/admin/settings/ai/local-runtime')),
    refetchInterval: 30_000,
    retry: 1,
  });

  const initial = useMemo(
    () =>
      draftFromItems(items, {
        vllm: runtime?.services.vllm.port ?? 38010,
        infinity: runtime?.services.infinity.port ?? 38011,
      }),
    [items, runtime],
  );
  const [draft, setDraft] = useState<Record<AiChannel, AiChannelDraft>>(initial);
  useEffect(() => setDraft(initial), [initial]);

  const setChannel = (ch: AiChannel, patch: Partial<AiChannelDraft>) =>
    setDraft((d) => ({ ...d, [ch]: { ...d[ch], ...patch } }));

  const changes = useMemo(
    () => buildChanges(draft, initial, runtime?.suggestions),
    [draft, initial, runtime],
  );

  const [saveOpen, setSaveOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const save = async (reason: string) => {
    if (changes.length === 0) return;
    setBusy(true);
    try {
      for (const c of changes) {
        await unwrap(
          api.PUT('/api/v1/admin/settings/{key}', {
            params: { path: { key: c.key } },
            body: { value: c.value, reason },
          }),
        );
      }
      toast.success(t('ai.form.savedToast', { count: changes.length }));
      setSaveOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['admin-settings'] });
      void queryClient.invalidateQueries({ queryKey: ['ai-local-runtime'] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('saveFailed'));
    } finally {
      setBusy(false);
    }
  };

  const keyIsSet = (ch: AiChannel) =>
    items.some((s) => s.key === `ai.${ch}.api_key` && s.value != null);

  return (
    <div className="space-y-4">
      <Alert>
        <AlertDescription>{t('ai.form.restartHint')}</AlertDescription>
      </Alert>
      {runtimeError && <Alert><AlertDescription>{t('ai.form.runtimeLoadFailed')}</AlertDescription></Alert>}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {AI_CHANNELS.map((ch) => {
          const d = draft[ch];
          const init = initial[ch];
          const s = runtime?.suggestions[ch];
          const service = CHANNEL_SERVICE[ch];
          const serviceStatus = service ? runtime?.services[service] : undefined;
          const localAvailable = Boolean(s?.available);
          const configuredLocal =
            service !== null && isLocalBase(init.baseUrl, service === 'vllm'
              ? (runtime?.services.vllm.port ?? 38010)
              : (runtime?.services.infinity.port ?? 38011));
          const localButUnavailable = configuredLocal && !localAvailable;

          return (
            <Card key={ch}>
              <CardHeader className="flex-row items-center justify-between space-y-0">
                <CardTitle className="text-base">{AI_CHANNEL_LABEL[ch] ?? ch}</CardTitle>
                <Badge variant="outline">
                  {service === null
                    ? t('ai.form.locationCloudOnly')
                    : d.location === 'local'
                      ? t('ai.form.locationLocal')
                      : t('ai.form.locationCloud')}
                </Badge>
              </CardHeader>
              <CardContent className="space-y-3">
                {/* 本地推理服务状态行 */}
                {serviceStatus && (
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <StatusBadge tone={serviceStatus.ok ? 'green' : 'red'}>
                      {service === 'vllm' ? 'vLLM' : 'Infinity'} :{serviceStatus.port}
                    </StatusBadge>
                    {serviceStatus.ok ? (
                      <span className="text-muted-foreground">
                        {t('ai.form.servedModels')}：
                        {serviceStatus.models.join('、') || '-'}
                      </span>
                    ) : (
                      <span className="text-destructive">{t('ai.form.notRunning')}</span>
                    )}
                  </div>
                )}

                {service !== null && (
                  <div className="space-y-1">
                    <Label>{t('ai.form.location')}</Label>
                    <Select
                      value={d.location}
                      onValueChange={(v) => {
                        const loc = v as 'local' | 'cloud';
                        if (loc === 'local') {
                          // 切到本地：模型名同步为本地标准 served 名（用户可再改）
                          setChannel(ch, { location: loc, model: s?.model ?? d.model });
                        } else {
                          // 切到云端：原值是本地端点时清空，引导填写云端地址
                          // （提交空串 = 清除该键 DB 覆盖，回落 config/loomvec.json）
                          const port =
                            service === 'vllm'
                              ? (runtime?.services.vllm.port ?? 38010)
                              : (runtime?.services.infinity.port ?? 38011);
                          setChannel(ch, {
                            location: loc,
                            baseUrl: isLocalBase(d.baseUrl, port) ? '' : d.baseUrl,
                          });
                        }
                      }}
                      disabled={!isSuperAdmin}
                    >
                      <SelectTrigger className="w-48">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="local" disabled={!localAvailable}>
                          {t('ai.form.locationLocal')}
                        </SelectItem>
                        <SelectItem value="cloud">{t('ai.form.locationCloud')}</SelectItem>
                      </SelectContent>
                    </Select>
                    {!localAvailable && (
                      <p className="text-xs text-muted-foreground">
                        {t('ai.form.localUnavailable')}
                      </p>
                    )}
                    {localButUnavailable && (
                      <p className="text-xs text-destructive">{t('ai.form.localDownWarning')}</p>
                    )}
                  </div>
                )}
                {service === null && (
                  <p className="text-xs text-muted-foreground">{t('ai.form.vlmCloudOnly')}</p>
                )}

                {service !== null && d.location === 'local' ? (
                  <div className="space-y-2">
                    <div className="grid gap-2 sm:grid-cols-2">
                      <div className="space-y-1">
                        <Label>base_url</Label>
                        <Input readOnly value={s?.base_url ?? d.baseUrl} className="font-mono text-xs" />
                      </div>
                      <div className="space-y-1">
                        <Label>{t('ai.form.model')}</Label>
                        <Input
                          value={d.model}
                          placeholder={s?.model}
                          onChange={(e) => setChannel(ch, { model: e.target.value })}
                          disabled={!isSuperAdmin}
                        />
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground">{t('ai.form.localNote')}</p>
                  </div>
                ) : (
                  <div className="space-y-2">
                    <div className="space-y-1">
                      <Label>base_url</Label>
                      <Input
                        value={d.baseUrl}
                        placeholder={CLOUD_BASE_URL_PLACEHOLDER[ch]}
                        onChange={(e) => setChannel(ch, { baseUrl: e.target.value })}
                        disabled={!isSuperAdmin}
                        className="font-mono text-xs"
                      />
                      <p className="text-xs text-muted-foreground">{t('ai.form.baseUrlFallbackHint')}</p>
                    </div>
                    <div className="grid gap-2 sm:grid-cols-2">
                      <div className="space-y-1">
                        <Label>API Key</Label>
                        <Input
                          type="password"
                          value={d.apiKey}
                          placeholder={
                            keyIsSet(ch) ? t('ai.form.apiKeyKeep') : t('ai.form.apiKeyEmpty')
                          }
                          onChange={(e) => setChannel(ch, { apiKey: e.target.value })}
                          disabled={!isSuperAdmin}
                        />
                      </div>
                      <div className="space-y-1">
                        <Label>{t('ai.form.model')}</Label>
                        <Input
                          value={d.model}
                          onChange={(e) => setChannel(ch, { model: e.target.value })}
                          disabled={!isSuperAdmin}
                        />
                      </div>
                    </div>
                    {CHANNELS_WITH_API_STYLE.includes(ch) && (
                      <div className="space-y-1">
                        <Label>api_style</Label>
                        <Select
                          value={d.apiStyle}
                          onValueChange={(v) => setChannel(ch, { apiStyle: v })}
                          disabled={!isSuperAdmin}
                        >
                          <SelectTrigger className="w-48">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="openai">openai</SelectItem>
                            <SelectItem value="dashscope">dashscope</SelectItem>
                            {ch === 'clip' && <SelectItem value="infinity">infinity</SelectItem>}
                          </SelectContent>
                        </Select>
                      </div>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>

      <div className="flex items-center gap-3">
        <Button
          disabled={!isSuperAdmin || busy || changes.length === 0}
          onClick={() => setSaveOpen(true)}
        >
          {t('ai.form.save')}
          {changes.length > 0 ? `（${changes.length}）` : ''}
        </Button>
        <Button variant="outline" disabled={busy} onClick={() => setDraft(initial)}>
          {t('ai.form.reset')}
        </Button>
        {runtimeFetching && (
          <span className="text-xs text-muted-foreground">{t('ai.form.probing')}</span>
        )}
      </div>

      <ReasonModal
        open={saveOpen}
        title={t('ai.form.saveTitle')}
        description={t('ai.form.saveDescription')}
        okText={t('ai.form.save')}
        confirmLoading={busy}
        onCancel={() => setSaveOpen(false)}
        onOk={save}
      />
    </div>
  );
}
