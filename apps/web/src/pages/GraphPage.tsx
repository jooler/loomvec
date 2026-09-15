import { useState } from 'react';
import { useParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { GitMerge, RotateCcw, ShieldAlert, Undo2 } from 'lucide-react';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { useMySpaces } from '@/hooks';
import { extractApiError } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@loomvec/ui/components/ui/alert-dialog';

/**
 * P3-WEB-05 图谱管理（空间 owner 视角）：实体列表 / 合并日志审阅与回滚 /
 * 重建触发。merge_log 回滚为高危操作，二次确认后执行。
 */

interface GraphStats {
  entities: number;
  edges: number;
  merges: number;
}

interface EntityItem {
  entity_id: string;
  name: string;
  type: string;
  description: string | null;
  aliases: string[];
}

interface MergeItem {
  log_id: string;
  status: string;
  reason: string;
  score: number | null;
  created_by: string | null;
  created_at: string | null;
  snapshot_names?: { loser?: unknown; winner?: unknown };
}

export function GraphPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const queryClient = useQueryClient();
  const { t } = useTranslation('graph');
  const [entityQuery, setEntityQuery] = useState('');

  const spaces = useMySpaces();
  const myRole = (spaces.data ?? []).find((s) => s.id === spaceId)?.my_role;
  const isOwner = myRole === 'owner';

  const stats = useQuery({
    queryKey: ['graph-stats', spaceId],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/spaces/{space_id}/graph/stats', {
        params: { path: { space_id: spaceId! } },
      });
      if (resp.error) throw new Error(extractApiError(resp.error, t('loadStatsFailed')));
      return resp.data as unknown as GraphStats;
    },
    enabled: !!spaceId,
  });

  const entities = useQuery({
    queryKey: ['graph-entities', spaceId, entityQuery],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/spaces/{space_id}/graph/entities', {
        params: { path: { space_id: spaceId! }, query: { q: entityQuery || undefined, limit: 100 } },
      });
      if (resp.error) throw new Error(extractApiError(resp.error, t('loadEntitiesFailed')));
      return resp.data as unknown as { items: EntityItem[]; total: number };
    },
    enabled: !!spaceId,
  });

  const merges = useQuery({
    queryKey: ['graph-merges', spaceId],
    queryFn: async () => {
      const resp = await api.GET('/api/v1/spaces/{space_id}/graph/merges', {
        params: { path: { space_id: spaceId! }, query: { limit: 50 } },
      });
      if (resp.error) throw new Error(extractApiError(resp.error, t('loadMergesFailed')));
      return resp.data as unknown as { items: MergeItem[]; total: number };
    },
    enabled: !!spaceId,
  });

  const rollback = useMutation({
    mutationFn: async (logId: string) => {
      const { error } = await api.POST(
        '/api/v1/spaces/{space_id}/graph/merges/{log_id}/rollback',
        { params: { path: { space_id: spaceId!, log_id: logId } } },
      );
      if (error) throw new Error(extractApiError(error, t('rollbackFailed')));
    },
    onSuccess: () => {
      toast.success(t('rolledBack'));
      queryClient.invalidateQueries({ queryKey: ['graph-merges', spaceId] });
      queryClient.invalidateQueries({ queryKey: ['graph-entities', spaceId] });
      queryClient.invalidateQueries({ queryKey: ['graph-stats', spaceId] });
    },
    onError: (e) => toast.error(e.message),
  });

  const runMerge = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST('/api/v1/spaces/{space_id}/graph/merge/run', {
        params: { path: { space_id: spaceId! } },
        body: {},
      });
      if (error) throw new Error(extractApiError(error, t('triggerFailed')));
    },
    onSuccess: () => {
      toast.success(t('mergeQueued'));
      queryClient.invalidateQueries({ queryKey: ['graph-merges', spaceId] });
    },
    onError: (e) => toast.error(e.message),
  });

  const rebuild = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST('/api/v1/spaces/{space_id}/graph/rebuild', {
        params: { path: { space_id: spaceId! } },
        body: { reprocess_assets: true },
      });
      if (error) throw new Error(extractApiError(error, t('triggerFailed')));
    },
    onSuccess: () => {
      toast.success(t('rebuildQueued'));
      queryClient.invalidateQueries({ queryKey: ['graph-stats', spaceId] });
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Card className="gap-2 py-4">
          <CardContent>
            <p className="text-sm text-muted-foreground">{t('statEntities')}</p>
            <p className="text-2xl font-semibold">{stats.data?.entities ?? '—'}</p>
          </CardContent>
        </Card>
        <Card className="gap-2 py-4">
          <CardContent>
            <p className="text-sm text-muted-foreground">{t('statEdges')}</p>
            <p className="text-2xl font-semibold">
              {stats.data?.edges === -1 ? t('graphUnavailable') : (stats.data?.edges ?? '—')}
            </p>
          </CardContent>
        </Card>
        <Card className="gap-2 py-4">
          <CardContent>
            <p className="text-sm text-muted-foreground">{t('statMerges')}</p>
            <p className="text-2xl font-semibold">{stats.data?.merges ?? '—'}</p>
          </CardContent>
        </Card>
      </div>

      <Card className="py-4">
        <CardHeader className="flex-row items-center justify-between border-b pb-3">
          <CardTitle className="text-base">{t('entitiesTitle')}</CardTitle>
          <div className="flex items-center gap-2">
            <Input
              className="h-8 w-48"
              placeholder={t('searchPlaceholder')}
              value={entityQuery}
              onChange={(e) => setEntityQuery(e.target.value)}
            />
            {isOwner && (
              <>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={runMerge.isPending}
                  onClick={() => runMerge.mutate()}
                >
                  <GitMerge /> {t('runMerge')}
                </Button>
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button size="sm" variant="outline" disabled={rebuild.isPending}>
                      {t('rebuild')}
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>{t('rebuildTitle')}</AlertDialogTitle>
                      <AlertDialogDescription>{t('rebuildDesc')}</AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>{t('action.cancel')}</AlertDialogCancel>
                      <AlertDialogAction onClick={() => rebuild.mutate()}>
                        {t('confirmRebuild')}
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              </>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {entities.isLoading ? (
            <div className="grid place-items-center py-8">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : entities.isError ? (
            <p className="text-sm text-destructive">{(entities.error as Error).message}</p>
          ) : (entities.data?.items?.length ?? 0) === 0 ? (
            <EmptyState title={t('emptyEntities')} description={t('emptyEntitiesDesc')} />
          ) : (
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {(entities.data?.items ?? []).map((e) => (
                <div key={e.entity_id} className="rounded-md border p-3">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">{e.name}</span>
                    <StatusBadge tone="blue">{e.type}</StatusBadge>
                  </div>
                  {e.description && (
                    <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{e.description}</p>
                  )}
                  {e.aliases.length > 0 && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t('aliasesLabel')}
                      {e.aliases.join('、')}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="py-4">
        <CardHeader className="items-center border-b pb-3">
          <CardTitle className="text-base">{t('mergesTitle')}</CardTitle>
          {!isOwner && (
            <CardAction>
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <ShieldAlert className="size-3" />
                {t('ownerRequired')}
              </span>
            </CardAction>
          )}
        </CardHeader>
        <CardContent>
          {merges.isLoading ? (
            <div className="grid place-items-center py-8">
              <Spinner className="size-5 text-muted-foreground" />
            </div>
          ) : merges.isError ? (
            <p className="text-sm text-destructive">{(merges.error as Error).message}</p>
          ) : (merges.data?.items?.length ?? 0) === 0 ? (
            <EmptyState title={t('emptyMerges')} />
          ) : (
            <div className="divide-y">
              {(merges.data?.items ?? []).map((log) => (
                <div key={log.log_id} className="flex flex-wrap items-center gap-3 py-3">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm">
                      <span className="text-muted-foreground">{String(log.snapshot_names?.loser ?? '?')}</span>
                      <RotateCcw className="mx-2 inline size-3 rotate-90 text-muted-foreground" />
                      <span className="font-medium">{String(log.snapshot_names?.winner ?? '?')}</span>
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {t(`reason.${log.reason}`, { defaultValue: log.reason })}
                      {log.score !== null && log.score !== undefined && ` · ${t('similarity', { score: log.score.toFixed(3) })}`}
                      {` · ${new Date(String(log.created_at)).toLocaleString()}`}
                      {log.created_by ? ` · by ${log.created_by}` : ''}
                    </p>
                  </div>
                  {log.status === 'rolled_back' ? (
                    <StatusBadge tone="gray">{t('statusRolledBack')}</StatusBadge>
                  ) : (
                    <div className="flex items-center gap-2">
                      <StatusBadge tone="green">{t('statusActive')}</StatusBadge>
                      <AlertDialog>
                        <AlertDialogTrigger asChild>
                          <Button size="sm" variant="outline" disabled={!isOwner}>
                            <Undo2 /> {t('rollback')}
                          </Button>
                        </AlertDialogTrigger>
                        <AlertDialogContent>
                          <AlertDialogHeader>
                            <AlertDialogTitle>{t('rollbackTitle')}</AlertDialogTitle>
                            <AlertDialogDescription>
                              {t('rollbackDesc', { name: String(log.snapshot_names?.loser ?? '?') })}
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>{t('action.cancel')}</AlertDialogCancel>
                            <AlertDialogAction onClick={() => rollback.mutate(log.log_id)}>
                              {t('confirmRollback')}
                            </AlertDialogAction>
                          </AlertDialogFooter>
                        </AlertDialogContent>
                      </AlertDialog>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
