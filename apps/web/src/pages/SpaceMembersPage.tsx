import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import type { ColumnDef } from '@tanstack/react-table';
import { Info, User, UserPlus } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { extractApiError, ROLE_META } from '@/utils';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { StatusBadge, type BadgeTone } from '@loomvec/ui/components/status-badge';
import { Alert, AlertDescription } from '@loomvec/ui/components/ui/alert';
import { Avatar, AvatarFallback } from '@loomvec/ui/components/ui/avatar';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';

/**
 * P2-WEB-01 成员管理：成员列表 + 邀请（按用户名）+ 角色变更 + 移除（仅 owner 可操作）。
 */

const ROLE_OPTIONS = [
  { value: 'viewer', label: '查看者（viewer）' },
  { value: 'editor', label: '编辑者（editor）' },
  { value: 'owner', label: '所有者（owner）' },
];

/** ROLE_META 的 antd 色 → StatusBadge tone。 */
const ROLE_COLOR_TONE: Record<string, BadgeTone> = {
  gold: 'amber',
  blue: 'blue',
  default: 'gray',
};

function RoleBadge({ role }: { role: string }) {
  const meta = ROLE_META[role];
  return (
    <StatusBadge tone={meta ? ROLE_COLOR_TONE[meta.color] : undefined}>
      {meta?.text ?? role}
    </StatusBadge>
  );
}

const inviteSchema = z.object({
  username: z.string().min(1, '请输入用户名'),
  role: z.enum(['viewer', 'editor']),
});

type InviteValues = z.infer<typeof inviteSchema>;

export function SpaceMembersPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const queryClient = useQueryClient();
  const form = useForm<InviteValues>({
    resolver: zodResolver(inviteSchema),
    defaultValues: { username: '', role: 'viewer' },
  });

  const space = useQuery({
    queryKey: ['space', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载空间失败'));
      return data;
    },
  });

  const members = useQuery({
    queryKey: ['space-members', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/members', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载成员失败'));
      return data.items;
    },
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['space-members', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['spaces'] });
  };

  const invite = useMutation({
    mutationFn: async (values: InviteValues) => {
      const { error } = await api.POST('/api/v1/spaces/{space_id}/members', {
        params: { path: { space_id: spaceId! } },
        body: { username: values.username, role: values.role },
      });
      if (error) throw new Error(extractApiError(error, '邀请失败'));
    },
    onSuccess: () => {
      toast.success('成员已加入');
      form.reset();
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const changeRole = useMutation({
    mutationFn: async (vars: { userId: string; role: string }) => {
      const { error } = await api.PATCH('/api/v1/spaces/{space_id}/members/{user_id}', {
        params: { path: { space_id: spaceId!, user_id: vars.userId } },
        body: { role: vars.role as 'owner' | 'editor' | 'viewer' },
      });
      if (error) throw new Error(extractApiError(error, '角色变更失败'));
    },
    onSuccess: () => {
      toast.success('角色已更新');
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const removeMember = useMutation({
    mutationFn: async (userId: string) => {
      const { error } = await api.DELETE('/api/v1/spaces/{space_id}/members/{user_id}', {
        params: { path: { space_id: spaceId!, user_id: userId } },
      });
      if (error) throw new Error(extractApiError(error, '移除失败'));
    },
    onSuccess: () => {
      toast.success('成员已移除');
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const isOwner = space.data?.my_role === 'owner';

  type MemberRow = NonNullable<typeof members.data>[number];

  const columns: ColumnDef<MemberRow, unknown>[] = [
    {
      id: 'member',
      header: '成员',
      cell: ({ row }) => (
        <div className="flex items-center gap-2">
          <Avatar size="sm">
            <AvatarFallback>
              <User className="size-3" />
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0">
            <div className="text-sm">
              {row.original.display_name || row.original.username || row.original.user_id}
            </div>
            {row.original.username && (
              <p className="text-xs text-muted-foreground">@{row.original.username}</p>
            )}
          </div>
        </div>
      ),
    },
    {
      accessorKey: 'role',
      header: '角色',
      cell: ({ row }) =>
        isOwner ? (
          <Select
            value={row.original.role}
            onValueChange={(next) =>
              changeRole.mutate({ userId: row.original.user_id, role: next })
            }
            disabled={changeRole.isPending}
          >
            <SelectTrigger className="w-[200px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ROLE_OPTIONS.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <RoleBadge role={row.original.role} />
        ),
    },
    {
      id: 'actions',
      header: '操作',
      cell: ({ row }) =>
        row.original.role === 'owner' ? (
          <span className="text-sm text-muted-foreground">owner 不可被移除</span>
        ) : isOwner ? (
          <ConfirmAction
            trigger={
              <Button
                variant="destructive"
                size="sm"
                disabled={removeMember.isPending}
              >
                移除
              </Button>
            }
            title={`移除成员 ${row.original.username ?? row.original.user_id}？`}
            description="移除后其将失去本空间的访问权限。"
            onConfirm={() => removeMember.mutate(row.original.user_id)}
          />
        ) : (
          <span className="text-sm text-muted-foreground">-</span>
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title="成员管理" />

      <Card>
        <CardContent className="space-y-4">
          {!space.isLoading && !isOwner && (
            <Alert>
              <Info />
              <AlertDescription>
                仅空间所有者可邀请成员与变更角色，以下为只读视图。
              </AlertDescription>
            </Alert>
          )}

          {isOwner && (
            <Card className="py-4">
              <CardHeader className="px-4">
                <CardTitle className="flex items-center gap-2 text-base">
                  <UserPlus className="size-4" /> 邀请成员
                </CardTitle>
              </CardHeader>
              <CardContent className="px-4">
                <form
                  onSubmit={form.handleSubmit((v) => invite.mutate(v))}
                  className="flex flex-wrap items-start gap-3"
                >
                  <div className="space-y-1">
                    <Input
                      placeholder="用户名（如 bob）"
                      className="w-[200px]"
                      {...form.register('username')}
                    />
                    {form.formState.errors.username && (
                      <p className="text-sm text-destructive">
                        {form.formState.errors.username.message}
                      </p>
                    )}
                  </div>
                  <Select
                    value={form.watch('role')}
                    onValueChange={(v) => form.setValue('role', v as InviteValues['role'])}
                  >
                    <SelectTrigger className="w-[180px]">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {ROLE_OPTIONS.filter((o) => o.value !== 'owner').map((o) => (
                        <SelectItem key={o.value} value={o.value}>
                          {o.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button type="submit" disabled={invite.isPending}>
                    邀请
                  </Button>
                </form>
              </CardContent>
            </Card>
          )}

          <DataTable
            columns={columns}
            data={members.data}
            loading={members.isLoading}
            getRowId={(row) => row.user_id}
          />
        </CardContent>
      </Card>
    </div>
  );
}
