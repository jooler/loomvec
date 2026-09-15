import { lazy, useEffect, useRef, useState } from 'react';
import { FileQuestion } from 'lucide-react';
import { useTheme } from 'next-themes';
import { useTranslation } from 'react-i18next';

import { cn } from 'cn';
import { t } from '../i18n';
import { Spinner } from './ui/spinner';
import { Button } from './ui/button';

/** embedpdf 体积较大且自带 UI，懒加载避免进入首屏包。 */
const LazyPdfViewer = lazy(() => import('@embedpdf/react-pdf-viewer'));

export type OriginalViewerKind =
  | 'pdf'
  | 'docx'
  | 'sheet'
  | 'image'
  | 'text'
  | 'media'
  | 'unsupported';

const TEXT_EXTS = new Set(['.txt', '.md', '.markdown', '.csv', '.json', '.log', '.xml', '.yaml', '.yml']);

/** 按扩展名/MIME 分派查看器（MinerU 支持的解析类格式优先走专用预览）。 */
export function resolveViewerKind(
  mime_type?: string | null,
  ext?: string | null,
): OriginalViewerKind {
  const e = (ext ?? '').toLowerCase();
  const m = (mime_type ?? '').toLowerCase();
  if (m === 'application/pdf' || e === '.pdf') return 'pdf';
  if (
    m === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ||
    e === '.docx'
  )
    return 'docx';
  if (
    m === 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' ||
    e === '.xlsx' ||
    e === '.xls'
  )
    return 'sheet';
  if (m.startsWith('image/')) return 'image';
  if (m.startsWith('video/') || m.startsWith('audio/')) return 'media';
  if (m.startsWith('text/') || TEXT_EXTS.has(e)) return 'text';
  return 'unsupported';
}

function ViewerLoading() {
  return (
    <div className="grid h-full place-items-center">
      <Spinner className="size-5 text-muted-foreground" />
    </div>
  );
}

/** embedpdf 就绪后跳转指定页（?page=N 深链；文档加载存在窗口期，带退避重试）。 */
function usePdfPageJump(
  registryRef: React.RefObject<{ getPlugin: (id: string) => unknown } | null>,
  page?: number,
) {
  useEffect(() => {
    if (!page || page < 1) return;
    let cancelled = false;
    const attempt = async (tries: number): Promise<void> => {
      if (cancelled || tries <= 0) return;
      const registry = registryRef.current;
      const plugin = registry?.getPlugin('scroll') as
        | { provides?: () => { scrollToPage?: (o: { pageNumber: number }) => void } }
        | undefined;
      try {
        plugin?.provides?.()?.scrollToPage?.({ pageNumber: page });
        return;
      } catch {
        // 文档未就绪：退避后重试
      }
      await new Promise((r) => setTimeout(r, 800));
      await attempt(tries - 1);
    };
    void attempt(12);
    return () => {
      cancelled = true;
    };
  }, [page, registryRef]);
}

/** 应用主题（next-themes）→ embedpdf 主题偏好；'system' 透传给查看器自行跟随系统。 */
function usePdfThemePreference(): 'light' | 'dark' | 'system' {
  const { theme, resolvedTheme } = useTheme();
  if (theme === 'system' || resolvedTheme === undefined) return 'system';
  return resolvedTheme === 'dark' ? 'dark' : 'light';
}

function PdfPane({ url, page }: { url: string; page?: number }) {
  const registryRef = useRef<{ getPlugin: (id: string) => unknown } | null>(null);
  // onInit 的 container 类型（避免为类型新增 @embedpdf/snippet 直接依赖）
  const containerRef = useRef<{
    setTheme: (theme: 'light' | 'dark' | 'system') => void;
  } | null>(null);
  const preference = usePdfThemePreference();
  usePdfPageJump(registryRef, page);

  // 应用内切换明暗主题时，运行时同步给已挂载的查看器（无需重载文档）
  useEffect(() => {
    containerRef.current?.setTheme(preference);
  }, [preference]);

  return (
    <LazyPdfViewer
      config={{
        src: url,
        // 自托管：不请求外部字体 CDN（缺失字形回退系统字体栈）
        fontFallback: null,
        // 明暗风格与应用一致（默认主题偏好也随 next-themes）
        theme: { preference },
        // 界面语言简体中文（i18n 插件默认注册全部内置语言，含 zh-CN）
        i18n: { defaultLocale: 'zh-CN', fallbackLocale: 'en' },
        // 只读查看器：分类禁用同时作用于 UI（工具栏/菜单/选中菜单）与命令系统——
        // annotation/redaction = 批注/形状/墨迹/图章/签名/密文等编辑工具；
        // insert/form = 顶栏「插入」「表单」下拉；panel-comment / panel-annotation-style
        // = 「评论」侧栏与标注样式面板
        disabledCategories: [
          'annotation',
          'redaction',
          'insert',
          'form',
          'panel-comment',
          'panel-annotation-style',
        ],
        // 权限位兜底（第二道闸）：无论 PDF 自身如何声明，一律禁止改内容/改标注/
        // 填表单/重组文档——annotation 插件的 create/update/delete 均检查
        // ModifyAnnotations，表单交互检查 FillForms
        permissions: {
          enforceDocumentPermissions: true,
          overrides: {
            modifyContents: false,
            modifyAnnotations: false,
            fillForms: false,
            assembleDocument: false,
          },
        },
      }}
      className="h-full w-full"
      onInit={(container) => {
        containerRef.current = container as unknown as {
          setTheme: (theme: 'light' | 'dark' | 'system') => void;
        };
      }}
      onReady={(registry) => {
        registryRef.current = registry as unknown as { getPlugin: (id: string) => unknown };
      }}
    />
  );
}

/** docx 原文预览：docx-preview 在容器内还原版式（纯前端，样式由库注入）。 */
function DocxPane({ url }: { url: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [{ renderAsync }, res] = await Promise.all([
          import('docx-preview'),
          fetch(url),
        ]);
        if (!res.ok) throw new Error(t('originalViewer.loadFailedHttp', { status: res.status }));
        const blob = await res.blob();
        if (cancelled || !containerRef.current) return;
        containerRef.current.innerHTML = '';
        await renderAsync(blob, containerRef.current, undefined, { inWrapper: true });
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : t('originalViewer.loadFailed'));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [url]);
  if (error) return <ViewerError message={error} url={url} />;
  return (
    <div className="h-full overflow-auto bg-muted/40 p-4">
      <div
        ref={containerRef}
        className="mx-auto min-h-full bg-background shadow-sm [&_*]:max-w-full"
      />
    </div>
  );
}

/** xlsx/xls 原文预览：SheetJS 解析后逐表渲染只读 HTML 表格。 */
function SheetPane({ url }: { url: string }) {
  const [sheets, setSheets] = useState<{ name: string; html: string }[]>([]);
  const [active, setActive] = useState(0);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [XLSX, res] = await Promise.all([import('xlsx'), fetch(url)]);
        if (!res.ok) throw new Error(t('originalViewer.loadFailedHttp', { status: res.status }));
        const workbook = XLSX.read(await res.arrayBuffer(), { type: 'array' });
        if (cancelled) return;
        const parsed = workbook.SheetNames.map((name) => {
          const doc = new DOMParser().parseFromString(
            XLSX.utils.sheet_to_html(workbook.Sheets[name]),
            'text/html',
          );
          return { name, html: doc.body.innerHTML };
        });
        setSheets(parsed);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : t('originalViewer.loadFailed'));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [url]);
  if (error) return <ViewerError message={error} url={url} />;
  if (sheets.length === 0) return <ViewerLoading />;
  return (
    <div className="flex h-full flex-col">
      {sheets.length > 1 && (
        <div className="flex flex-wrap gap-1 border-b px-2 py-1.5">
          {sheets.map((s, i) => (
            <Button
              key={s.name}
              size="xs"
              variant={i === active ? 'default' : 'ghost'}
              onClick={() => setActive(i)}
            >
              {s.name}
            </Button>
          ))}
        </div>
      )}
      <div className="flex-1 overflow-auto p-2">
        <div
          className="[&_td]:whitespace-nowrap [&_td]:border [&_td]:px-2 [&_td]:py-0.5 [&_table]:border-collapse"
          dangerouslySetInnerHTML={{ __html: sheets[active]?.html ?? '' }}
        />
      </div>
    </div>
  );
}

function TextPane({ url }: { url: string }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(t('originalViewer.loadFailedHttp', { status: r.status }));
        return r.text();
      })
      .then((content) => {
        if (!cancelled) setText(content);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : t('originalViewer.loadFailed'));
      });
    return () => {
      cancelled = true;
    };
  }, [url]);
  if (error) return <ViewerError message={error} url={url} />;
  if (text === null) return <ViewerLoading />;
  return (
    <div className="h-full overflow-auto">
      <pre className="whitespace-pre-wrap p-4 text-sm">{text}</pre>
    </div>
  );
}

function ViewerError({ message, url }: { message: string; url: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
      <FileQuestion className="size-8" />
      <p>{message}</p>
      <Button variant="outline" size="sm" asChild>
        <a href={url} target="_blank" rel="noreferrer">
          {t('originalViewer.openInNewWindow')}
        </a>
      </Button>
    </div>
  );
}

/**
 * 原始文件只读查看器：PDF（embedpdf）/ DOCX（docx-preview）/ XLSX（SheetJS）/
 * 图片 / 文本 / 音视频，其余格式给下载引导。容器需给定高度（h-full 依赖父级）。
 */
export function OriginalFileViewer({
  url,
  mime_type,
  ext,
  page,
  className,
}: {
  url?: string | null;
  mime_type?: string | null;
  ext?: string | null;
  page?: number;
  className?: string;
}) {
  const kind = resolveViewerKind(mime_type, ext);
  const { t } = useTranslation();
  const cls = cn('h-full w-full overflow-hidden rounded-md border bg-background', className);
  if (!url)
    return (
      <div className={cn(cls, 'grid place-items-center text-sm text-muted-foreground')}>
        {t('originalViewer.noOriginalFile')}
      </div>
    );
  if (kind === 'unsupported')
    return (
      <div className={cn(cls, 'flex flex-col items-center justify-center gap-2')}>
        <FileQuestion className="size-8 text-muted-foreground" />
        <p className="text-sm text-muted-foreground">{t('originalViewer.unsupportedFormat')}</p>
        <Button variant="outline" size="sm" asChild>
          <a href={url} target="_blank" rel="noreferrer">
            {t('originalViewer.openInNewWindow')}
          </a>
        </Button>
      </div>
    );
  return (
    <div className={cls}>
      {kind === 'pdf' && <PdfPane url={url} page={page} />}
      {kind === 'docx' && <DocxPane url={url} />}
      {kind === 'sheet' && <SheetPane url={url} />}
      {kind === 'image' && (
        <div className="grid h-full place-items-center overflow-auto p-2">
          <img src={url} alt={t('originalViewer.imageAlt')} className="max-h-full max-w-full object-contain" />
        </div>
      )}
      {kind === 'text' && <TextPane url={url} />}
      {kind === 'media' &&
        ((mime_type ?? '').startsWith('audio/') ? (
          <div className="grid h-full place-items-center p-4">
            <audio src={url} controls className="w-full max-w-md" />
          </div>
        ) : (
          <div className="grid h-full place-items-center p-2">
            <video src={url} controls className="max-h-full max-w-full" />
          </div>
        ))}
    </div>
  );
}
