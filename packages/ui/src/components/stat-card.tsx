import { Card, CardContent } from './ui/card';
import { cn } from 'cn';
import type { LucideIcon } from 'lucide-react';

/** 统计卡片（替代 antd Statistic）。 */
export function StatCard(props: {
  title: React.ReactNode;
  value: React.ReactNode;
  /** 数值下方的补充说明 */
  hint?: React.ReactNode;
  icon?: LucideIcon;
  /** 数值着色，如 text-amber-600 */
  valueClassName?: string;
  className?: string;
}) {
  const Icon = props.icon;
  return (
    <Card className={cn('py-4', props.className)}>
      <CardContent className="space-y-1 px-4">
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {Icon && <Icon className="size-3.5" />}
          {props.title}
        </p>
        <p className={cn('text-2xl font-semibold tabular-nums', props.valueClassName)}>
          {props.value}
        </p>
        {props.hint && <p className="text-xs text-muted-foreground">{props.hint}</p>}
      </CardContent>
    </Card>
  );
}
