import { ChevronDown } from 'lucide-react';
import { cn } from 'cn';
import { Badge } from '@loomvec/ui/components/ui/badge';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@loomvec/ui/components/ui/dropdown-menu';

/**
 * 多选筛选（替代 antd Select mode="multiple"，含 maxTagCount 语义）：
 * 下拉勾选选项，触发器内展示已选徽标（最多 2 个 + 溢出数量）。
 */
export function MultiSelect(props: {
  value: string[];
  onChange: (value: string[]) => void;
  options: { value: string; label: string }[];
  placeholder: string;
  loading?: boolean;
  className?: string;
}) {
  const labelOf = (v: string) => props.options.find((o) => o.value === v)?.label ?? v;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={props.loading}>
        <button
          type="button"
          className={cn(
            'flex h-9 min-w-36 max-w-xs items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 py-2 text-sm whitespace-nowrap shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30',
            props.className,
          )}
        >
          <span className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
            {props.value.length === 0 ? (
              <span className="truncate text-muted-foreground">{props.placeholder}</span>
            ) : (
              <>
                {props.value.slice(0, 2).map((v) => (
                  <Badge key={v} variant="secondary" className="max-w-24 truncate font-normal">
                    {labelOf(v)}
                  </Badge>
                ))}
                {props.value.length > 2 && (
                  <span className="text-xs text-muted-foreground">+{props.value.length - 2}</span>
                )}
              </>
            )}
          </span>
          <ChevronDown className="size-4 shrink-0 opacity-50" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-72 w-56 overflow-y-auto">
        {props.loading ? (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">加载中…</p>
        ) : props.options.length === 0 ? (
          <p className="px-2 py-1.5 text-sm text-muted-foreground">暂无选项</p>
        ) : (
          props.options.map((o) => (
            <DropdownMenuCheckboxItem
              key={o.value}
              checked={props.value.includes(o.value)}
              onCheckedChange={(checked) =>
                props.onChange(
                  checked
                    ? [...props.value, o.value]
                    : props.value.filter((v) => v !== o.value),
                )
              }
              onSelect={(e) => e.preventDefault()}
            >
              {o.label}
            </DropdownMenuCheckboxItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
