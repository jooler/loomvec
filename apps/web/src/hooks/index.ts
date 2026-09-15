/**
 * 共享 react-query hooks：当前用户 / 空间列表 / 空间维度字典（分类、标签）/ 通知。
 * 同一 queryKey 的 queryFn 必须全库唯一（返回形态一致），否则缓存互相污染。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import type { MeInfo } from '../auth';
import { extractApiError } from '../utils';
import { t } from '@/i18n';

/** 当前用户信息（个人中心 / 通知未读数共用缓存）。 */
export function useMe() {
  return useQuery({
    queryKey: ['me'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/me');
      if (error) throw new Error(extractApiError(error, t('profile:loadMeFailed')));
      return data as unknown as MeInfo;
    },
    staleTime: 30_000,
  });
}

/** 我的空间列表（多页共用缓存）。 */
export function useMySpaces() {
  return useQuery({
    queryKey: ['spaces'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/spaces');
      if (error) throw new Error(extractApiError(error, t('spaces:loadListFailed')));
      return data.items;
    },
  });
}

/** 公共空间条目（P5）：经分组可见；仅可链接/断开，不可进入浏览。 */
export interface PublicSpaceItem {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  linked: boolean;
}

/** 对我可见的公共空间列表（含链接状态；空间页/问答检索源共用缓存）。 */
export function usePublicSpaces() {
  return useQuery({
    queryKey: ['public-spaces'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/public-spaces');
      if (error) throw new Error(extractApiError(error, t('spaces:loadPublicFailed')));
      return data.items as unknown as PublicSpaceItem[];
    },
  });
}

/** 公共空间链接开关（P5）：链接后该空间可作为问答检索源。 */
export function useSpaceLinkToggle() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (vars: { spaceId: string; linked: boolean }) => {
      const { data, error } = await api.PUT('/api/v1/public-spaces/{space_id}/link', {
        params: { path: { space_id: vars.spaceId } },
        body: { linked: vars.linked },
      });
      if (error)
        throw new Error(
          extractApiError(error, vars.linked ? t('spaces:linkFailed') : t('spaces:unlinkFailed')),
        );
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['public-spaces'] });
    },
    onError: (e) => toast.error(e.message),
  });
}

/** 对话会话条目（侧栏会话列表 / 对话页共用）。 */
export interface ChatSessionItem {
  session_id: string;
  title: string;
  scope_space_ids: string[];
}

/** 对话会话列表（AppLayout 侧栏 + 对话页共用缓存）。 */
export function useChatSessions() {
  return useQuery({
    queryKey: ['chat-sessions'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/chat/sessions', {});
      if (error) throw new Error(extractApiError(error, t('chat:loadSessionsFailed')));
      return data as unknown as { items: ChatSessionItem[]; total: number };
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
      if (error) throw new Error(extractApiError(error, t('assets:loadCategoriesFailed')));
      return data;
    },
  });
}

/** 空间标签选项（空间标签字典接口）。 */
export function useSpaceTags(spaceId?: string | null) {
  return useQuery({
    queryKey: ['space-tags', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const r = await api.GET('/api/v1/spaces/{space_id}/tags', {
        params: { path: { space_id: spaceId! } },
      });
      if (r.error) throw new Error(extractApiError(r.error, t('assets:loadTagsFailed')));
      return r.data.map((t) => ({ id: t.id, name: t.name }));
    },
    staleTime: 60_000,
  });
}

/** 通知列表（侧栏未读徽标 30s 轮询 + 通知页共用缓存，单次最多 200 条）。 */
export function useNotifications() {
  return useQuery({
    queryKey: ['notifications'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/notifications', {
        params: { query: { limit: 200 } },
      });
      if (error) throw new Error(extractApiError(error, t('notifications:loadFailed')));
      return data;
    },
    refetchInterval: 30_000,
  });
}

/** 通知已读动作（顶栏铃铛与个人中心共用）。 */
export function useNotificationActions() {
  const queryClient = useQueryClient();
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['notifications'] });
    void queryClient.invalidateQueries({ queryKey: ['me'] });
  };

  const markRead = useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.POST('/api/v1/notifications/{notification_id}/read', {
        params: { path: { notification_id: id } },
      });
      if (error) throw new Error(extractApiError(error, t('notifications:markReadFailed')));
    },
    onSuccess: invalidate,
    onError: (e) => toast.error(e.message),
  });

  const markAllRead = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST('/api/v1/notifications/read-all');
      if (error) throw new Error(extractApiError(error, t('notifications:markAllReadFailed')));
    },
    onSuccess: invalidate,
    onError: (e) => toast.error(e.message),
  });

  return { markRead, markAllRead };
}
