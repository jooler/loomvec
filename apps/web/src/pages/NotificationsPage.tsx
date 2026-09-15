import { useNavigate } from 'react-router';
import { Bell, Check } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNotificationActions, useNotifications } from '@/hooks';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

/**
 * 通知中心（用户级独立页面）：全部通知的统一管理入口（未读徽标 / 单条已读 / 全部已读）。
 * 点击通知即标记已读；payload 携带 asset_id 时跳转资产详情。
 */

/** 通知 payload 的可读摘要（k=v 串联；asset_id 由点击跳转承担，不重复展示）。 */
function payloadSummary(payload: Record<string, unknown>): string {
  const entries = Object.entries(payload).filter(([k]) => k !== 'asset_id');
  return entries.map(([k, v]) => `${k}=${String(v)}`).join(' · ');
}

export function NotificationsPage() {
  const navigate = useNavigate();
  const { t } = useTranslation('notifications');
  const notifications = useNotifications();
  const { markRead, markAllRead } = useNotificationActions();

  const unread = notifications.data?.unread_count ?? 0;

  /** 点击通知：标记已读 + payload 携带 asset_id 时跳转资产详情。 */
  const openNotification = (id: string, read: boolean, payload: Record<string, unknown>) => {
    if (!read) markRead.mutate(id);
    const assetId = payload?.asset_id;
    if (typeof assetId === 'string' && assetId) navigate(`/a/${assetId}`);
  };

  return (
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
          {t('title')}
        </CardTitle>
        <CardAction>
          <Button
            variant="outline"
            size="sm"
            disabled={unread === 0 || markAllRead.isPending}
            onClick={() => markAllRead.mutate()}
          >
            <Check /> {t('markAllRead')}
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        {notifications.isLoading ? (
          <div className="grid place-items-center py-10">
            <Spinner className="size-5 text-muted-foreground" />
          </div>
        ) : notifications.isError ? (
          <p className="text-sm text-destructive">{(notifications.error as Error).message}</p>
        ) : (notifications.data?.items.length ?? 0) === 0 ? (
          <EmptyState description={t('empty')} />
        ) : (
          <ul className="divide-y">
            {(notifications.data?.items ?? []).map((n) => {
              const payload = (n.payload ?? {}) as Record<string, unknown>;
              const assetId = typeof payload.asset_id === 'string' ? payload.asset_id : null;
              const summary = payloadSummary(payload);
              return (
                <li
                  key={n.id}
                  className={`flex items-start justify-between gap-3 rounded-md px-2 py-3 ${
                    assetId ? 'cursor-pointer hover:bg-muted/60' : ''
                  }`}
                  onClick={() => assetId && openNotification(n.id, n.read, payload)}
                >
                  <div className="min-w-0 space-y-1">
                    <p className="flex items-center gap-2 text-sm font-medium">
                      {!n.read && (
                        <span className="size-2 shrink-0 animate-pulse rounded-full bg-blue-500" />
                      )}
                      <span className="truncate">{n.title}</span>
                      <Badge variant="outline">{n.type}</Badge>
                    </p>
                    {summary && <p className="text-sm break-all text-muted-foreground">{summary}</p>}
                    <p className="text-sm text-muted-foreground">
                      {new Date(n.created_at).toLocaleString()}
                      {assetId && <span className="ml-2 text-primary">{t('viewAsset')}</span>}
                    </p>
                  </div>
                  {!n.read && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="shrink-0"
                      disabled={markRead.isPending && markRead.variables === n.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        markRead.mutate(n.id);
                      }}
                    >
                      {markRead.isPending && markRead.variables === n.id
                        ? t('marking')
                        : t('markRead')}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
