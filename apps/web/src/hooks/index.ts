/**
 * 共享 react-query hooks：空间列表 / 空间维度字典（分类、标签）/ 通知轮询。
 */
import { useQuery } from '@tanstack/react-query';
import { api } from '@loomvec/sdk-ts';
import { extractApiError } from '../utils';

export interface SpaceLike {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  space_type: string;
  review_required: boolean;
  my_role?: string | null;
  member_count?: number | null;
}

/** 我的空间列表（多页共用缓存）。 */
export function useMySpaces() {
  return useQuery({
    queryKey: ['spaces'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces');
      if (error) throw new Error(extractApiError(error, '加载空间列表失败'));
      return data.items;
    },
  });
}

/** 空间分类（筛选/详情编辑用）。 */
export function useSpaceCategories(spaceId?: string | null) {
  return useQuery({
    queryKey: ['space-categories', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces/{space_id}/categories', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, '加载分类失败'));
      return data;
    },
  });
}

/**
 * 空间标签选项：资产列表 API 不携带标签，这里抽样前 20 个资产的详情聚合（演示规模够用）。
 */
export function useSpaceTags(spaceId?: string | null) {
  return useQuery({
    queryKey: ['space-tags', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const r = await api.GET('/api/v1/spaces/{space_id}/tags', {
        params: { path: { space_id: spaceId! } },
      });
      if (r.error) throw new Error(extractApiError(r.error, '加载标签失败'));
      return r.data.map((t) => ({ id: t.id, name: t.name }));
    },
    staleTime: 60_000,
  });
}

/** 通知列表（AppLayout 轮询 30s + 个人中心共用缓存）。 */
export function useNotifications() {
  return useQuery({
    queryKey: ['notifications'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/notifications');
      if (error) throw new Error(extractApiError(error, '加载通知失败'));
      return data;
    },
    refetchInterval: 30_000,
  });
}
