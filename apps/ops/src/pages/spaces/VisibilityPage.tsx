import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';
import { toast } from 'sonner';
import { api, unwrap } from '@/api';
import { Button } from '@loomvec/ui/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@loomvec/ui/components/ui/card';
import { Checkbox } from '@loomvec/ui/components/ui/checkbox';
import { Skeleton } from '@loomvec/ui/components/ui/skeleton';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { Label } from '@loomvec/ui/components/ui/label';

/**
 * 可见性页签（P5，替代用户端「成员管理」语义）：
 * 列出全部用户分组，勾选 = 空间对该分组公开；保存为全量替换（PUT）。
 * 取消勾选后用户既有链接即时失效（后端 authz 即时求交），无需清理。
 */

interface VisibilityItem {
  group_id: string;
  name: string;
  description: string | null;
  member_count: number;
  visible: boolean;
}

export function VisibilityPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const queryClient = useQueryClient();
  const [checked, setChecked] = useState<Set<string> | null>(null);

  const visibility = useQuery({
    queryKey: ['ops-visibility', spaceId],
    queryFn: () =>
      unwrap<{ items: VisibilityItem[] }>(
        api.GET('/api/v1/ops/spaces/{space_id}/visibility', {
          params: { path: { space_id: spaceId! } },
        }),
      ).then((r) => r.items),
    enabled: !!spaceId,
  });

  // 服务端数据到达 / 变更后同步本地勾选集合；与已保存集合不一致时视为有未保存更改
  useEffect(() => {
    if (visibility.data)
      setChecked(new Set(visibility.data.filter((i) => i.visible).map((i) => i.group_id)));
  }, [visibility.data]);

  const savedKey = useMemo(
    () =>
      (visibility.data ?? [])
        .filter((i) => i.visible)
        .map((i) => i.group_id)
        .sort()
        .join(),
    [visibility.data],
  );
  const dirty = checked !== null && [...checked].sort().join() !== savedKey;

  const save = useMutation({
    mutationFn: async () => {
      await unwrap(
        api.PUT('/api/v1/ops/spaces/{space_id}/visibility', {
          params: { path: { space_id: spaceId! } },
          body: { group_ids: [...(checked ?? [])] },
        }),
      );
    },
    onSuccess: () => {
      toast.success('可见性已更新');
      void queryClient.invalidateQueries({ queryKey: ['ops-visibility', spaceId] });
      void queryClient.invalidateQueries({ queryKey: ['ops-spaces'] });
      void queryClient.invalidateQueries({ queryKey: ['ops-space', spaceId] });
    },
    onError: (e) => toast.error(e.message),
  });

  const toggle = (groupId: string, on: boolean) => {
    setChecked((prev) => {
      const next = new Set(prev ?? []);
      if (on) next.add(groupId);
      else next.delete(groupId);
      return next;
    });
  };

  if (visibility.isLoading) {
    return (
      <div className="space-y-2">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-14 rounded-lg" />
        ))}
      </div>
    );
  }

  if (visibility.isError) {
    return <p className="text-sm text-destructive">{(visibility.error as Error).message}</p>;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>分组可见性</CardTitle>
        <CardDescription>
          勾选的分组可让所属用户在用户端看到该公共空间并选择「链接」；链接后仅作为问答检索源，
          用户不能浏览空间内容。未勾选任何分组 = 不对任何人公开。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {(visibility.data?.length ?? 0) === 0 ? (
          <EmptyState
            title="还没有用户分组"
            description="先到「用户分组」创建分组并添加成员，再回到这里勾选。"
            action={
              <Button asChild variant="outline" size="sm">
                <Link to="/groups">前往用户分组</Link>
              </Button>
            }
          />
        ) : (
          <div className="divide-y rounded-lg border">
            {visibility.data?.map((item) => (
              <div key={item.group_id} className="flex items-center justify-between gap-4 px-4 py-3">
                <div className="flex min-w-0 items-center gap-3">
                  <Checkbox
                    id={`vis-${item.group_id}`}
                    checked={checked?.has(item.group_id) ?? item.visible}
                    onCheckedChange={(v) => toggle(item.group_id, v === true)}
                  />
                  <div className="min-w-0">
                    <Label
                      htmlFor={`vis-${item.group_id}`}
                      className="cursor-pointer text-sm font-medium"
                    >
                      {item.name}
                    </Label>
                    {item.description && (
                      <p className="truncate text-xs text-muted-foreground">{item.description}</p>
                    )}
                  </div>
                </div>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {item.member_count} 名成员
                </span>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          {dirty && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                setChecked(
                  new Set(visibility.data?.filter((i) => i.visible).map((i) => i.group_id)),
                )
              }
            >
              放弃更改
            </Button>
          )}
          <Button
            size="sm"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? '保存中…' : '保存可见性'}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
