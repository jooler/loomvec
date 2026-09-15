import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams } from 'react-router';
import { GitMerge, RotateCcw } from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
import { extractApiError } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@loomvec/ui/components/ui/table';

/**
 * 图谱页签（运营端知识库维护）：统计 / 实体检索 / 合并日志与回滚 / 重建触发。
 * 与用户端图谱页同链路（/spaces/{id}/graph/*），运营者以 owner 成员身份操作。
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
}

interface MergeItem {
  log_id: string;
  status: string;
  reason: string;
  score: number | null;
  created_at: string | null;
}

export function GraphPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const queryClient = useQueryClient();
  const { t } = useTranslation('spaces');
  // 输入即时受控，防抖 300ms 后才发起检索（逐键打接口会造成请求风暴与列表闪烁）
  const [entityInput, setEntityInput] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(entityInput.trim()), 300);
    return () => clearTimeout(timer);
  }, [entityInput]);

  const stats = useQuery({
    queryKey: ['ops-graph-stats', spaceId],
    queryFn: () =>
      unwrap<GraphStats>(
        api.GET('/api/v1/spaces/{space_id}/graph/stats', {
          params: { path: { space_id: spaceId! } },
        }),
      ),
    enabled: !!spaceId,
  });

  const entities = useQuery({
    queryKey: ['ops-graph-entities', spaceId, debouncedQuery],
    queryFn: () =>
      unwrap<{ items: EntityItem[]; total: number }>(
        api.GET('/api/v1/spaces/{space_id}/graph/entities', {
          params: { path: { space_id: spaceId! }, query: { q: debouncedQuery || undefined, limit: 50 } },
        }),
      ),
    enabled: !!spaceId,
    // 击键间隙保持旧列表，避免闪回加载态
    placeholderData: (prev) => prev,
  });

  const merges = useQuery({
    queryKey: ['ops-graph-merges', spaceId],
    queryFn: () =>
      unwrap<{ items: MergeItem[]; total: number }>(
        api.GET('/api/v1/spaces/{space_id}/graph/merges', {
          params: { path: { space_id: spaceId! }, query: { limit: 20 } },
        }),
      ),
    enabled: !!spaceId,
  });

  const invalidateGraph = () => {
    void queryClient.invalidateQueries({ queryKey: ['ops-graph-merges', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['ops-graph-entities', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['ops-graph-stats', spaceId] });
  };

  const rollback = useMutation({
    mutationFn: async (logId: string) => {
      const { error } = await api.POST('/api/v1/spaces/{space_id}/graph/merges/{log_id}/rollback', {
        params: { path: { space_id: spaceId!, log_id: logId } },
      });
      if (error) throw new Error(extractApiError(error, t('graph.rollbackFailed')));
    },
    onSuccess: () => {
      toast.success(t('graph.rolledBack'));
      invalidateGraph();
    },
    onError: (e) => toast.error(e.message),
  });

  const runMerge = useMutation({
    mutationFn: async () => {
      await unwrap(
        api.POST('/api/v1/spaces/{space_id}/graph/merge/run', {
          params: { path: { space_id: spaceId! } },
          body: {},
        }),
      );
    },
    onSuccess: () => {
      toast.success(t('graph.mergeEnqueued'));
      void queryClient.invalidateQueries({ queryKey: ['ops-graph-merges', spaceId] });
    },
    onError: (e) => toast.error(e.message),
  });

  const rebuild = useMutation({
    mutationFn: async () => {
      await unwrap(
        api.POST('/api/v1/spaces/{space_id}/graph/rebuild', {
          params: { path: { space_id: spaceId! } },
          body: { reprocess_assets: true },
        }),
      );
    },
    onSuccess: () => {
      toast.success(t('graph.rebuildEnqueued'));
      void queryClient.invalidateQueries({ queryKey: ['ops-graph-stats', spaceId] });
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-3">
        {[
          { label: t('graph.statEntities'), value: stats.data?.entities },
          { label: t('graph.statEdges'), value: stats.data?.edges },
          { label: t('graph.statMerges'), value: stats.data?.merges },
        ].map((card) => (
          <Card key={card.label}>
            <CardHeader>
              <CardDescription>{card.label}</CardDescription>
              <CardTitle className="text-2xl">
                {stats.isLoading ? <Spinner className="size-4" /> : (card.value ?? '-')}
              </CardTitle>
            </CardHeader>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t('graph.searchTitle')}</CardTitle>
          <CardAction className="flex gap-2">
            <Input
              value={entityInput}
              onChange={(e) => setEntityInput(e.target.value)}
              placeholder={t('graph.searchPlaceholder')}
              className="w-56"
            />
          </CardAction>
        </CardHeader>
        <CardContent>
          {entities.isLoading ? (
            <div className="grid place-items-center py-6">
              <Spinner className="size-4 text-muted-foreground" />
            </div>
          ) : (entities.data?.items.length ?? 0) === 0 ? (
            <EmptyState title={t('graph.emptyEntities')} />
          ) : (
            <div className="max-h-72 overflow-y-auto rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('field.name')}</TableHead>
                    <TableHead>{t('graph.colType')}</TableHead>
                    <TableHead>{t('field.description')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {entities.data?.items.map((e) => (
                    <TableRow key={e.entity_id}>
                      <TableCell className="font-medium">{e.name}</TableCell>
                      <TableCell>
                        <StatusBadge tone="blue">{e.type}</StatusBadge>
                      </TableCell>
                      <TableCell className="max-w-72 truncate text-muted-foreground">
                        {e.description ?? '-'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t('graph.mergeTitle')}</CardTitle>
          <CardAction className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={runMerge.isPending}
              onClick={() => runMerge.mutate()}
            >
              <GitMerge /> {t('graph.runMerge')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={rebuild.isPending}
              onClick={() => rebuild.mutate()}
            >
              <RotateCcw /> {t('graph.rebuildGraph')}
            </Button>
          </CardAction>
        </CardHeader>
        <CardContent>
          {(merges.data?.items.length ?? 0) === 0 ? (
            <EmptyState title={t('graph.emptyMerges')} />
          ) : (
            <div className="divide-y rounded-lg border">
              {merges.data?.items.map((m) => (
                <div key={m.log_id} className="flex items-center justify-between gap-3 px-4 py-2.5">
                  <div className="flex min-w-0 items-center gap-2 text-sm">
                    <StatusBadge
                      tone={m.status === 'applied' ? 'green' : m.status === 'rolled_back' ? 'gray' : 'amber'}
                    >
                      {m.status}
                    </StatusBadge>
                    <span className="text-muted-foreground">{m.reason}</span>
                    {m.score != null && (
                      <span className="text-xs text-muted-foreground">score {m.score.toFixed(3)}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {m.created_at && (
                      <span className="text-xs text-muted-foreground">
                        {new Date(m.created_at).toLocaleString()}
                      </span>
                    )}
                    {m.status === 'applied' && (
                      <Button
                        variant="outline"
                        size="xs"
                        disabled={rollback.isPending}
                        onClick={() => rollback.mutate(m.log_id)}
                      >
                        {t('graph.rollback')}
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
