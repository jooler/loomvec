import { Bell, Check, User } from 'lucide-react';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useNotifications, useMySpaces } from '@/hooks';
import { extractApiError, formatBytes, ROLE_META } from '@/utils';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { Avatar, AvatarFallback } from '@loomvec/ui/components/ui/avatar';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

/**
 * P2-WEB-07 个人中心：当前用户信息 + 各空间配额进度 + 通知中心（未读徽标/单条已读/全部已读）。
 */

interface MeOut {
  user_id: string;
  username: string;
  tenant_id: string | null;
  tenant_name?: string | null;
  roles: string[];
  unread_notifications?: number;
}

function percentOf(used: number, quota: number): number {
  if (quota <= 0) return 0;
  return Math.min(100, Math.round((used / quota) * 100));
}

/** 用量进度条（antd Progress size=small：条 + 百分比文字）。 */
function QuotaBar({ percent }: { percent: number }) {
  return (
    <div className="flex items-center gap-2">
      <Progress className="flex-1" value={percent} />
      <span className="w-10 shrink-0 text-right text-xs text-muted-foreground">{percent}%</span>
    </div>
  );
}

export function ProfilePage() {
  const queryClient = useQueryClient();

  const me = useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/me');
      if (error) throw new Error(extractApiError(error, '加载用户信息失败'));
      return data as unknown as MeOut;
    },
  });

  const spaces = useMySpaces();
  const notifications = useNotifications();

  const usages = useQueries({
    queries: (spaces.data ?? []).map((s) => ({
      queryKey: ['space-usage', s.id],
      queryFn: async () => {
        const { data, error } = await api.GET('/api/v1/spaces/{space_id}/usage', {
          params: { path: { space_id: s.id } },
        });
        if (error) throw new Error(extractApiError(error, '加载用量失败'));
        return data;
      },
      staleTime: 30_000,
    })),
  });

  const invalidateNotifications = () => {
    void queryClient.invalidateQueries({ queryKey: ['notifications'] });
    void queryClient.invalidateQueries({ queryKey: ['me'] });
  };

  const markRead = useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.POST('/api/v1/notifications/{notification_id}/read', {
        params: { path: { notification_id: id } },
      });
      if (error) throw new Error(extractApiError(error, '标记已读失败'));
    },
    onSuccess: () => {
      invalidateNotifications();
    },
    onError: (e) => toast.error(e.message),
  });

  const markAllRead = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST('/api/v1/notifications/read-all');
      if (error) throw new Error(extractApiError(error, '全部已读失败'));
    },
    onSuccess: () => {
      toast.success('已全部标记为已读');
      invalidateNotifications();
    },
    onError: (e) => toast.error(e.message),
  });

  const unread = notifications.data?.unread_count ?? 0;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <div className="space-y-4 lg:col-span-5">
        <Card>
          <CardHeader>
            <CardTitle>个人信息</CardTitle>
          </CardHeader>
          <CardContent>
            {me.isLoading ? (
              <div className="grid place-items-center py-10">
                <Spinner className="size-5 text-muted-foreground" />
              </div>
            ) : me.isError ? (
              <p className="text-sm text-destructive">{(me.error as Error).message}</p>
            ) : me.data ? (
              <>
                <div className="mb-4 flex items-center gap-3">
                  <Avatar className="size-12">
                    <AvatarFallback>
                      <User className="size-5" />
                    </AvatarFallback>
                  </Avatar>
                  <h2 className="text-xl font-semibold">{me.data.username}</h2>
                </div>
                <DescriptionList cols={1}>
                  <DescriptionItem label="用户 ID">{me.data.user_id}</DescriptionItem>
                  <DescriptionItem label="租户">
                    {me.data.tenant_name ?? me.data.tenant_id ?? '-'}
                  </DescriptionItem>
                  <DescriptionItem label="全局角色">
                    {(me.data.roles ?? []).length > 0 ? (
                      <span className="flex flex-wrap items-center gap-1">
                        {me.data.roles.map((r) => (
                          <Badge key={r} variant="outline">
                            {r}
                          </Badge>
                        ))}
                      </span>
                    ) : null}
                  </DescriptionItem>
                  <DescriptionItem label="空间角色">
                    {(spaces.data ?? []).length > 0 ? (
                      <span className="flex flex-wrap items-center gap-1">
                        {(spaces.data ?? []).map((s) => (
                          <Badge key={s.id} variant="outline">
                            {s.name}：{ROLE_META[s.my_role ?? '']?.text ?? s.my_role}
                          </Badge>
                        ))}
                      </span>
                    ) : null}
                  </DescriptionItem>
                </DescriptionList>
              </>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>各空间配额</CardTitle>
          </CardHeader>
          <CardContent>
            {spaces.isLoading ? (
              <div className="grid place-items-center py-10">
                <Spinner className="size-5 text-muted-foreground" />
              </div>
            ) : (spaces.data?.length ?? 0) === 0 ? (
              <EmptyState description="暂无空间" />
            ) : (
              <div className="space-y-4">
                {(spaces.data ?? []).map((s, i) => {
                  const u = usages[i]?.data;
                  return (
                    <div key={s.id} className="space-y-1">
                      <p className="text-sm font-semibold">{s.name}</p>
                      {u ? (
                        <>
                          <p className="text-sm text-muted-foreground">
                            存储 {formatBytes(u.storage_bytes)} /{' '}
                            {u.quota_storage_bytes > 0 ? formatBytes(u.quota_storage_bytes) : '不限额'}{' '}
                            · 文件 {u.file_count} /{' '}
                            {u.quota_file_count > 0 ? u.quota_file_count : '不限额'}
                          </p>
                          <QuotaBar percent={percentOf(u.storage_bytes, u.quota_storage_bytes)} />
                          <QuotaBar percent={percentOf(u.file_count, u.quota_file_count)} />
                        </>
                      ) : (
                        <p className="text-sm text-muted-foreground">（用量加载中）</p>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="lg:col-span-7">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <span className="relative inline-flex">
                <Bell className="size-4" />
                {unread > 0 && (
                  <span className="absolute -top-1.5 -right-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] leading-none font-medium text-white">
                    {unread}
                  </span>
                )}
              </span>
              通知中心
            </CardTitle>
            <CardAction>
              <Button
                variant="outline"
                size="sm"
                disabled={unread === 0 || markAllRead.isPending}
                onClick={() => markAllRead.mutate()}
              >
                <Check /> 全部已读
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            {notifications.isLoading ? (
              <div className="grid place-items-center py-10">
                <Spinner className="size-5 text-muted-foreground" />
              </div>
            ) : notifications.isError ? (
              <p className="text-sm text-destructive">
                {(notifications.error as Error).message}
              </p>
            ) : (notifications.data?.items.length ?? 0) === 0 ? (
              <EmptyState description="暂无通知" />
            ) : (
              <ul className="divide-y">
                {(notifications.data?.items ?? []).map((n) => (
                  <li key={n.id} className="flex items-start justify-between gap-3 py-3">
                    <div className="min-w-0 space-y-1">
                      <p className="flex items-center gap-2 text-sm font-medium">
                        {!n.read && (
                          <span className="size-2 shrink-0 animate-pulse rounded-full bg-blue-500" />
                        )}
                        <span className="truncate">{n.title}</span>
                        <Badge variant="outline">{n.type}</Badge>
                      </p>
                      <p className="text-sm break-all">{JSON.stringify(n.payload)}</p>
                      <p className="text-sm text-muted-foreground">
                        {new Date(n.created_at).toLocaleString()}
                      </p>
                    </div>
                    {!n.read && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="shrink-0"
                        disabled={markRead.isPending && markRead.variables === n.id}
                        onClick={() => markRead.mutate(n.id)}
                      >
                        {markRead.isPending && markRead.variables === n.id ? '标记中…' : '标记已读'}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
