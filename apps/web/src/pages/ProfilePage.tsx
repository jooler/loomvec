import { User } from 'lucide-react';
import { useQueries } from '@tanstack/react-query';
import { api } from '@loomvec/sdk-ts';
import { useMe, useMySpaces } from '@/hooks';
import { extractApiError, formatBytes, percentOf, ROLE_META } from '@/utils';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { Avatar, AvatarFallback } from '@loomvec/ui/components/ui/avatar';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

/**
 * P2-WEB-07 个人中心：当前用户信息 + 各空间配额进度。
 * 通知管理已独立为「通知」页面（侧栏条目进入），此处不再展示。
 */

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
  const me = useMe();
  const spaces = useMySpaces();

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

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
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
  );
}
