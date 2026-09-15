import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, UserPlus, Users } from 'lucide-react';
import { toast } from 'sonner';
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
      toast.success('分组已创建');
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
          <DialogTitle>创建用户分组</DialogTitle>
          <DialogDescription>
            分组用于公共空间可见性：在某空间「可见性」页签勾选分组，该分组内用户即可链接该空间。
          </DialogDescription>
        </DialogHeader>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
          className="space-y-4"
        >
          <div className="space-y-2">
            <Label htmlFor="group-name">分组名称</Label>
            <Input
              id="group-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如：产品研发部"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="group-desc">描述</Label>
            <Input
              id="group-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => props.onOpenChange(false)}>
              取消
            </Button>
            <Button type="submit" disabled={create.isPending || name.trim().length === 0}>
              {create.isPending ? '创建中…' : '创建'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function GroupMembersDialog(props: { group: GroupItem; onClose: () => void }) {
  const queryClient = useQueryClient();
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
      toast.success(`已加入 ${username}`);
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
      if (error) throw new Error('移除失败');
    },
    onSuccess: () => {
      toast.success('已移出分组');
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <Dialog open onOpenChange={(open) => !open && props.onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>分组成员：{props.group.name}</DialogTitle>
          <DialogDescription>按用户名添加成员；成员可属多个分组。</DialogDescription>
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
            placeholder="用户名"
          />
          <Button type="submit" size="sm" disabled={add.isPending || username.trim().length === 0}>
            <UserPlus /> 添加
          </Button>
        </form>
        <div className="max-h-72 divide-y overflow-y-auto rounded-lg border">
          {members.isLoading ? (
            <div className="grid place-items-center py-6">
              <Skeleton className="h-6 w-40" />
            </div>
          ) : (members.data?.length ?? 0) === 0 ? (
            <p className="px-4 py-6 text-center text-sm text-muted-foreground">还没有成员</p>
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
                      移除
                    </Button>
                  }
                  title="移出分组？"
                  description="移出后该用户对仅授权此分组的公共空间立即失去可见性与检索资格。"
                  confirmText="移除"
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
      if (error) throw new Error('删除分组失败');
    },
    onSuccess: () => {
      toast.success('分组已删除（其可见性勾选一并清除）');
      void queryClient.invalidateQueries({ queryKey: ['ops-groups'] });
      void queryClient.invalidateQueries({ queryKey: ['ops-visibility'] });
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title="用户分组"
        description="分组是公共空间可见性的授权单元。在空间的「可见性」页签勾选分组即对该分组公开。"
        actions={
          <Button onClick={() => setCreateOpen(true)}>
            <Plus /> 创建分组
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
          title="还没有用户分组"
          description="创建分组并添加成员，再到公共空间「可见性」页签勾选。"
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
                  {g.member_count} 名成员 · 对 {g.space_count} 个空间可见
                </p>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => setManaging(g)}>
                    管理成员
                  </Button>
                  <ConfirmAction
                    trigger={
                      <Button variant="outline" size="sm" className="text-destructive hover:text-destructive">
                        删除
                      </Button>
                    }
                    title={`删除分组「${g.name}」？`}
                    description="删除后该分组在各空间的可见性勾选一并清除。"
                    confirmText="删除"
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
