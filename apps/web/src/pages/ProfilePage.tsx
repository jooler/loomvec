import { User } from 'lucide-react';
import { useQueries } from '@tanstack/react-query';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { useMe, useMySpaces } from '@/hooks';
import { extractApiError, formatBytes, percentOf } from '@/utils';
import { ApiKeysCard } from '@/components/api-keys-card';
import { DescriptionItem, DescriptionList } from '@loomvec/ui/components/description-list';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { Avatar, AvatarFallback } from '@loomvec/ui/components/ui/avatar';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

/**
 * P2-WEB-07 个人中心：当前用户信息 + 各空间配额进度 + API Key 管理（P5 开放授权）。
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
  const { t } = useTranslation('profile');

  const usages = useQueries({
    queries: (spaces.data ?? []).map((s) => ({
      queryKey: ['space-usage', s.id],
      queryFn: async () => {
        const { data, error } = await api.GET('/api/v1/spaces/{space_id}/usage', {
          params: { path: { space_id: s.id } },
        });
        if (error) throw new Error(extractApiError(error, t('loadUsageFailed')));
        return data;
      },
      staleTime: 30_000,
    })),
  });

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>{t('title')}</CardTitle>
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
                <DescriptionItem label={t('userIdLabel')}>{me.data.user_id}</DescriptionItem>
                <DescriptionItem label={t('tenantLabel')}>
                  {me.data.tenant_name ?? me.data.tenant_id ?? '-'}
                </DescriptionItem>
                <DescriptionItem label={t('globalRolesLabel')}>
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
                <DescriptionItem label={t('spaceRolesLabel')}>
                  {(spaces.data ?? []).length > 0 ? (
                    <span className="flex flex-wrap items-center gap-1">
                      {(spaces.data ?? []).map((s) => (
                        <Badge key={s.id} variant="outline">
                          {s.name}：{t(`role.${s.my_role ?? ''}`, { defaultValue: s.my_role ?? '' })}
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
          <CardTitle>{t('quotasTitle')}</CardTitle>
        </CardHeader>
        <CardContent>
          {spaces.isLoading ? (
            <div className="grid place-items-center py-10">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : (spaces.data?.length ?? 0) === 0 ? (
            <EmptyState description={t('emptySpaces')} />
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
                          {t('storageWord')} {formatBytes(u.storage_bytes)} /{' '}
                          {u.quota_storage_bytes > 0 ? formatBytes(u.quota_storage_bytes) : t('unlimitedQuota')}{' '}
                          · {t('fileCountWord')} {u.file_count} /{' '}
                          {u.quota_file_count > 0 ? u.quota_file_count : t('unlimitedQuota')}
                        </p>
                        <QuotaBar percent={percentOf(u.storage_bytes, u.quota_storage_bytes)} />
                        <QuotaBar percent={percentOf(u.file_count, u.quota_file_count)} />
                      </>
                    ) : (
                      <p className="text-sm text-muted-foreground">{t('usageLoading')}</p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <ApiKeysCard />
    </div>
  );
}
