import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { ArrowLeft } from 'lucide-react';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate, useParams } from 'react-router';
import { toast } from 'sonner';
import { z } from 'zod';
import { api, unwrap } from '@/api';
import { usePerm } from '@/auth';
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

const quotaSchema = z.object({
  quota_storage_bytes: z.number({ error: '请输入存储配额（字节）' }).min(0),
  quota_file_count: z.number({ error: '请输入文件数配额' }).min(0),
});

const transferSchema = z.object({
  new_owner_id: z.string().min(1, '请输入成员 user_id'),
});

/** 空间治理详情：成员列表 / 封禁解封 / owner 转移 / 内容清空（危险）/ 配额调整（docs/04 §5.4）。 */
export function SpaceDetailPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const { canWrite } = usePerm();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

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
  const { data: space } = useQuery({
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
      toast.error(e instanceof Error ? e.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const memberColumns: ColumnDef<SpaceMember, unknown>[] = [
    { accessorKey: 'user_id', header: '用户 ID' },
    {
      accessorKey: 'role',
      header: '角色',
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
      header: '邀请人',
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
          <ArrowLeft /> 返回空间列表
        </Button>
      </div>
      <PageHeader
        title={space?.name ?? '空间详情'}
        description={space?.slug}
        actions={
          <>
            {space?.banned ? (
              <Button variant="outline" disabled={!canWrite} onClick={() => setUnbanOpen(true)}>
                解封
              </Button>
            ) : (
              <Button variant="destructive" disabled={!canWrite} onClick={() => setBanOpen(true)}>
                封禁
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
              Owner 转移
            </Button>
            <Button
              variant="outline"
              disabled={!canWrite}
              onClick={() => {
                quotaForm.reset({ quota_storage_bytes: 0, quota_file_count: 0 });
                setQuotaOpen(true);
              }}
            >
              调整配额
            </Button>
            <Button variant="destructive" disabled={!canWrite} onClick={() => setClearOpen(true)}>
              内容清空
            </Button>
          </>
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>基本信息</CardTitle>
        </CardHeader>
        <CardContent>
          <DescriptionList cols={2}>
            <DescriptionItem label="ID">{space?.id}</DescriptionItem>
            <DescriptionItem label="类型">
              {space?.space_type === 'personal' ? '个人空间' : '共享空间'}
            </DescriptionItem>
            <DescriptionItem label="所属租户">{space?.tenant_id ?? '—（平台级）'}</DescriptionItem>
            <DescriptionItem label="Owner">{space?.owner_name ?? '-'}</DescriptionItem>
            <DescriptionItem label="成员数">{space?.member_count ?? 0}</DescriptionItem>
            <DescriptionItem label="资产数">{space?.asset_count ?? 0}</DescriptionItem>
            <DescriptionItem label="存储用量">{formatBytes(space?.storage_bytes ?? 0)}</DescriptionItem>
            <DescriptionItem label="文件数">{space?.file_count ?? 0}</DescriptionItem>
            <DescriptionItem label="先审后见">
              {space?.review_required ? (
                <StatusBadge tone="amber">开启</StatusBadge>
              ) : (
                <Badge variant="outline">关闭</Badge>
              )}
            </DescriptionItem>
            <DescriptionItem label="状态">
              {space?.banned ? (
                <StatusBadge tone="red">已封禁</StatusBadge>
              ) : (
                <StatusBadge tone="green">正常</StatusBadge>
              )}
            </DescriptionItem>
            <DescriptionItem label="嵌入模型">{space?.embedding_model ?? '-'}</DescriptionItem>
            <DescriptionItem label="分块预设">{space?.chunk_preset ?? '-'}</DescriptionItem>
            <DescriptionItem label="创建时间">{formatDateTime(space?.created_at)}</DescriptionItem>
          </DescriptionList>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>成员列表</CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable columns={memberColumns} data={space?.members} getRowId={(m) => m.user_id} />
        </CardContent>
      </Card>

      {/* 封禁（危险操作） */}
      <ReasonModal
        open={banOpen}
        title={`封禁空间「${space?.name ?? ''}」`}
        description="封禁后该空间所有成员访问被拒，数据保留。此为危险操作。"
        okText="确认封禁"
        danger
        confirmLoading={busy}
        onCancel={() => setBanOpen(false)}
        onOk={(reason) => post('/api/v1/admin/spaces/{space_id}/ban', reason, '已封禁', () => setBanOpen(false))}
      />

      {/* 解封 */}
      <ReasonModal
        open={unbanOpen}
        title="解封空间"
        okText="确认解封"
        confirmLoading={busy}
        onCancel={() => setUnbanOpen(false)}
        onOk={(reason) => post('/api/v1/admin/spaces/{space_id}/unban', reason, '已解封', () => setUnbanOpen(false))}
      />

      {/* owner 转移：新 owner 必须是空间成员 */}
      <ReasonModal
        open={transferOpen}
        title="Owner 转移"
        description="旧 owner 将变为 editor；新 owner 必须已是空间成员。"
        okText="确认转移"
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
            toast.success('Owner 已转移');
            setTransferOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="new_owner_id">新 Owner（成员 user_id）</Label>
          <Select
            value={transferForm.watch('new_owner_id') || undefined}
            onValueChange={(v) => transferForm.setValue('new_owner_id', v, { shouldValidate: true })}
          >
            <SelectTrigger className="w-full">
              <SelectValue placeholder="选择空间成员" />
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
        title={`清空空间「${space?.name ?? ''}」全部内容`}
        description={`软删全部 ${space?.asset_count ?? 0} 个资产并连带清理向量，操作不可逆。此为危险操作。`}
        okText="确认清空"
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
            toast.success('清空任务已受理');
            setClearOpen(false);
            invalidate();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : '操作失败');
          } finally {
            setBusy(false);
          }
        }}
      />

      {/* 配额调整 */}
      <ReasonModal
        open={quotaOpen}
        title="调整空间配额"
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
              api.PUT('/api/v1/admin/spaces/{space_id}/quota', {
                params: { path: { space_id: spaceId! } },
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
        <div className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="quota_storage_bytes">存储配额（字节）</Label>
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
            <Label htmlFor="quota_file_count">文件数配额</Label>
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
