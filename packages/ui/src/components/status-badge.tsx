import { Badge } from './ui/badge';
import { cn } from 'cn';

/**
 * 彩色状态徽标：antd Tag 色系 → Tailwind 色调的统一映射。
 * green=成功/正常 red=失败/危险 amber=待处理/警告 blue=进行中/信息 purple=特殊 gray=中性。
 */
export type BadgeTone = 'green' | 'red' | 'amber' | 'blue' | 'purple' | 'gray';

const TONE_CLS: Record<BadgeTone, string> = {
  green:
    'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-300',
  red: 'border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300',
  amber:
    'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300',
  blue: 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-300',
  purple:
    'border-purple-200 bg-purple-50 text-purple-700 dark:border-purple-900 dark:bg-purple-950 dark:text-purple-300',
  gray: '',
};

export function StatusBadge(props: {
  tone?: BadgeTone;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Badge variant="outline" className={cn(TONE_CLS[props.tone ?? 'gray'], props.className)}>
      {props.children}
    </Badge>
  );
}
