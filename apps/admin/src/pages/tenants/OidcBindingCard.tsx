/** 租户 OIDC 域绑定卡片：绑定/更新表单（空 secret 保留原值），从 TenantDetail 拆出。 */
import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { t as tt } from '@/i18n';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { Switch } from '@loomvec/ui/components/ui/switch';
import type { OidcBinding } from '@/types';

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const bindingSchema = z.object({
  domain: z.string().min(1, tt('tenants:oidc.domainRequired')).max(255),
  issuer: z.string().min(1, tt('tenants:oidc.issuerRequired')).max(512),
  client_id: z.string().min(1, tt('tenants:oidc.clientIdRequired')).max(255),
  client_secret: z.string().max(512),
  enabled: z.boolean(),
});

type BindingValues = z.infer<typeof bindingSchema>;

const EMPTY_VALUES: BindingValues = {
  domain: '',
  issuer: '',
  client_id: '',
  client_secret: '',
  enabled: true,
};

export function OidcBindingCard(props: { tenantId: string; canWrite: boolean }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const { t } = useTranslation('tenants');
  const bindingForm = useForm<BindingValues>({
    resolver: zodResolver(bindingSchema),
    defaultValues: EMPTY_VALUES,
  });

  const bindingKey = ['admin-tenant-oidc', props.tenantId];
  const { data: binding } = useQuery({
    queryKey: bindingKey,
    queryFn: () =>
      unwrap<OidcBinding | null>(
        api.GET('/api/v1/admin/tenants/{tenant_id}/oidc-binding', {
          params: { path: { tenant_id: props.tenantId } },
        }),
      ),
  });

  // 绑定记录变更时回填表单；依赖 tenantId，直接在租户详情间跳转（同为未绑定时
  // binding.id 不变）也能清掉上一租户的草稿
  useEffect(() => {
    bindingForm.reset(
      binding
        ? {
            domain: binding.domain,
            issuer: binding.issuer,
            client_id: binding.client_id,
            client_secret: '',
            enabled: binding.enabled,
          }
        : EMPTY_VALUES,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.tenantId, binding?.id]);

  /** OIDC 绑定保存：空 secret 保留原值（后端语义）。 */
  const saveBinding = async (values: BindingValues) => {
    if (!binding?.has_secret && !values.client_secret) {
      bindingForm.setError('client_secret', { message: t('oidc.clientSecretRequired') });
      return;
    }
    setBusy(true);
    try {
      await unwrap(
        api.PUT('/api/v1/admin/tenants/{tenant_id}/oidc-binding', {
          params: { path: { tenant_id: props.tenantId } },
          body: { ...values, client_secret: values.client_secret ?? '' },
        }),
      );
      toast.success(t('oidc.saved'));
      void queryClient.invalidateQueries({ queryKey: bindingKey });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('oidc.saveFailed'));
    } finally {
      setBusy(false);
    }
  };
  const submitBinding = bindingForm.handleSubmit(saveBinding);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('oidc.title')}</CardTitle>
        <CardAction>
          {binding ? (
            <span className="text-sm">{t('oidc.boundHint')}</span>
          ) : (
            <span className="text-sm text-muted-foreground">{t('oidc.unboundHint')}</span>
          )}
        </CardAction>
      </CardHeader>
      <CardContent>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submitBinding();
          }}
          className="max-w-[560px] space-y-4"
        >
          <div className="space-y-2">
            <Label htmlFor="binding-domain">{t('oidc.domainLabel')}</Label>
            <Input id="binding-domain" maxLength={255} {...bindingForm.register('domain')} />
            <p className="text-xs text-muted-foreground">{t('oidc.domainHint')}</p>
            {bindingForm.formState.errors.domain && (
              <p className="text-sm text-destructive">
                {bindingForm.formState.errors.domain.message}
              </p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="binding-issuer">Issuer</Label>
            <Input
              id="binding-issuer"
              maxLength={512}
              placeholder="https://idp.example.com"
              {...bindingForm.register('issuer')}
            />
            {bindingForm.formState.errors.issuer && (
              <p className="text-sm text-destructive">
                {bindingForm.formState.errors.issuer.message}
              </p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="binding-client-id">Client ID</Label>
            <Input id="binding-client-id" maxLength={255} {...bindingForm.register('client_id')} />
            {bindingForm.formState.errors.client_id && (
              <p className="text-sm text-destructive">
                {bindingForm.formState.errors.client_id.message}
              </p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="binding-client-secret">Client Secret</Label>
            <Input
              id="binding-client-secret"
              type="password"
              maxLength={512}
              autoComplete="new-password"
              {...bindingForm.register('client_secret')}
            />
            <p className="text-xs text-muted-foreground">
              {binding?.has_secret ? t('oidc.secretSetHint') : t('oidc.secretRequiredHint')}
            </p>
            {bindingForm.formState.errors.client_secret && (
              <p className="text-sm text-destructive">
                {bindingForm.formState.errors.client_secret.message}
              </p>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Switch
              id="binding-enabled"
              checked={bindingForm.watch('enabled')}
              onCheckedChange={(v) => bindingForm.setValue('enabled', v)}
            />
            <Label htmlFor="binding-enabled">{t('action.enable')}</Label>
          </div>
          <Button type="submit" disabled={busy || !props.canWrite}>
            {busy ? t('action.saving') : t('oidc.saveBinding')}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
