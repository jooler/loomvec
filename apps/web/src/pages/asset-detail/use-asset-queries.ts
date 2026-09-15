/**
 * 资产详情数据查询（页面与子组件共用的类型来源）。
 */
import { useQuery } from '@tanstack/react-query';
import { api } from '@loomvec/sdk-ts';
import { extractApiError } from '@/utils';
import { t } from '@/i18n';

export function useAssetDetail(assetId?: string) {
  return useQuery({
    queryKey: ['asset', assetId],
    enabled: !!assetId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, t('ui:viewer.loadAssetFailed')));
      return data;
    },
    refetchInterval: (q) =>
      q.state.data && ['pending', 'processing'].includes(q.state.data.status) ? 3000 : false,
  });
}

export function useAssetJobs(assetId?: string) {
  return useQuery({
    queryKey: ['asset-jobs', assetId],
    enabled: !!assetId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets/{asset_id}/jobs', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, t('assetDetail:loadJobsFailed')));
      return data;
    },
  });
}

export function useAssetPreview(assetId?: string) {
  return useQuery({
    queryKey: ['asset-preview', assetId],
    enabled: !!assetId,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/assets/{asset_id}/preview', {
        params: { path: { asset_id: assetId! } },
      });
      if (error) throw new Error(extractApiError(error, t('ui:viewer.loadPreviewFailed')));
      return data;
    },
  });
}

export type AssetDetailData = NonNullable<ReturnType<typeof useAssetDetail>['data']>;
export type AssetJob = NonNullable<ReturnType<typeof useAssetJobs>['data']>[number];
