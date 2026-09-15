import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
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
import { DataTable } from '@loomvec/ui/components/data-table';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ReasonModal } from '@/components/ReasonModal';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
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
import { formatBytes, formatDateTime } from '@/utils';
import type { SpaceDetail } from '@/types';

type SpaceMember = SpaceDetail['members'][number];

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const quotaSchema = z.object({
  quota_storage_bytes: z.number({ error: tt('spaces:quotaStorageRequired') }).min(0),
  quota_file_count: z.number({ error: tt('spaces:quotaFilesRequired') }).min(0),
});

const transferSchema = z.object({
  new_owner_id: z.string().min(1, tt('spaces:newOwnerRequired')),
});

/** 空间治理详情：成员列表 / 封禁解封 / owner 转移 / 内容清空（危险）/ 配额调整（docs/04 §5.4）。 */
export function SpaceDetailPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation('spaces');

  const [quotaOpen, setQuotaOpen] = useState(false);
  const [transferOpen, setTransferOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [banOpen, setBanOpen] = useState(false);
  const [unbanOpen, setUnbanOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const quotaForm = useForm<z.infer<typeof quotaSchema>>({
    resolver: zodResolver(quotaSchema),
    defaultValues: { quota_storage_bytes: 0, quota_file_count: 0 },
  });
  const transferForm = useForm<z.infer<typeof transferSchema>>({
    resolver: zodResolver(transferSchema),
    defaultValues: { new_owner_id: '' },
  });

  const spaceKey = ['admin-space', spaceId];
  const { data: space, isError, error } = useQuery({
    queryKey: spaceKey,
    enabled: !!spaceId,
    queryFn: () =>
      unwrap<SpaceDetail>(
        api.GET('/api/v1/admin/spaces/{space_id}', {
          params: { path: { space_id: spaceId! } },
        }),
      ),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: spaceKey });
    void queryClient.invalidateQueries({ queryKey: ['admin-spaces'] });
    void queryClient.invalidateQueries({ queryKey: ['admin-system-status'] });
  };

  const post = async (
    url: '/api/v1/admin/spaces/{space_id}/ban' | '/api/v1/admin/spaces/{space_id}/unban',
    reason: string,
    okText: string,
    close: () => void,
  ) => {
    setBusy(true);
    try {
      await unwrap(api.POST(url, { params: { path: { space_id: spaceId! } }, body: { reason } }));
      toast.success(okText);
      close();
      invalidate();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
    } finally {
      setBusy(false);
    }
  };

  const memberColumns: ColumnDef<SpaceMember, unknown>[] = [
    { accessorKey: 'user_id', header: t('col.userId') },
    {
      accessorKey: 'role',
      header: t('col.role'),
      cell: ({ row }) =>
        row.original.role === 'owner' ? (
          <StatusBadge tone="amber">owner</StatusBadge>
        ) : row.original.role === 'editor' ? (
          <StatusBadge tone="blue">editor</StatusBadge>
        ) : (
          <Badge variant="outline">viewer</Badge>
        ),
    },
    {
      accessorKey: 'invited_by',
      header: t('col.invitedBy'),
      cell: ({ row }) => row.original.invited_by ?? '-',
    },
  ];

  return (
    <div className="space-y-4">
      <div>
        <Button
          variant="ghost"
          size="sm"
          className="-ml-2 text-muted-foreground"
          onClick={() => navigate('/spaces')}
        >
          <ArrowLeft /> {t('backToList')}
        </Button>
      </div>
      <PageHeader
        title={space?.name ?? t('detailTitle')}
        description={space?.slug}
        actions={
          <>
            {space?.banned ? (
              <Button variant="outline" disabled={!canWrite} onClick={() => setUnbanOpen(true)}>
                {t('unban')}
              </Button>
            ) : (
              <Button variant="destructive" disabled={!canWrite} onClick={() => setBanOpen(true)}>
                {t('ban')}
              </Button>
            )}
            <Button
              variant="outline"
              disabled={!canWrite}
              onClick={() => {
                transferForm.reset({ new_owner_id: '' });
                setTransferOpen(true);
              }}
            >
              {t('transferOwner')}
            </Button>
            <Button
              variant="outline"
              disabled={!canWrite}
              onClick={() => {
                quotaForm.reset({ quota_storage_bytes: 0, quota_file_count: 0 });
                setQuotaOpen(true);
              }}
            >
              {t('adjustQuota')}
            </Button>
            <Button variant="destructive" disabled={!canWrite} onClick={() => setClearOpen(true)}>
              {t('clearContent')}
            </Button>
          </>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>{t('basicInfoTitle')}</CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList cols={2}>
            <DescriptionItem label="ID">{space?.id}</DescriptionItem>
            <DescriptionItem label={t('col.type')}>
              {space?.space_type === 'personal' ? t('type.personalSpace') : t('type.sharedSpace')}
            </DescriptionItem>
            <DescriptionItem label={t('col.tenant')}>{space?.tenant_id ?? t('platformLevel')}</DescriptionItem>
            <DescriptionItem label="Owner">{space?.owner_name ?? '-'}</DescriptionItem>
            <DescriptionItem label={t('col.memberCount')}>{space?.member_count ?? 0}</DescriptionItem>
            <DescriptionItem label={t('col.assetCount')}>{space?.asset_count ?? 0}</DescriptionItem>
            <DescriptionItem label={t('col.storageUsage')}>{formatBytes(space?.storage_bytes ?? 0)}</DescriptionItem>
            <DescriptionItem label={t('col.fileCount')}>{space?.file_count ?? 0}</DescriptionItem>
            <DescriptionItem label={t('col.reviewRequired')}>
              {space?.review_required ? (
                <StatusBadge tone="amber">{t('toggle.on')}</StatusBadge>
              ) : (
                <Badge variant="outline">{t('toggle.off')}</Badge>
              )}
            </DescriptionItem>
            <DescriptionItem label={t('field.status')}>
              {space?.banned ? (
                <StatusBadge tone="red">{t('status.banned')}</StatusBadge>
              ) : (
                <StatusBadge tone="green">{t('status.normal')}</StatusBadge>
              )}
            </DescriptionItem>
            <DescriptionItem label={t('col.embeddingModel')}>{space?.embedding_model ?? '-'}</DescriptionItem>
            <DescriptionItem label={t('col.chunkPreset')}>{space?.chunk_preset ?? '-'}</DescriptionItem>
            <DescriptionItem label={t('field.createdAt')}>{formatDateTime(space?.created_at)}</DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('membersTitle')}</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={memberColumns}
            data={space?.members}
            error={isError ? error : undefined}
            getRowId={(m) => m.user_id}
          />
        </CardContent>
      </Card>

      {/* 封禁（危险操作） */}
      <ReasonModal
        open={banOpen}
        title={t('banTitle', { name: space?.name ?? '' })}
        description={t('banDescription')}
        okText={t('confirmBan')}
        danger
        confirmLoading={busy}
        onCancel={() => setBanOpen(false)}
        onOk={(reason) => post('/api/v1/admin/spaces/{space_id}/ban', reason, t('bannedToast'), () => setBanOpen(false))}
      />

      {/* 解封 */}
      <ReasonModal
        open={unbanOpen}
        title={t('unbanTitle')}
        okText={t('confirmUnban')}
        confirmLoading={busy}
        onCancel={() => setUnbanOpen(false)}
        onOk={(reason) => post('/api/v1/admin/spaces/{space_id}/unban', reason, t('unbannedToast'), () => setUnbanOpen(false))}
      />

      {/* owner 转移：新 owner 必须是空间成员 */}
      <ReasonModal
        open={transferOpen}
        title={t('transferTitle')}
        description={t('transferDescription')}
        okText={t('confirmTransfer')}
        confirmLoading={busy}
        onCancel={() => setTransferOpen(false)}
        onOk={async (reason) => {
          const ok = await transferForm.trigger();
          if (!ok) return;
          const { new_owner_id } = transferForm.getValues();
          setBusy(true);
          try {
            await unwrap(
              api.POST('/api/v1/admin/spaces/{space_id}/transfer-owner', {
                params: { path: { space_id: spaceId! } },
                body: { new_owner_id, reason },
              }),
            );
            toast.success(t('transferSuccess'));
            setTransferOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="new_owner_id">{t('newOwnerLabel')}</Label>
          <Select
            value={transferForm.watch('new_owner_id') || undefined}
            onValueChange={(v) => transferForm.setValue('new_owner_id', v, { shouldValidate: true })}
          >
            <SelectTrigger className="w-full">
              <SelectValue placeholder={t('memberPlaceholder')} />
            </SelectTrigger>
            <SelectContent>
              {(space?.members ?? []).map((m) => (
                <SelectItem key={m.user_id} value={m.user_id}>
                  {`${m.user_id}（${m.role}）`}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {transferForm.formState.errors.new_owner_id && (
            <p className="text-sm text-destructive">
              {transferForm.formState.errors.new_owner_id.message}
            </p>
          )}
        </div>
      </ReasonModal>

      {/* 内容清空（危险操作） */}
      <ReasonModal
        open={clearOpen}
        title={t('clearTitle', { name: space?.name ?? '' })}
        description={t('clearDescription', { count: space?.asset_count ?? 0 })}
        okText={t('confirmClear')}
        danger
        confirmLoading={busy}
        onCancel={() => setClearOpen(false)}
        onOk={async (reason) => {
          setBusy(true);
          try {
            await unwrap(
              api.POST('/api/v1/admin/spaces/{space_id}/clear', {
                params: { path: { space_id: spaceId! } },
                body: { reason },
              }),
            );
            toast.success(t('clearAccepted'));
            setClearOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : t('feedback.operationFailed'));
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 配额调整 */}
      <ReasonModal
        open={quotaOpen}
        title={t('quotaTitle')}
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
              api.PUT('/api/v1/admin/spaces/{space_id}/quota', {
                params: { path: { space_id: spaceId! } },
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
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="quota_storage_bytes">{t('quotaStorageLabel')}</Label>
            <Input
              id="quota_storage_bytes"
              type="number"
              min={0}
              className="w-full"
              {...quotaForm.register('quota_storage_bytes', { valueAsNumber: true })}
            />
            {quotaForm.formState.errors.quota_storage_bytes && (
              <p className="text-sm text-destructive">
                {quotaForm.formState.errors.quota_storage_bytes.message}
              </p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="quota_file_count">{t('quotaFilesLabel')}</Label>
            <Input
              id="quota_file_count"
              type="number"
              min={0}
              className="w-full"
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
    </div>
  );
}
