import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '@loomvec/ui/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@loomvec/ui/components/ui/select';
import { MultiSelect } from '@/components/multi-select';
import { ALL, EXT_OPTIONS, STATUS_TONE } from './constants';
import type { AssetFilters } from './use-assets-list';

/** 资产多维筛选条（视图切换在 Finder 工具栏，此处仅筛选）。 */
export function AssetsFiltersBar(props: {
  spaceId?: string;
  filters: AssetFilters;
  onFilterChange: (patch: Partial<AssetFilters>) => void;
  tags: { id: string; name: string }[];
  tagsLoading: boolean;
  categories: { id: string; name: string }[];
  onRefresh: () => void;
}) {
  const { t } = useTranslation('assets');
  const { filters, onFilterChange } = props;
  // 审核状态文案复用 ui 命名空间 reviewStatus.*；资产状态复用 assetStatus.*
  const reviewOptions = (['pending_review', 'approved', 'rejected'] as const).map((v) => ({
    value: v,
    label: t(`reviewStatus.${v}`, { defaultValue: v }),
  }));
  const statusOptions = Object.entries(STATUS_TONE).map(([v]) => ({
    value: v,
    label: t(`assetStatus.${v}`, { defaultValue: v }),
  }));

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        value={filters.ext ?? ALL}
        onValueChange={(v) => onFilterChange({ ext: v === ALL ? undefined : v })}
      >
        <SelectTrigger className="w-[110px]">
          <SelectValue placeholder={t('typePlaceholder')} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>{t('action.all')}</SelectItem>
          {EXT_OPTIONS.map(([v, label]) => (
            <SelectItem key={v} value={v}>
              {label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={filters.status ?? ALL}
        onValueChange={(v) => onFilterChange({ status: v === ALL ? undefined : v })}
      >
        <SelectTrigger className="w-[110px]">
          <SelectValue placeholder={t('field.status')} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>{t('action.all')}</SelectItem>
          {statusOptions.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Select
        value={filters.review_status ?? ALL}
        onValueChange={(v) => onFilterChange({ review_status: v === ALL ? undefined : v })}
      >
        <SelectTrigger className="w-[120px]">
          <SelectValue placeholder={t('reviewStatusPlaceholder')} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>{t('action.all')}</SelectItem>
          {reviewOptions.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {props.spaceId && (
        <MultiSelect
          className="min-w-40"
          placeholder={t('tagPlaceholder')}
          loading={props.tagsLoading}
          value={filters.tagIds}
          options={props.tags.map((tag) => ({ value: tag.id, label: tag.name }))}
          onChange={(v) => onFilterChange({ tagIds: v })}
        />
      )}
      {props.spaceId && (
        <Select
          value={filters.categoryId ?? ALL}
          onValueChange={(v) => onFilterChange({ categoryId: v === ALL ? undefined : v })}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder={t('categoryPlaceholder')} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>{t('action.all')}</SelectItem>
            {props.categories.map((c) => (
              <SelectItem key={c.id} value={c.id}>
                {c.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Button variant="outline" onClick={props.onRefresh}>
        <RefreshCw /> {t('action.refresh')}
      </Button>
    </div>
  );
}
