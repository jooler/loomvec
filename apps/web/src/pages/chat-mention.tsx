import { useEffect, useState } from 'react';
import { useQueries } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { cn } from 'cn';
import { FileText } from 'lucide-react';
import { api } from '@loomvec/sdk-ts';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { extractApiError } from '@/utils';
import { t as translate } from '@/i18n';

/**
 * 对话输入框 @ 文献：在检索范围内的空间里按名称搜索资产，选中后作为 chip 随提问提交，
 * 服务端把该轮检索限定在这些文献内。
 */

/** 单条提问最多 @ 的文献数（对齐服务端 MAX_MENTIONS）。 */
export const MAX_MENTIONS = 10;
/** 每个空间的候选条数上限。 */
const CANDIDATES_PER_SPACE = 8;
const SEARCH_DEBOUNCE_MS = 200;

export interface MentionDoc {
  asset_id: string;
  name: string;
  /** 所在空间；检索范围变化时据此剔除已不在范围内的 chip。 */
  space_id?: string;
}

export interface MentionCandidate extends MentionDoc {
  space_id: string;
  hint: string;
}

/** 光标前的 `@关键词`：`@` 须在行首或空白之后，关键词不含空白。 */
export function findMentionTrigger(
  text: string,
  caret: number,
): { start: number; query: string } | null {
  const m = /(^|\s)@([^\s@]*)$/.exec(text.slice(0, caret));
  if (!m) return null;
  return { start: caret - m[2].length - 1, query: m[2] };
}

/** 选中文献后移除输入框里的 `@关键词` 触发文本。 */
export function removeMentionTrigger(text: string, start: number, caret: number): string {
  return text.slice(0, start) + text.slice(caret);
}

function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

/** 检索范围内各空间按名称搜索资产（仅解析完成的），合并去重。 */
export function useMentionCandidates(spaceIds: string[], keyword: string, enabled: boolean) {
  const q = useDebounced(keyword, SEARCH_DEBOUNCE_MS);
  const results = useQueries({
    queries: spaceIds.map((spaceId) => ({
      queryKey: ['mention-candidates', spaceId, q],
      enabled,
      queryFn: async () => {
        const { data, error } = await api.GET('/api/v1/assets', {
          params: {
            query: {
              space_id: spaceId,
              q: q || undefined,
              status: 'ready',
              limit: CANDIDATES_PER_SPACE,
            },
          },
        });
        if (error) throw new Error(extractApiError(error, translate('agent:mention.loadFailed')));
        return data.items;
      },
    })),
  });
  const items: MentionCandidate[] = [];
  const seen = new Set<string>();
  for (const r of results) {
    for (const a of r.data ?? []) {
      if (seen.has(a.id) || !a.space_id) continue;
      seen.add(a.id);
      const hint = [a.paper?.first_author, a.paper?.year, a.paper?.journal]
        .filter(Boolean)
        .join(' · ');
      items.push({ asset_id: a.id, name: a.name, space_id: a.space_id, hint });
    }
  }
  return {
    items,
    // 关键词还在防抖中也算加载，避免先闪一下「无匹配」
    loading: enabled && (q !== keyword || results.some((r) => r.isLoading)),
  };
}

/** @ 候选浮层（向上弹出；mousedown 不抢输入框焦点）。 */
export function MentionPicker(props: {
  items: MentionCandidate[];
  loading: boolean;
  activeIndex: number;
  hasScope: boolean;
  onPick: (item: MentionCandidate) => void;
  onHover: (index: number) => void;
}) {
  const { t } = useTranslation('agent');
  return (
    <div
      role="listbox"
      className="absolute inset-x-0 bottom-full z-20 mb-1 max-h-64 overflow-y-auto rounded-md border bg-popover p-1 text-popover-foreground shadow-md"
    >
      {!props.hasScope ? (
        <p className="px-2 py-1.5 text-sm text-muted-foreground">{t('mention.noScope')}</p>
      ) : props.items.length === 0 ? (
        <p className="flex items-center gap-2 px-2 py-1.5 text-sm text-muted-foreground">
          {props.loading ? <Spinner className="size-3" /> : null}
          {props.loading ? t('mention.loading') : t('mention.empty')}
        </p>
      ) : (
        props.items.map((item, i) => (
          <button
            key={item.asset_id}
            type="button"
            role="option"
            aria-selected={i === props.activeIndex}
            className={cn(
              'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm',
              i === props.activeIndex && 'bg-accent text-accent-foreground',
            )}
            onMouseDown={(e) => e.preventDefault()}
            onMouseEnter={() => props.onHover(i)}
            onClick={() => props.onPick(item)}
          >
            <FileText className="size-4 shrink-0 text-muted-foreground" />
            <span className="min-w-0 flex-1 truncate">{item.name}</span>
            {item.hint && (
              <span className="max-w-48 shrink-0 truncate text-xs text-muted-foreground">
                {item.hint}
              </span>
            )}
          </button>
        ))
      )}
    </div>
  );
}
