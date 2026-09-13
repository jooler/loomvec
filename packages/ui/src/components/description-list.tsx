import { cn } from 'cn';

/**
 * 详情页键值对列表（替代 antd Descriptions）。
 * 用法：
 *   <DescriptionList>
 *     <DescriptionItem label="名称">{t.name}</DescriptionItem>
 *     <DescriptionItem label="状态"><StatusBadge .../></DescriptionItem>
 *   </DescriptionList>
 */
export function DescriptionList(props: {
  children: React.ReactNode;
  /** 列数，默认 2 */
  cols?: 1 | 2 | 3;
  className?: string;
}) {
  const cols =
    props.cols === 1
      ? 'grid-cols-1'
      : props.cols === 3
        ? 'sm:grid-cols-2 lg:grid-cols-3'
        : 'sm:grid-cols-2';
  return (
    <dl className={cn('grid grid-cols-1 gap-x-8 gap-y-4', cols, props.className)}>
      {props.children}
    </dl>
  );
}

export function DescriptionItem(props: {
  label: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('min-w-0 space-y-1', props.className)}>
      <dt className="text-xs text-muted-foreground">{props.label}</dt>
      <dd className="text-sm break-words">
        {props.children ?? <span className="text-muted-foreground">-</span>}
      </dd>
    </div>
  );
}
