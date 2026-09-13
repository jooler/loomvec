import type { LucideIcon } from 'lucide-react';
import { Inbox } from 'lucide-react';
import { cn } from 'cn';

/** 列表/页面空状态。 */
export function EmptyState(props: {
  title?: string;
  description?: string;
  icon?: LucideIcon;
  action?: React.ReactNode;
  className?: string;
}) {
  const Icon = props.icon ?? Inbox;
  return (
    <div className={cn('flex flex-col items-center justify-center gap-2 py-10 text-center', props.className)}>
      <div className="flex size-10 items-center justify-center rounded-full bg-muted">
        <Icon className="size-5 text-muted-foreground" />
      </div>
      <p className="text-sm font-medium">{props.title ?? '暂无数据'}</p>
      {props.description && <p className="max-w-sm text-sm text-muted-foreground">{props.description}</p>}
      {props.action && <div className="mt-2">{props.action}</div>}
    </div>
  );
}
