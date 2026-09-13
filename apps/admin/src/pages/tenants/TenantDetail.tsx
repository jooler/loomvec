import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { Switch } from '@loomvec/ui/components/ui/switch';
import type { OidcBinding, TenantRow } from '@/types';
import { formatDateTime, formatQuota } from '@/utils';

const quotaSchema = z.object({
  quota_storage_bytes: z.number({ error: '请输入存储配额' }).min(0),
  quota_file_count: z.number({ error: '请输入文件数配额' }).min(0),
});

const editSchema = z.object({
  name: z.string().min(1, '请输入租户名称').max(255),
  plan: z.string(),
});

const bindingSchema = z.object({
  domain: z.string().min(1, '请输入邮箱域').max(255),
  issuer: z.string().min(1, '请输入 Issuer').max(512),
  client_id: z.string().min(1, '请输入 Client ID').max(255),
  client_secret: z.string().max(512),
  enabled: z.boolean(),
});

type QuotaValues = z.infer<typeof quotaSchema>;
type EditValues = z.infer<typeof editSchema>;
type BindingValues = z.infer<typeof bindingSchema>;

/** 租户详情：用量报表 / 配额调整 / 编辑 / 停用启用 / OIDC 域绑定（docs/04 §5.2）。 */
export function TenantDetailPage() {
  const { tenantId } = useParams<{ tenantId: string }>();
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [quotaOpen, setQuotaOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [suspendOpen, setSuspendOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const quotaForm = useForm<QuotaValues>({
    resolver: zodResolver(quotaSchema),
    defaultValues: { quota_storage_bytes: 0, quota_file_count: 0 },
  });
  const editForm = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    defaultValues: { name: '', plan: '' },
  });
  const bindingForm = useForm<BindingValues>({
    resolver: zodResolver(bindingSchema),
    defaultValues: { domain: '', issuer: '', client_id: '', client_secret: '', enabled: true },
  });

  const tenantKey = ['admin-tenant', tenantId];
  const { data: tenant } = useQuery({
    queryKey: tenantKey,
    enabled: !!tenantId,
    queryFn: () =>
      unwrap<TenantRow>(
        api.GET('/api/v1/admin/tenants/{tenant_id}', {
          params: { path: { tenant_id: tenantId! } },
        }),
      ),
  });

  const { data: binding } = useQuery({
    queryKey: ['admin-tenant-oidc', tenantId],
    enabled: !!tenantId,
    queryFn: () =>
      unwrap<OidcBinding | null>(
        api.GET('/api/v1/admin/tenants/{tenant_id}/oidc-binding', {
          params: { path: { tenant_id: tenantId! } },
        }),
      ),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: tenantKey });
    void queryClient.invalidateQueries({ queryKey: ['admin-tenants'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
  };

  // 绑定记录变更时回填表单（对应旧 Form key={binding?.id ?? 'new'} 重挂载语义）
  const bindingId = binding?.id ?? 'new';
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
        : { domain: '', issuer: '', client_id: '', client_secret: '', enabled: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bindingId]);

  const activateTenant = async () => {
    try {
      await unwrap(
        api.POST('/api/v1/admin/tenants/{tenant_id}/activate', {
          params: { path: { tenant_id: tenantId! } },
        }),
      );
      toast.success('租户已启用');
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '操作失败');
    }
  };

  /** OIDC 绑定保存：空 secret 保留原值（后端语义）。 */
  const saveBinding = async (values: BindingValues) => {
    if (!binding?.has_secret && !values.client_secret) {
      bindingForm.setError('client_secret', { message: '请输入 Client Secret' });
      return;
    }
    setBusy(true);
    try {
      await unwrap(
        api.PUT('/api/v1/admin/tenants/{tenant_id}/oidc-binding', {
          params: { path: { tenant_id: tenantId! } },
          body: { ...values, client_secret: values.client_secret ?? '' },
        }),
      );
      toast.success('OIDC 域绑定已保存');
      void queryClient.invalidateQueries({ queryKey: ['admin-tenant-oidc', tenantId] });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '保存失败');
    } finally {
      setBusy(false);
    }
  };
  const submitBinding = bindingForm.handleSubmit(saveBinding);

  return (
    <div className="space-y-4">
      <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/tenants')}>
        <ArrowLeft /> 返回
      </Button>

      <PageHeader
        title={tenant?.name ?? '租户详情'}
        description={tenant?.status === 'active' ? undefined : '已停用'}
        actions={
          <>
            <Button
              variant="outline"
              disabled={!canWrite}
              onClick={() => {
                editForm.reset({ name: tenant?.name ?? '', plan: tenant?.plan ?? '' });
                setEditOpen(true);
              }}
            >
              编辑
            </Button>
            <Button
              variant="outline"
              disabled={!canWrite}
              onClick={() => {
                quotaForm.reset({
                  quota_storage_bytes: tenant?.quota_storage_bytes ?? 0,
                  quota_file_count: tenant?.quota_file_count ?? 0,
                });
                setQuotaOpen(true);
              }}
            >
              调整配额
            </Button>
            {tenant?.status === 'active' ? (
              <Button variant="destructive" disabled={!canWrite} onClick={() => setSuspendOpen(true)}>
                停用
              </Button>
            ) : (
              <Button variant="outline" disabled={!canWrite} onClick={() => void activateTenant()}>
                启用
              </Button>
            )}
          </>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>基本信息与用量报表</CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList cols={2}>
            <DescriptionItem label="ID">{tenant?.id}</DescriptionItem>
            <DescriptionItem label="状态">{tenant?.status}</DescriptionItem>
            <DescriptionItem label="套餐">{tenant?.plan}</DescriptionItem>
            <DescriptionItem label="创建时间">{formatDateTime(tenant?.created_at)}</DescriptionItem>
            <DescriptionItem label="空间数">{tenant?.space_count ?? 0}</DescriptionItem>
            <DescriptionItem label="用户数">{tenant?.user_count ?? 0}</DescriptionItem>
            <DescriptionItem label="存储用量">
              {formatQuota(tenant?.used_storage_bytes ?? 0, tenant?.quota_storage_bytes ?? 0)}
            </DescriptionItem>
            <DescriptionItem label="文件数">
              {tenant?.used_file_count ?? 0} / {tenant?.quota_file_count ? tenant.quota_file_count : '不限'}
            </DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>OIDC 域绑定</CardTitle>
          <CardAction>
            {binding ? (
              <span className="text-sm">该邮箱域登录的用户自动归属本租户</span>
            ) : (
              <span className="text-sm text-muted-foreground">
                未绑定（域内首个用户由租户管理员分配角色）
              </span>
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
              <Label htmlFor="binding-domain">邮箱域</Label>
              <Input id="binding-domain" maxLength={255} {...bindingForm.register('domain')} />
              <p className="text-xs text-muted-foreground">如 example.com</p>
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
                {binding?.has_secret ? '已设置；留空保留原值' : '必填（首次绑定）'}
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
              <Label htmlFor="binding-enabled">启用</Label>
            </div>
            <Button type="submit" disabled={busy || !canWrite}>
              {busy ? '保存中…' : '保存绑定'}
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* 配额调整：必填理由入审计 */}
      <ReasonModal
        open={quotaOpen}
        title="调整租户配额"
        okText="保存"
        confirmLoading={busy}
        onCancel={() => setQuotaOpen(false)}
        onOk={async (reason) => {
          const ok = await quotaForm.trigger();
          if (!ok) return;
          const { quota_storage_bytes, quota_file_count } = quotaForm.getValues();
          setBusy(true);
          try {
            await unwrap(
              api.PUT('/api/v1/admin/tenants/{tenant_id}/quota', {
                params: { path: { tenant_id: tenantId! } },
                body: { quota_storage_bytes, quota_file_count, reason },
              }),
            );
            toast.success('配额已调整');
            setQuotaOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="quota-storage">存储配额（字节）</Label>
            <Input
              id="quota-storage"
              type="number"
              min={0}
              {...quotaForm.register('quota_storage_bytes', { valueAsNumber: true })}
            />
            {quotaForm.formState.errors.quota_storage_bytes && (
              <p className="text-sm text-destructive">
                {quotaForm.formState.errors.quota_storage_bytes.message}
              </p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="quota-files">文件数配额</Label>
            <Input
              id="quota-files"
              type="number"
              min={0}
              {...quotaForm.register('quota_file_count', { valueAsNumber: true })}
            />
            {quotaForm.formState.errors.quota_file_count && (
              <p className="text-sm text-destructive">
                {quotaForm.formState.errors.quota_file_count.message}
              </p>
            )}
          </div>
        </div>
      </ReasonModal>

      {/* 编辑名称/套餐 */}
      <ReasonModal
        open={editOpen}
        title="编辑租户"
        okText="保存"
        requireReason={false}
        confirmLoading={busy}
        onCancel={() => setEditOpen(false)}
        onOk={async () => {
          const ok = await editForm.trigger();
          if (!ok) return;
          const values = editForm.getValues();
          setBusy(true);
          try {
            await unwrap(
              api.PATCH('/api/v1/admin/tenants/{tenant_id}', {
                params: { path: { tenant_id: tenantId! } },
                body: { name: values.name, plan: values.plan },
              }),
            );
            toast.success('已保存');
            setEditOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="edit-name">租户名称</Label>
            <Input id="edit-name" maxLength={255} {...editForm.register('name')} />
            {editForm.formState.errors.name && (
              <p className="text-sm text-destructive">{editForm.formState.errors.name.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="edit-plan">套餐</Label>
            <Input id="edit-plan" {...editForm.register('plan')} />
          </div>
        </div>
      </ReasonModal>

      {/* 停用（冻结）：危险操作 */}
      <ReasonModal
        open={suspendOpen}
        title={`停用租户「${tenant?.name ?? ''}」`}
        description="停用后该租户所有用户登录与 API 访问被冻结，数据保留。此为危险操作。"
        okText="确认停用"
        danger
        confirmLoading={busy}
        onCancel={() => setSuspendOpen(false)}
        onOk={async (reason) => {
          setBusy(true);
          try {
            await unwrap(
              api.POST('/api/v1/admin/tenants/{tenant_id}/suspend', {
                params: { path: { tenant_id: tenantId! } },
                body: { reason },
              }),
            );
            toast.success('租户已停用');
            setSuspendOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      />
    </div>
  );
}
