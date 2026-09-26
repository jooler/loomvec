import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { z } from 'zod';
import type { ColumnDef } from '@tanstack/react-table';
import { Info, User, UserPlus } from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { t } from '@/i18n';
import { useSpace } from '@/hooks';
import { extractApiError, ROLE_TONE } from '@/utils';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { DataTable } from '@loomvec/ui/components/data-table';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
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

/** 角色下拉选项（文案走 i18n member.roleOption.*）。 */
const ROLE_OPTIONS = ['viewer', 'editor', 'owner'] as const;

function RoleBadge({ role }: { role: string }) {
  const { t } = useTranslation('spaces');
  return (
    <StatusBadge tone={ROLE_TONE[role]}>{t(`role.${role}`, { defaultValue: role })}</StatusBadge>
  );
}

// 模块级文案（zod 校验消息）：复用 auth 命名空间的同名文案
const inviteSchema = z.object({
  username: z.string().min(1, t('auth:usernameRequired')),
  role: z.enum(['viewer', 'editor']),
});

type InviteValues = z.infer<typeof inviteSchema>;

export function SpaceMembersPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const { t } = useTranslation('spaces');
  const queryClient = useQueryClient();
  const form = useForm<InviteValues>({
    resolver: zodResolver(inviteSchema),
    defaultValues: { username: '', role: 'viewer' },
  });

  const space = useSpace(spaceId);

  const members = useQuery({
    queryKey: ['space-members', spaceId],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/members', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, t('member.loadMembersFailed')));
      return data.items;
    },
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['space-members', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['space', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['spaces'] });
  };

  const invite = useMutation({
    mutationFn: async (values: InviteValues) => {
      const { error } = await api.POST('/api/v1/spaces/{space_id}/members', {
        params: { path: { space_id: spaceId! } },
        body: { username: values.username, role: values.role },
      });
      if (error) throw new Error(extractApiError(error, t('member.inviteFailed')));
    },
    onSuccess: () => {
      toast.success(t('member.added'));
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
      if (error) throw new Error(extractApiError(error, t('member.changeRoleFailed')));
    },
    onSuccess: () => {
      toast.success(t('member.roleUpdated'));
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const removeMember = useMutation({
    mutationFn: async (userId: string) => {
      const { error } = await api.DELETE('/api/v1/spaces/{space_id}/members/{user_id}', {
        params: { path: { space_id: spaceId!, user_id: userId } },
      });
      if (error) throw new Error(extractApiError(error, t('member.removeFailed')));
    },
    onSuccess: () => {
      toast.success(t('member.removed'));
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const isOwner = space.data?.my_role === 'owner';

  type MemberRow = NonNullable<typeof members.data>[number];

  const columns: ColumnDef<MemberRow, unknown>[] = [
    {
      id: 'member',
      header: t('member.column'),
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
      header: t('member.roleColumn'),
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
                <SelectItem key={o} value={o}>
                  {t(`member.roleOption.${o}`, { defaultValue: o })}
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
      header: t('field.actions'),
      cell: ({ row }) =>
        row.original.role === 'owner' ? (
          <span className="text-sm text-muted-foreground">{t('member.ownerImmovable')}</span>
        ) : isOwner ? (
          <ConfirmAction
            trigger={
              <Button
                variant="destructive"
                size="sm"
                disabled={removeMember.isPending}
              >
                {t('member.remove')}
              </Button>
            }
            title={t('member.removeTitle', {
              name: row.original.username ?? row.original.user_id,
            })}
            description={t('member.removeDesc')}
            onConfirm={() => removeMember.mutate(row.original.user_id)}
          />
        ) : (
          <span className="text-sm text-muted-foreground">-</span>
        ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title={t('member.title')} />

      <Card>
        <CardContent className="space-y-4">
          {!space.isLoading && !isOwner && (
            <Alert>
              <Info />
              <AlertDescription>{t('member.readOnlyHint')}</AlertDescription>
            </Alert>
          )}

          {isOwner && (
            <Card className="py-4">
              <CardHeader className="px-4">
                <CardTitle className="flex items-center gap-2 text-base">
                  <UserPlus className="size-4" /> {t('member.inviteTitle')}
                </CardTitle>
              </CardHeader>
              <CardContent className="px-4">
                <form
                  onSubmit={form.handleSubmit((v) => invite.mutate(v))}
                  className="flex flex-wrap items-start gap-3"
                >
                  <div className="space-y-1">
                    <Input
                      placeholder={t('member.usernamePlaceholder')}
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
                      {ROLE_OPTIONS.filter((o) => o !== 'owner').map((o) => (
                        <SelectItem key={o} value={o}>
                          {t(`member.roleOption.${o}`, { defaultValue: o })}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button type="submit" disabled={invite.isPending}>
                    {t('member.invite')}
                  </Button>
                </form>
              </CardContent>
            </Card>
          )}

          {members.isError ? (
            <p className="text-sm text-destructive">{(members.error as Error).message}</p>
          ) : (
            <DataTable
              columns={columns}
              data={members.data}
              loading={members.isLoading}
              getRowId={(row) => row.user_id}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
