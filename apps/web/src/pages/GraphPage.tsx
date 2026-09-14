import { useState } from 'react';
import { useParams } from 'react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { GitMerge, RotateCcw, ShieldAlert, Undo2 } from 'lucide-react';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
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

const REASON_LABEL: Record<string, string> = {
  auto_vector: '向量近邻',
  auto_name: '名称相似',
  manual: '人工',
};

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
      if (resp.error) throw new Error(extractApiError(resp.error, '加载图谱统计失败'));
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
      if (resp.error) throw new Error(extractApiError(resp.error, '加载实体失败'));
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
      if (resp.error) throw new Error(extractApiError(resp.error, '加载合并日志失败'));
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
      if (error) throw new Error(extractApiError(error, '回滚失败（需要 owner 角色）'));
    },
    onSuccess: () => {
      toast.success('已回滚该次合并');
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
      if (error) throw new Error(extractApiError(error, '触发失败（需要 owner 角色）'));
    },
    onSuccess: () => {
      toast.success('合并任务已入队（低优先级队列异步执行）');
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
      if (error) throw new Error(extractApiError(error, '触发失败（需要 owner 角色）'));
    },
    onSuccess: () => {
      toast.success('图谱重建已入队，将逐资产重跑图谱步骤');
      queryClient.invalidateQueries({ queryKey: ['graph-stats', spaceId] });
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Card className="gap-2 py-4">
          <CardContent>
            <p className="text-sm text-muted-foreground">实体</p>
            <p className="text-2xl font-semibold">{stats.data?.entities ?? '—'}</p>
          </CardContent>
        </Card>
        <Card className="gap-2 py-4">
          <CardContent>
            <p className="text-sm text-muted-foreground">关系（图边）</p>
            <p className="text-2xl font-semibold">
              {stats.data?.edges === -1 ? '图谱不可用' : (stats.data?.edges ?? '—')}
            </p>
          </CardContent>
        </Card>
        <Card className="gap-2 py-4">
          <CardContent>
            <p className="text-sm text-muted-foreground">合并次数</p>
            <p className="text-2xl font-semibold">{stats.data?.merges ?? '—'}</p>
          </CardContent>
        </Card>
      </div>

      <Card className="py-4">
        <CardHeader className="flex-row items-center justify-between border-b pb-3">
          <CardTitle className="text-base">实体列表</CardTitle>
          <div className="flex items-center gap-2">
            <Input
              className="h-8 w-48"
              placeholder="搜索实体名"
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
                  <GitMerge /> 运行合并
                </Button>
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button size="sm" variant="outline" disabled={rebuild.isPending}>
                      重建图谱
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>重建空间图谱？</AlertDialogTitle>
                      <AlertDialogDescription>
                        将清空该空间的图结构并逐资产重跑图谱抽取（低优先级队列异步执行）。
                        实体主数据与合并日志保留。已完成抽取的资产会重新调用 LLM。
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>取消</AlertDialogCancel>
                      <AlertDialogAction onClick={() => rebuild.mutate()}>
                        确认重建
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
            <EmptyState title="暂无实体" description="上传文档并完成图谱抽取后，实体将出现在这里。" />
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
                    <p className="mt-1 text-xs text-muted-foreground">别名：{e.aliases.join('、')}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="py-4">
        <CardHeader className="items-center border-b pb-3">
          <CardTitle className="text-base">合并日志（可回滚）</CardTitle>
          {!isOwner && (
            <CardAction>
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <ShieldAlert className="size-3" />
                回滚/重建需空间 owner
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
            <EmptyState title="暂无合并记录" />
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
                      {REASON_LABEL[log.reason] ?? log.reason}
                      {log.score !== null && log.score !== undefined && ` · 相似度 ${log.score.toFixed(3)}`}
                      {` · ${new Date(String(log.created_at)).toLocaleString()}`}
                      {log.created_by ? ` · by ${log.created_by}` : ''}
                    </p>
                  </div>
                  {log.status === 'rolled_back' ? (
                    <StatusBadge tone="gray">已回滚</StatusBadge>
                  ) : (
                    <div className="flex items-center gap-2">
                      <StatusBadge tone="green">已生效</StatusBadge>
                      <AlertDialog>
                        <AlertDialogTrigger asChild>
                          <Button size="sm" variant="outline" disabled={!isOwner}>
                            <Undo2 /> 回滚
                          </Button>
                        </AlertDialogTrigger>
                        <AlertDialogContent>
                          <AlertDialogHeader>
                            <AlertDialogTitle>回滚这次实体合并？</AlertDialogTitle>
                            <AlertDialogDescription>
                              将按合并日志快照恢复「{String(log.snapshot_names?.loser ?? '?')}」及其关系边。
                              图结构恢复为合并前状态。
                            </AlertDialogDescription>
                          </AlertDialogHeader>
                          <AlertDialogFooter>
                            <AlertDialogCancel>取消</AlertDialogCancel>
                            <AlertDialogAction onClick={() => rollback.mutate(log.log_id)}>
                              确认回滚
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
