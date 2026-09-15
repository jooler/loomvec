import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
import { t as tt } from '@/i18n';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import type { TenantRow } from '@/types';
import { formatDateTime, formatQuota } from '@/utils';
import { OidcBindingCard } from './OidcBindingCard';

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const quotaSchema = z.object({
  quota_storage_bytes: z.number({ error: tt('tenants:form.quotaStorageRequired') }).min(0),
  quota_file_count: z.number({ error: tt('tenants:form.quotaFilesRequired') }).min(0),
});

const editSchema = z.object({
  name: z.string().min(1, tt('tenants:form.nameRequired')).max(255),
  plan: z.string(),
});

type QuotaValues = z.infer<typeof quotaSchema>;
type EditValues = z.infer<typeof editSchema>;

/** 租户详情：用量报表 / 配额调整 / 编辑 / 停用启用 / OIDC 域绑定（docs/04 §5.2）。 */
export function TenantDetailPage() {
  const { tenantId } = useParams<{ tenantId: string }>();
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('tenants');

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

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: tenantKey });
    void queryClient.invalidateQueries({ queryKey: ['admin-tenants'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
  };

  const activateTenant = async () => {
    try {
      await unwrap(
        api.POST('/api/v1/admin/tenants/{tenant_id}/activate', {
          params: { path: { tenant_id: tenantId! } },
        }),
      );
      toast.success(t('activated'));
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    }
  };

  return (
    <div className="space-y-4">
      <Button variant="ghost" size="sm" className="-ml-2" onClick={() => navigate('/tenants')}>
        <ArrowLeft /> {t('action.back')}
      </Button>

      <PageHeader
        title={tenant?.name ?? t('detailTitle')}
        description={tenant?.status === 'active' ? undefined : t('suspendedStatus')}
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
              {t('action.edit')}
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
              {t('adjustQuota')}
            </Button>
            {tenant?.status === 'active' ? (
              <Button variant="destructive" disabled={!canWrite} onClick={() => setSuspendOpen(true)}>
                {t('suspend')}
              </Button>
            ) : (
              <Button variant="outline" disabled={!canWrite} onClick={() => void activateTenant()}>
                {t('action.enable')}
              </Button>
            )}
          </>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>{t('basicInfoTitle')}</CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList cols={2}>
            <DescriptionItem label="ID">{tenant?.id}</DescriptionItem>
            <DescriptionItem label={t('field.status')}>{tenant?.status}</DescriptionItem>
            <DescriptionItem label={t('col.plan')}>{tenant?.plan}</DescriptionItem>
            <DescriptionItem label={t('field.createdAt')}>{formatDateTime(tenant?.created_at)}</DescriptionItem>
            <DescriptionItem label={t('col.spaceCount')}>{tenant?.space_count ?? 0}</DescriptionItem>
            <DescriptionItem label={t('col.userCount')}>{tenant?.user_count ?? 0}</DescriptionItem>
            <DescriptionItem label={t('col.storageUsage')}>
              {formatQuota(tenant?.used_storage_bytes ?? 0, tenant?.quota_storage_bytes ?? 0)}
            </DescriptionItem>
            <DescriptionItem label={t('col.fileCount')}>
              {tenant?.used_file_count ?? 0} / {tenant?.quota_file_count ? tenant.quota_file_count : t('unlimited')}
            </DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      {tenantId && <OidcBindingCard tenantId={tenantId} canWrite={canWrite} />}

      {/* 配额调整：必填理由入审计 */}
      <ReasonModal
        open={quotaOpen}
        title={t('adjustQuotaTitle')}
        okText={t('action.save')}
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
            toast.success(t('quotaAdjusted'));
            setQuotaOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="quota-storage">{t('form.quotaStoragePlainLabel')}</Label>
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
            <Label htmlFor="quota-files">{t('form.quotaFilesPlainLabel')}</Label>
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
        title={t('editTenant')}
        okText={t('action.save')}
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
            toast.success(t('feedback.saved'));
            setEditOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="edit-name">{t('form.nameLabel')}</Label>
            <Input id="edit-name" maxLength={255} {...editForm.register('name')} />
            {editForm.formState.errors.name && (
              <p className="text-sm text-destructive">{editForm.formState.errors.name.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="edit-plan">{t('form.planLabel')}</Label>
            <Input id="edit-plan" {...editForm.register('plan')} />
          </div>
        </div>
      </ReasonModal>

      {/* 停用（冻结）：危险操作 */}
      <ReasonModal
        open={suspendOpen}
        title={t('suspendTitle', { name: tenant?.name ?? '' })}
        description={t('suspendDescriptionShort')}
        okText={t('confirmSuspend')}
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
            toast.success(t('suspendToastShort'));
            setSuspendOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      />
    </div>
  );
}
