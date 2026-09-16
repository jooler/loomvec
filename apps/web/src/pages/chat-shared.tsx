/**
 * 对话页共享渲染助手（legacy 与 agent 两引擎共用，14 文档 §9.3）：
 * 助手消息 Markdown 渲染（Streamdown）、[n] 引用标记、locator 标签、引用跳转。
 */
import { ComponentProps, useMemo } from 'react';
import { useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Streamdown } from 'streamdown';
import { code } from '@streamdown/code';
import { cjk } from '@streamdown/cjk';
import { t as sharedT } from '@/i18n';
import type { ChatCitation } from '@/agent/agent';

/** [n] 引用标记在 Markdown 中的内部锚点前缀（经 components.a 覆写回可点击上标）。 */
const CITE_HREF_PREFIX = '#loomvec-cite-';

/** 把答案中的 [n] 引用标记转成锚点链接；代码块 / 行内代码保持原文不转换。 */
export function withCitationLinks(text: string, citations: ChatCitation[] | undefined): string {
  if (!citations?.length) return text;
  const indexes = new Set(citations.map((c) => c.index));
  // split 捕获组：偶数段为普通文本（需转换），奇数段为代码块 / 行内代码（保持原文）
  return text
    .split(/(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g)
    .map((part, i) =>
      i % 2 === 0
        ? part.replace(/\[(\d{1,3})\](?!\()/g, (raw, d: string) =>
            // Markdown 层需转义方括号：[\[1\]](锚点) → 渲染为链接文本 [1]
            indexes.has(Number(d)) ? `[\\[${d}\\]](${CITE_HREF_PREFIX}${d})` : raw,
          )
        : part,
    )
    .join('');
}

/** 生成覆写后的链接组件：内部锚点 → 可点击引用上标，其余链接原样透出。 */
function useCitationAnchor(citations: ChatCitation[] | undefined) {
  const navigate = useNavigate();
  return useMemo(() => {
    const Anchor = function CitationAnchor({
      // node 为 react-markdown 注入的 hast 节点，渲染无需使用，仅解构剥离
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      node: _node,
      children,
      href,
      ...props
    }: ComponentProps<'a'> & { node?: unknown }) {
      const cite =
        typeof href === 'string' && href.startsWith(CITE_HREF_PREFIX)
          ? citations?.find((c) => c.index === Number(href.slice(CITE_HREF_PREFIX.length)))
          : undefined;
      if (!cite) return <a href={href} {...props}>{children}</a>;
      return (
        <button
          type="button"
          className="mx-0.5 align-super text-xs font-semibold text-primary hover:underline"
          title={cite.text_snippet}
          onClick={(e) => {
            e.stopPropagation();
            jumpToCitation(navigate, cite);
          }}
        >
          {children}
        </button>
      );
    };
    return Anchor;
  }, [citations, navigate]);
}

export interface AnswerMarkdownProps {
  content: string;
  citations?: ChatCitation[];
  /** 流式中：启用逐词淡入动画 + 打字光标；完成后静态渲染。 */
  streaming?: boolean;
}

/**
 * 助手消息 Markdown 渲染（Streamdown，AI Elements Message 同款引擎）：
 * 流式时逐词淡入（cjk 插件保证中文分词粒度）+ 打字光标，代码块 Shiki 高亮，
 * 未闭合 Markdown 块按已流出的内容容错渲染。
 */
export function AnswerMarkdown({ content, citations, streaming }: AnswerMarkdownProps) {
  const CitationAnchor = useCitationAnchor(citations);
  const markdown = useMemo(() => withCitationLinks(content, citations), [content, citations]);
  const translations = useStreamdownTranslations();
  // 对象属性必须保持引用稳定：Streamdown 是 memo 组件，动画游标依赖
  // 跨渲染的插件实例状态，属性身份变化会打散 memo 并重置逐词动画时间线
  const animated = useMemo(() => ({ animation: 'fadeIn' as const }), []);
  const plugins = useMemo(() => ({ code, cjk }), []);
  const components = useMemo(() => ({ a: CitationAnchor }), [CitationAnchor]);
  return (
    <div className="min-w-0 break-words">
      <Streamdown
        animated={animated}
        isAnimating={!!streaming}
        caret="block"
        components={components}
        plugins={plugins}
        translations={translations}
      >
        {markdown}
      </Streamdown>
    </div>
  );
}

export function citationLabel(c: ChatCitation): string {
  const loc = c.locator ?? {};
  if (loc.pages?.length)
    return sharedT('ui:chunks.pageLocator', { pages: loc.pages.map((p) => p + 1).join(',') });
  if (loc.time_start !== undefined)
    return sharedT('agent:timeStart', {
      minutes: Math.floor(loc.time_start / 60),
      seconds: Math.round(loc.time_start % 60),
    });
  if (loc.start_line !== undefined)
    return sharedT('ui:chunks.lineLocator', { start: loc.start_line, end: loc.end_line ?? '' });
  return sharedT('agent:noLocator');
}

export function jumpToCitation(navigate: ReturnType<typeof useNavigate>, c: ChatCitation) {
  const loc = c.locator ?? {};
  const params = new URLSearchParams();
  if (loc.pages?.length) params.set('page', String(loc.pages[0] + 1));
  if (loc.time_start !== undefined) params.set('t', String(loc.time_start));
  const qs = params.toString();
  navigate(`/a/${c.asset_id}${qs ? `?${qs}` : ''}`);
}

/** Streamdown 内置控件文案（复制代码 / 表格导出等）的中文映射键。 */
export function useStreamdownTranslations(): ComponentProps<typeof Streamdown>['translations'] {
  const { t } = useTranslation('agent');
  return useMemo(
    () => ({
      close: t('sd.close'),
      copied: t('sd.copied'),
      copyCode: t('sd.copyCode'),
      copyLink: t('sd.copyLink'),
      copyTable: t('sd.copyTable'),
      copyTableAsCsv: t('sd.copyTableAsCsv'),
      copyTableAsMarkdown: t('sd.copyTableAsMarkdown'),
      copyTableAsTsv: t('sd.copyTableAsTsv'),
      downloadFile: t('sd.downloadFile'),
      downloadImage: t('sd.downloadImage'),
      downloadTable: t('sd.downloadTable'),
      exitFullscreen: t('sd.exitFullscreen'),
      viewFullscreen: t('sd.viewFullscreen'),
      zoomIn: t('sd.zoomIn'),
      zoomOut: t('sd.zoomOut'),
    }),
    [t],
  );
}
