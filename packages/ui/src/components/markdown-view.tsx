import { memo } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

import { cn } from 'cn';

/**
 * 只读 Markdown 渲染（MinerU 解析产物预览）。
 * - GFM（表格/删除线/任务列表）+ 数学公式（$...$ / $$...$$，KaTeX）；
 * - MinerU 产物中的图片为相对路径（图片不落对象存储），渲染为占位徽标；
 * - 链接一律新开页（只读场景不做站内路由跳转）。
 */
export const MarkdownView = memo(function MarkdownView({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        'markdown-view text-sm leading-relaxed [&_.katex-display]:overflow-x-auto',
        className,
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={{
          img: ({ alt }) => (
            <span className="mx-1 inline-flex items-center gap-1 rounded border border-dashed px-1.5 py-0.5 align-middle text-xs text-muted-foreground">
              🖼 {alt || t('markdown.imageFallback')}
            </span>
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline underline-offset-2"
            >
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className="my-2 overflow-x-auto">
              <table className="w-full border-collapse text-xs [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:bg-muted [&_th]:px-2 [&_th]:py-1">
                {children}
              </table>
            </div>
          ),
          code: ({ className: cls, children }) =>
            cls?.startsWith('language-') ? (
              <code className="block overflow-x-auto rounded bg-muted p-2 font-mono text-xs">
                {children}
              </code>
            ) : (
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{children}</code>
            ),
          h1: ({ children }) => <h1 className="mb-2 mt-4 text-lg font-semibold">{children}</h1>,
          h2: ({ children }) => <h2 className="mb-2 mt-4 text-base font-semibold">{children}</h2>,
          h3: ({ children }) => (
            <h3 className="mb-1.5 mt-3 text-sm font-semibold">{children}</h3>
          ),
          p: ({ children }) => <p className="my-2">{children}</p>,
          ul: ({ children }) => <ul className="my-2 list-disc pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="my-2 list-decimal pl-5">{children}</ol>,
          blockquote: ({ children }) => (
            <blockquote className="my-2 border-l-2 pl-3 text-muted-foreground">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-3 border-border" />,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
