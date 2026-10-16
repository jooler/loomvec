import { useTranslation } from 'react-i18next';
import type { ApiClient } from '@loomvec/sdk-ts';
import { isProfileStale, resolveProfile, useExtractionProfiles } from '../lib/extraction-profiles';
import { Label } from './ui/label';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './ui/select';

/** 下拉里「跟随全局」选项的值（Select 不接受空串，提交时换回 null）。 */
const FOLLOW_GLOBAL = '__global__';

/**
 * 空间「抽取领域配置」选择（web/ops 空间设置、admin 空间治理共用）：
 * null = 跟随全局默认；选项来自 GET /extraction-profiles（内置 + 自定义）。
 * 下方提示只对新入库或重新处理的文件生效，并展示所选配置的说明；
 * 已选配置失效（自定义配置被删）时仍显示该 key 并提示回落到的配置，用户可重选。
 */
export function ExtractionProfileField(props: {
  client: ApiClient;
  value: string | null;
  onChange: (value: string | null) => void;
  disabled?: boolean;
  id?: string;
  /** 提示「只对新入库或重新处理的文件生效」（创建空间时无存量文件，不显示） */
  showEffectHint?: boolean;
}) {
  const { t } = useTranslation('ui');
  const profiles = useExtractionProfiles(props.client);
  const items = profiles.data?.items ?? [];
  const defaultKey = profiles.data?.default;
  const labelOf = (key: string | null | undefined) =>
    items.find((p) => p.key === key)?.label ?? key ?? '';
  // 空间引用的配置已不在可选集合（如自定义配置被删除）时，生效的是回落后的全局默认配置
  const effective = resolveProfile(profiles.data, props.value);
  const stale = isProfileStale(profiles.data, props.value);
  const id = props.id ?? 'extraction-profile';

  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{t('extractionProfile.label')}</Label>
      <Select
        value={props.value ?? FOLLOW_GLOBAL}
        onValueChange={(v) => props.onChange(v === FOLLOW_GLOBAL ? null : v)}
        disabled={props.disabled || profiles.isLoading}
      >
        <SelectTrigger id={id} className="w-[240px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={FOLLOW_GLOBAL}>
            {t('extractionProfile.followGlobal', { label: labelOf(defaultKey) })}
          </SelectItem>
          {items.map((p) => (
            <SelectItem key={p.key} value={p.key}>
              {p.is_builtin ? p.label : t('extractionProfile.customLabel', { label: p.label })}
            </SelectItem>
          ))}
          {stale && (
            <SelectItem value={props.value!}>
              {t('extractionProfile.staleOption', { key: props.value })}
            </SelectItem>
          )}
        </SelectContent>
      </Select>
      {effective?.description && (
        <p className="text-xs text-muted-foreground">{effective.description}</p>
      )}
      {stale && (
        <p className="text-xs text-destructive">
          {t('extractionProfile.stale', { key: props.value, fallback: effective?.label ?? '' })}
        </p>
      )}
      {profiles.isError && (
        <p className="text-xs text-destructive">{(profiles.error as Error).message}</p>
      )}
      {props.showEffectHint !== false && (
        <p className="text-xs text-muted-foreground">{t('extractionProfile.effectHint')}</p>
      )}
    </div>
  );
}
