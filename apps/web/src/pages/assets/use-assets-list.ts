/**
 * 资产列表查询（页面与子组件共用的类型来源）。
 */
import { useQuery } from '@tanstack/react-query';
import { api } from '@loomvec/sdk-ts';
import { extractApiError } from '@/utils';
import { t } from '@/i18n';

export interface AssetFilters {
  ext?: string;
  status?: string;
  review_status?: string;
  tagIds: string[];
  categoryId?: string;
}

export const EMPTY_FILTERS: AssetFilters = { tagIds: [] };

export function useAssetsList(spaceId?: string, filters: AssetFilters = EMPTY_FILTERS) {
  return useQuery({
    queryKey: ['assets', spaceId ?? 'all', filters],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets', {
        params: {
          query: {
            limit: 50,
            space_id: spaceId,
            ext: filters.ext || undefined,
            status: filters.status || undefined,
            review_status: filters.review_status || undefined,
            category_id: filters.categoryId || undefined,
            tag_ids: filters.tagIds.length > 0 ? filters.tagIds.join(',') : undefined,
          },
        },
      });
      if (error) throw new Error(extractApiError(error, t('assets:loadListFailed')));
      return data;
    },
    // 处理中轮询（P1 用轮询，SSE/Webhook 后置）
    refetchInterval: (query) =>
      query.state.data?.items.some((a) => ['pending', 'processing'].includes(a.status))
        ? 3000
        : false,
  });
}

export type AssetListItem = NonNullable<ReturnType<typeof useAssetsList>['data']>['items'][number];
