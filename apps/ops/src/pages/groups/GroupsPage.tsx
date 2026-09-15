import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, UserPlus, Users } from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';
import { ConfirmAction } from '@loomvec/ui/components/confirm-action';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { PageHeader } from '@loomvec/ui/components/page-header';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';

/**
 * 用户分组管理（运营端）：分组 CRUD + 成员增删（按用户名添加）。
 * 分组是公共空间可见性的授权单元（「可见性」页签勾选），与空间成员角色解耦。
 */

interface GroupItem {
  id: string;
  name: string;
  description: string | null;
  member_count: number;
  space_count: number;
}

interface GroupMember {
  user_id: string;
  username: string | null;
  display_name: string | null;
}

function CreateGroupDialog(props: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient();
  const { t } = useTranslation('groups');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');

  const create = useMutation({
    mutationFn: async () => {
      await unwrap(
        api.POST('/api/v1/ops/groups', {
          body: { name, description: description || null },
        }),
      );
    },
    onSuccess: () => {
      toast.success(t('groupCreated'));
      void queryClient.invalidateQueries({ queryKey: ['ops-groups'] });
      setName('');
      setDescription('');
      props.onOpenChange(false);
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <Dialog open={props.open} onOpenChange={props.onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('createTitle')}</DialogTitle>
          <DialogDescription>{t('createDesc')}</DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
          className="space-y-4"
        >
          <div className="space-y-2">
            <Label htmlFor="group-name">{t('nameLabel')}</Label>
            <Input
              id="group-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t('namePlaceholder')}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="group-desc">{t('field.description')}</Label>
            <Input
              id="group-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => props.onOpenChange(false)}>
              {t('action.cancel')}
            </Button>
            <Button type="submit" disabled={create.isPending || name.trim().length === 0}>
              {create.isPending ? t('creating') : t('action.create')}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function GroupMembersDialog(props: { group: GroupItem; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { t } = useTranslation('groups');
  const [username, setUsername] = useState('');

  const members = useQuery({
    queryKey: ['ops-group-members', props.group.id],
    queryFn: () =>
      unwrap<{ items: GroupMember[] }>(
        api.GET('/api/v1/ops/groups/{group_id}/members', {
          params: { path: { group_id: props.group.id } },
        }),
      ).then((r) => r.items),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['ops-group-members', props.group.id] });
    void queryClient.invalidateQueries({ queryKey: ['ops-groups'] });
  };

  const add = useMutation({
    mutationFn: async () => {
      await unwrap(
        api.POST('/api/v1/ops/groups/{group_id}/members', {
          params: { path: { group_id: props.group.id } },
          body: { username },
        }),
      );
    },
    onSuccess: () => {
      toast.success(t('memberAdded', { username }));
      setUsername('');
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: async (userId: string) => {
      const { error } = await api.DELETE('/api/v1/ops/groups/{group_id}/members/{user_id}', {
        params: { path: { group_id: props.group.id, user_id: userId } },
      });
      if (error) throw new Error(t('removeFailed'));
    },
    onSuccess: () => {
      toast.success(t('memberRemoved'));
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('membersTitle', { name: props.group.name })}</DialogTitle>
          <DialogDescription>{t('membersDesc')}</DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (username.trim()) add.mutate();
          }}
          className="flex gap-2"
        >
          <Input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={t('usernamePlaceholder')}
          />
          <Button type="submit" size="sm" disabled={add.isPending || username.trim().length === 0}>
            <UserPlus /> {t('add')}
          </Button>
        </form>
        <div className="max-h-72 divide-y overflow-y-auto rounded-lg border">
          {members.isLoading ? (
            <div className="grid place-items-center py-6">
              <Skeleton className="h-6 w-40" />
            </div>
          ) : (members.data?.length ?? 0) === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-muted-foreground">{t('noMembers')}</p>
          ) : (
            members.data?.map((m) => (
              <div key={m.user_id} className="flex items-center justify-between gap-2 px-4 py-2.5">
                <div className="min-w-0 text-sm">
                  <span className="font-medium">{m.display_name || m.username}</span>
                  {m.display_name && m.username && (
                    <span className="ml-1.5 text-muted-foreground">@{m.username}</span>
                  )}
                </div>
                <ConfirmAction
                  trigger={
                    <Button variant="outline" size="xs" className="text-destructive hover:text-destructive">
                      {t('remove')}
                    </Button>
                  }
                  title={t('removeTitle')}
                  description={t('removeDesc')}
                  confirmText={t('remove')}
                  danger
                  onConfirm={() => remove.mutate(m.user_id)}
                />
              </div>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function GroupsPage() {
  const queryClient = useQueryClient();
  const { t } = useTranslation('groups');
  const [createOpen, setCreateOpen] = useState(false);
  const [managing, setManaging] = useState<GroupItem | null>(null);

  const groups = useQuery({
    queryKey: ['ops-groups'],
    queryFn: () =>
      unwrap<{ items: GroupItem[] }>(api.GET('/api/v1/ops/groups')).then((r) => r.items),
  });

  const removeGroup = useMutation({
    mutationFn: async (groupId: string) => {
      const { error } = await api.DELETE('/api/v1/ops/groups/{group_id}', {
        params: { path: { group_id: groupId } },
      });
      if (error) throw new Error(t('deleteFailed'));
    },
    onSuccess: () => {
      toast.success(t('groupDeleted'));
      void queryClient.invalidateQueries({ queryKey: ['ops-groups'] });
      void queryClient.invalidateQueries({ queryKey: ['ops-visibility'] });
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('title')}
        description={t('description')}
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <Plus /> {t('createGroup')}
          </Button>
        }
      />

      {groups.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-32 rounded-xl" />
          ))}
        </div>
      ) : groups.isError ? (
        <p className="text-sm text-destructive">{(groups.error as Error).message}</p>
      ) : (groups.data?.length ?? 0) === 0 ? (
        <EmptyState
          title={t('emptyTitle')}
          description={t('emptyDesc')}
          className="mt-10"
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {groups.data?.map((g) => (
            <Card key={g.id}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Users className="size-4 text-muted-foreground" />
                  {g.name}
                </CardTitle>
                {g.description && <CardDescription className="line-clamp-2">{g.description}</CardDescription>}
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  {t('summary', { memberCount: g.member_count, spaceCount: g.space_count })}
                </p>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => setManaging(g)}>
                    {t('manageMembers')}
                  </Button>
                  <ConfirmAction
                    trigger={
                      <Button variant="outline" size="sm" className="text-destructive hover:text-destructive">
                        {t('action.delete')}
                      </Button>
                    }
                    title={t('deleteTitle', { name: g.name })}
                    description={t('deleteDesc')}
                    confirmText={t('action.delete')}
                    danger
                    onConfirm={() => removeGroup.mutate(g.id)}
                  />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <CreateGroupDialog open={createOpen} onOpenChange={setCreateOpen} />
      {managing && <GroupMembersDialog group={managing} onClose={() => setManaging(null)} />}
    </div>
  );
}
