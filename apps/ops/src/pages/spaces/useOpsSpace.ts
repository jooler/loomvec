import { useQuery } from '@tanstack/react-query';
import { api, unwrap } from '@/api';

/** 运营端公共空间详情（GET /ops/spaces/{id}）；布局 / 资产 / 图谱 / 设置页共用同一缓存键。 */
export interface OpsSpaceDetail {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  review_required: boolean;
  embedding_model: string | null;
  chunk_preset: string | null;
  /** 抽取领域配置 key；null = 跟随全局默认 */
  extraction_profile: string | null;
  /** false = 所选自定义配置已失效（被删），管线正回落全局默认；留空恒为 true */
  extraction_profile_valid: boolean;
  quota_storage_bytes: number;
  quota_file_count: number;
  created_at: string;
}

export function useOpsSpace(spaceId?: string) {
  return useQuery({
    queryKey: ['ops-space', spaceId],
    queryFn: () =>
      unwrap<OpsSpaceDetail>(
        api.GET('/api/v1/ops/spaces/{space_id}', { params: { path: { space_id: spaceId! } } }),
      ),
    enabled: !!spaceId,
  });
}
