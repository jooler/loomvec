import { cn } from 'cn';
import { X } from 'lucide-react';

/** 处理步骤时间线（替代 antd Steps）：parse → chunk → embed → index。 */
export function StepTimeline(props: {
  steps: { key: string; label: string; state: 'wait' | 'process' | 'finish' | 'error'; description?: string }[];
}) {
  return (
    <ol className="flex">
      {props.steps.map((s, i) => (
        <li key={s.key} className="min-w-0 flex-1">
          <div className="flex items-center">
            <span
              className={cn(
                'flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-medium',
                s.state === 'finish' && 'bg-primary text-primary-foreground',
                s.state === 'process' && 'border border-primary text-primary',
                s.state === 'error' && 'bg-destructive text-white',
                s.state === 'wait' && 'border bg-muted text-muted-foreground',
              )}
            >
              {s.state === 'error' ? <X className="size-3.5" /> : i + 1}
            </span>
            {i < props.steps.length - 1 && (
              <span
                className={cn(
                  'h-px flex-1',
                  s.state === 'finish'
                    ? 'bg-primary'
                    : s.state === 'error'
                      ? 'bg-destructive'
                      : 'bg-border',
                )}
              />
            )}
          </div>
          <p className={cn('mt-1.5 text-sm', s.state === 'wait' ? 'text-muted-foreground' : 'font-medium')}>
            {s.label}
          </p>
          {s.description && (
            <p className="truncate text-xs text-muted-foreground" title={s.description}>
              {s.description}
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}
