import { Image as ImageIcon, LayoutGrid, RefreshCw } from 'lucide-react';
import { Button } from '@loomvec/ui/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@loomvec/ui/components/ui/select';
import { Tabs, TabsList, TabsTrigger } from '@loomvec/ui/components/ui/tabs';
import { MultiSelect } from '@/components/multi-select';
import { REVIEW_STATUS_META } from '@/utils';
import { ALL, EXT_OPTIONS, STATUS_TONE } from './constants';
import type { AssetFilters } from './use-assets-list';

/** 资产多维筛选条 + 视图切换（列表/缩略图）。 */
export function AssetsFiltersBar(props: {
  spaceId?: string;
  filters: AssetFilters;
  onFilterChange: (patch: Partial<AssetFilters>) => void;
  view: 'list' | 'grid';
  onViewChange: (view: 'list' | 'grid') => void;
  tags: { id: string; name: string }[];
  tagsLoading: boolean;
  categories: { id: string; name: string }[];
  onRefresh: () => void;
}) {
  const { filters, onFilterChange } = props;
  const reviewOptions = Object.entries(REVIEW_STATUS_META).map(([v, m]) => ({ value: v, label: m.text }));
  const statusOptions = Object.entries(STATUS_TONE).map(([v, s]) => ({ value: v, label: s.text }));

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Select
        value={filters.ext ?? ALL}
        onValueChange={(v) => onFilterChange({ ext: v === ALL ? undefined : v })}
      >
        <SelectTrigger className="w-[110px]">
          <SelectValue placeholder="类型" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>全部</SelectItem>
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
          <SelectValue placeholder="状态" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>全部</SelectItem>
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
          <SelectValue placeholder="审核状态" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>全部</SelectItem>
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
          placeholder="标签"
          loading={props.tagsLoading}
          value={filters.tagIds}
          options={props.tags.map((t) => ({ value: t.id, label: t.name }))}
          onChange={(v) => onFilterChange({ tagIds: v })}
        />
      )}
      {props.spaceId && (
        <Select
          value={filters.categoryId ?? ALL}
          onValueChange={(v) => onFilterChange({ categoryId: v === ALL ? undefined : v })}
        >
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="分类" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>全部</SelectItem>
            {props.categories.map((c) => (
              <SelectItem key={c.id} value={c.id}>
                {c.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <Tabs value={props.view} onValueChange={(v) => props.onViewChange(v as 'list' | 'grid')}>
        <TabsList>
          <TabsTrigger value="list">
            <LayoutGrid className="size-4" /> 列表
          </TabsTrigger>
          <TabsTrigger value="grid">
            <ImageIcon className="size-4" /> 缩略图
          </TabsTrigger>
        </TabsList>
      </Tabs>
      <Button variant="outline" onClick={props.onRefresh}>
        <RefreshCw /> 刷新
      </Button>
    </div>
  );
}
