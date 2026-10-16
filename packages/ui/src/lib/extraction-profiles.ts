/**
 * 抽取领域配置（GET /api/v1/extraction-profiles，web/ops/admin 共用）：
 * 空间设置下拉的数据源 + 实体类型 → 中文说明的标签字典。
 *
 * client 由 app 注入（web 用 SDK 默认单例，admin/ops 用自带 401 处理的实例）。
 */
import { useQuery } from '@tanstack/react-query';
import type { ApiClient } from '@loomvec/sdk-ts';
import { t } from '../i18n';
import { extractApiError } from './format';

export interface ExtractionProfile {
  key: string;
  label: string;
  description: string;
  entity_types: { type: string; label: string }[];
  is_builtin: boolean;
}

export interface ExtractionProfileList {
  items: ExtractionProfile[];
  /** 全局默认配置 key（空间留空时生效） */
  default: string;
}

export function useExtractionProfiles(client: ApiClient) {
  return useQuery({
    queryKey: ['extraction-profiles'],
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/extraction-profiles');
      if (error) throw new Error(extractApiError(error, t('ui:extractionProfile.loadFailed')));
      return data as ExtractionProfileList;
    },
  });
}

/** 中文说明取括号前的部分作短标签：「组织/机构（公司、政府机关…）」→「组织/机构」。 */
export function shortTypeLabel(label: string, fallback: string): string {
  return label.split(/[（(]/)[0].trim() || fallback;
}

/**
 * 空间当前生效的配置：key 为空跟随全局默认；key 不在可选集合（自定义配置被删）时
 * 与管线一致回落全局默认。列表未加载时返回 undefined。
 */
export function resolveProfile(
  profiles: ExtractionProfileList | undefined,
  key: string | null | undefined,
): ExtractionProfile | undefined {
  if (!profiles) return undefined;
  const byKey = (k: string) => profiles.items.find((p) => p.key === k);
  return (key ? byKey(key) : undefined) ?? byKey(profiles.default);
}

/** 空间引用的配置 key 是否已失效（非空且不在可选集合）；列表未加载时按未失效处理。 */
export function isProfileStale(
  profiles: ExtractionProfileList | undefined,
  key: string | null | undefined,
): boolean {
  return !!profiles && !!key && !profiles.items.some((p) => p.key === key);
}

/**
 * 实体类型的中文短标签（图例/徽标/筛选用）：只查该空间生效配置的类型表（同一类型名在
 * 不同领域含义不同，如法律的 organization 含法院）；查不到（历史数据、配置已切换）
 * 时原样返回类型名。
 */
export function entityTypeLabel(profile: ExtractionProfile | undefined, type: string): string {
  const hit = profile?.entity_types.find((e) => e.type === type);
  return hit ? shortTypeLabel(hit.label, type) : type;
}

/**
 * 某空间的实体类型 → 短标签函数（图谱页用）。spaceProfile 为空间的 extraction_profile
 * （null = 跟随全局；空间详情未加载时传 undefined，按全局默认配置先行展示）。
 */
export function useEntityTypeLabel(
  client: ApiClient,
  spaceProfile: string | null | undefined,
): (type: string) => string {
  const profiles = useExtractionProfiles(client);
  const profile = resolveProfile(profiles.data, spaceProfile);
  return (type) => entityTypeLabel(profile, type);
}
