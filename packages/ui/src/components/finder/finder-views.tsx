/**
 * Finder 三视图（纯展示 + 交互回调）：list（列表）/ gallery（图标预览）/
 * columns（macOS Finder 分栏）。右键菜单、拖拽、重命名由 finder.tsx 注入。
 */
import { useEffect, useRef } from 'react';
import {
  ChevronRight,
  FileAudio,
  FileImage,
  FileText,
  FileVideo,
  File as FileIcon,
  Folder,
} from 'lucide-react';
import { cn } from 'cn';
import { useTranslation } from 'react-i18next';
import { Input } from '../ui/input';
import { Spinner } from '../ui/spinner';
import { StatusBadge } from '../status-badge';
import { ReviewStatusTag } from '../review-status-tag';
import { ContextMenu, ContextMenuTrigger } from '../ui/context-menu';
import { formatBytes } from '../../lib/format';
import { FINDER_DRAG_MIME } from './types';
import type { FinderAsset, FinderFolder } from './types';

/** 资产状态 → 徽标色调（Finder 视图与两端资产页共用口径；文案见 ui:assetStatus.*）。 */
export const STATUS_TONE: Record<string, { tone: 'gray' | 'blue' | 'green' | 'red' }> = {
  pending: { tone: 'gray' },
  processing: { tone: 'blue' },
  ready: { tone: 'green' },
  failed: { tone: 'red' },
};

/** 视图公共 props（由 Finder 主容器注入）。 */
export interface FinderViewProps {
  folders: FinderFolder[];
  assets: FinderAsset[];
  selectedFolders: Set<string>;
  selectedAssets: Set<string>;
  renaming: { kind: 'folder' | 'asset'; id: string; name: string } | null;
  canWrite: boolean;
  thumbFor: (asset: FinderAsset) => string | null;
  /** 条目右键菜单内容（ContextMenu Content 片段）。 */
  menuFor: (kind: 'folder' | 'asset', id: string) => React.ReactNode;
  /**
   * 选中条目。
   * - 普通点击：单选
   * - Ctrl/⌘：加减选
   * - Shift：从锚点到当前项的连续选（orderedItems 决定顺序）
   */
  onSelect: (
    kind: 'folder' | 'asset',
    id: string,
    mods: { additive: boolean; range: boolean },
    orderedItems?: Array<{ kind: 'folder' | 'asset'; id: string }>,
  ) => void;
  onOpen: (kind: 'folder' | 'asset', id: string) => void;
  onRenameSubmit: (name: string) => void;
  onRenameCancel: () => void;
  onDragStartItem: (e: React.DragEvent, kind: 'folder' | 'asset', id: string) => void;
  onDropOnFolder: (e: React.DragEvent, folderId: string | null) => void;
}

export function FileGlyph({
  asset,
  className,
  strokeWidth,
}: {
  asset: FinderAsset;
  className?: string;
  strokeWidth?: number;
}) {
  const mime = asset.mime_type ?? '';
  if (mime.startsWith('image/')) return <FileImage className={className} strokeWidth={strokeWidth} />;
  if (mime.startsWith('video/')) return <FileVideo className={className} strokeWidth={strokeWidth} />;
  if (mime.startsWith('audio/')) return <FileAudio className={className} strokeWidth={strokeWidth} />;
  if (['.pdf', '.docx', '.pptx', '.xlsx', '.txt', '.md', '.markdown'].includes(asset.ext))
    return <FileText className={className} strokeWidth={strokeWidth} />;
  return <FileIcon className={className} strokeWidth={strokeWidth} />;
}

function StatusBits({ asset, compact }: { asset: FinderAsset; compact?: boolean }) {
  const { t } = useTranslation();
  const meta = STATUS_TONE[asset.status];
  return (
    <span className={cn('flex items-center gap-1', compact && 'flex-wrap')}>
      <StatusBadge tone={meta?.tone}>
        {t(`assetStatus.${asset.status}`, { defaultValue: asset.status })}
      </StatusBadge>
      {!compact && asset.review_status && <ReviewStatusTag status={asset.review_status} />}
    </span>
  );
}

/** 作者列展示：≤2 全列；更多则首两位 + et al.；兼容仅有 first_author 的旧数据。 */
function formatAuthors(asset: FinderAsset): string {
  const paper = asset.paper;
  if (!paper) return '';
  const authors =
    paper.authors && paper.authors.length > 0
      ? paper.authors
      : paper.first_author
        ? [paper.first_author]
        : [];
  if (authors.length === 0) return '';
  if (authors.length <= 2) return authors.join('; ');
  return `${authors[0]}; ${authors[1]} et al.`;
}

/** 行内重命名输入框（回车提交 / Esc 取消 / 失焦提交）。 */
function RenameInput({
  defaultName,
  onSubmit,
  onCancel,
}: {
  defaultName: string;
  onSubmit: (name: string) => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);
  return (
    <Input
      ref={ref}
      defaultValue={defaultName}
      className="h-6 max-w-full px-1.5 py-0 text-[13px]"
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === 'Enter') onSubmit(ref.current?.value.trim() ?? '');
        if (e.key === 'Escape') onCancel();
      }}
      onBlur={() => onSubmit(ref.current?.value.trim() ?? '')}
    />
  );
}

type FinderItemRef = { kind: 'folder' | 'asset'; id: string };

/** 当前视图/列的可见顺序（文件夹在前，资产在后）。 */
export function finderOrderedItems(
  folders: FinderFolder[],
  assets: FinderAsset[],
): FinderItemRef[] {
  return [
    ...folders.map((f) => ({ kind: 'folder' as const, id: f.id })),
    ...assets.map((a) => ({ kind: 'asset' as const, id: a.id })),
  ];
}

/** 条目外壳：右键菜单 + 拖拽源 + 拖拽目标（文件夹）+ 选中/打开交互。 */
function itemHandlers(
  props: FinderViewProps,
  kind: 'folder' | 'asset',
  id: string,
  orderedItems?: FinderItemRef[],
) {
  return {
    draggable: !(props.renaming?.kind === kind && props.renaming.id === id),
    onDragStart: (e: React.DragEvent) => props.onDragStartItem(e, kind, id),
    onClick: (e: React.MouseEvent) => {
      e.stopPropagation();
      props.onSelect(
        kind,
        id,
        { additive: e.metaKey || e.ctrlKey, range: e.shiftKey },
        orderedItems,
      );
    },
    onDoubleClick: (e: React.MouseEvent) => {
      e.stopPropagation();
      props.onOpen(kind, id);
    },
    ...(kind === 'folder'
      ? {
          onDragOver: (e: React.DragEvent) => {
            if (e.dataTransfer.types.includes(FINDER_DRAG_MIME)) {
              e.preventDefault();
              e.dataTransfer.dropEffect = 'move';
            }
          },
          onDrop: (e: React.DragEvent) => props.onDropOnFolder(e, id),
        }
      : {}),
  };
}

/** 拖拽目标属性（容器空白区 = 当前文件夹）。 */
function dropZone(onDrop: (e: React.DragEvent) => void) {
  return {
    onDragOver: (e: React.DragEvent) => {
      if (e.dataTransfer.types.includes(FINDER_DRAG_MIME)) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
      }
    },
    onDrop,
  };
}

/** 条目右键菜单外壳：Root/Trigger(asChild) 不产生 DOM，table 行内也合法。 */
function WithMenu({
  menu,
  children,
}: {
  menu: React.ReactNode;
  children: React.ReactElement;
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      {menu}
    </ContextMenu>
  );
}

// ---------------------------------------------------------------------------
// 列表视图
// ---------------------------------------------------------------------------

export function FinderListView(props: FinderViewProps) {
  const { t } = useTranslation();
  const ordered = finderOrderedItems(props.folders, props.assets);
  const rows: React.ReactNode[] = [];
  for (const f of props.folders) {
    const selected = props.selectedFolders.has(f.id);
    const isRenaming = props.renaming?.kind === 'folder' && props.renaming.id === f.id;
    rows.push(
      <WithMenu key={f.id} menu={props.menuFor('folder', f.id)}>
        <tr
          className={cn(
            'cursor-default border-b border-border/40 select-none',
            selected ? 'bg-accent text-accent-foreground' : 'hover:bg-muted/60',
          )}
          {...itemHandlers(props, 'folder', f.id, ordered)}
        >
          <td className="max-w-0 px-3 py-1.5">
            <span className="flex items-center gap-2">
              <Folder className="size-4 shrink-0 text-muted-foreground" />
              {isRenaming ? (
                <RenameInput
                  defaultName={f.name}
                  onSubmit={props.onRenameSubmit}
                  onCancel={props.onRenameCancel}
                />
              ) : (
                <span className="truncate">{f.name}</span>
              )}
            </span>
          </td>
          <td className="px-3 py-1.5 text-muted-foreground">--</td>
          <td className="px-3 py-1.5 text-muted-foreground">--</td>
          <td className="px-3 py-1.5 text-muted-foreground">--</td>
          <td className="px-3 py-1.5 text-muted-foreground">--</td>
          <td className="px-3 py-1.5 text-muted-foreground">{t('finder.folderKind')}</td>
          <td className="px-3 py-1.5 text-muted-foreground">
            {new Date(f.created_at).toLocaleDateString()}
          </td>
        </tr>
      </WithMenu>,
    );
  }
  for (const a of props.assets) {
    const selected = props.selectedAssets.has(a.id);
    const isRenaming = props.renaming?.kind === 'asset' && props.renaming.id === a.id;
    const authors = formatAuthors(a);
    rows.push(
      <WithMenu key={a.id} menu={props.menuFor('asset', a.id)}>
        <tr
          className={cn(
            'cursor-default border-b border-border/40 select-none',
            selected ? 'bg-accent text-accent-foreground' : 'hover:bg-muted/60',
          )}
          {...itemHandlers(props, 'asset', a.id, ordered)}
        >
          <td className="max-w-0 px-3 py-1.5">
            <span className="flex items-center gap-2">
              <FileGlyph asset={a} className="size-4 shrink-0 text-muted-foreground" />
              {isRenaming ? (
                <RenameInput
                  defaultName={a.name}
                  onSubmit={props.onRenameSubmit}
                  onCancel={props.onRenameCancel}
                />
              ) : (
                <span className="truncate">{a.name}</span>
              )}
            </span>
          </td>
          <td className="whitespace-nowrap px-3 py-1.5 text-muted-foreground tabular-nums">
            {a.paper?.year ?? ''}
          </td>
          <td className="max-w-[12rem] px-3 py-1.5 text-muted-foreground">
            <span className="block truncate" title={authors || undefined}>
              {authors}
            </span>
          </td>
          <td className="max-w-[10rem] px-3 py-1.5 text-muted-foreground">
            <span className="block truncate" title={a.paper?.journal || undefined}>
              {a.paper?.journal ?? ''}
            </span>
          </td>
          <td className="px-3 py-1.5 text-muted-foreground">{formatBytes(a.size_bytes)}</td>
          <td className="px-3 py-1.5">
            <StatusBits asset={a} compact />
          </td>
          <td className="px-3 py-1.5 text-muted-foreground">
            {new Date(a.created_at).toLocaleDateString()}
          </td>
        </tr>
      </WithMenu>,
    );
  }
  return (
    <div className="min-h-64 flex-1 overflow-auto rounded-md border" {...dropZone((e) => props.onDropOnFolder(e, null))}>
      <table className="w-full text-sm">
        <thead className="sticky top-0 z-10 bg-muted/95 backdrop-blur">
          <tr className="text-left text-xs text-muted-foreground">
            <th className="px-3 py-2 font-medium">{t('finder.colName')}</th>
            <th className="w-16 px-3 py-2 font-medium">{t('finder.colYear')}</th>
            <th className="w-44 px-3 py-2 font-medium">{t('finder.colAuthors')}</th>
            <th className="w-36 px-3 py-2 font-medium">{t('finder.colJournal')}</th>
            <th className="w-24 px-3 py-2 font-medium">{t('finder.colSize')}</th>
            <th className="w-28 px-3 py-2 font-medium">{t('finder.colStatus')}</th>
            <th className="w-40 px-3 py-2 font-medium">{t('finder.colModified')}</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 图标（预览）视图
// ---------------------------------------------------------------------------

export function FinderGalleryView(props: FinderViewProps) {
  const ordered = finderOrderedItems(props.folders, props.assets);
  return (
    <div
      className="min-h-64 flex-1 overflow-auto rounded-md border p-3"
      {...dropZone((e) => props.onDropOnFolder(e, null))}
    >
      <div className="grid grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-3">
        {props.folders.map((f) => {
          const selected = props.selectedFolders.has(f.id);
          const isRenaming = props.renaming?.kind === 'folder' && props.renaming.id === f.id;
          return (
            <WithMenu key={f.id} menu={props.menuFor('folder', f.id)}>
              <div
                className={cn(
                  'flex cursor-default flex-col items-center gap-1.5 rounded-lg p-2 select-none',
                  selected ? 'bg-accent text-accent-foreground' : 'hover:bg-muted/60',
                )}
                {...itemHandlers(props, 'folder', f.id, ordered)}
              >
                <Folder className="size-12 shrink-0 text-muted-foreground" strokeWidth={1.25} />
                {isRenaming ? (
                  <RenameInput
                    defaultName={f.name}
                    onSubmit={props.onRenameSubmit}
                    onCancel={props.onRenameCancel}
                  />
                ) : (
                  <span className="w-full truncate text-center text-[13px]" title={f.name}>
                    {f.name}
                  </span>
                )}
              </div>
            </WithMenu>
          );
        })}
        {props.assets.map((a) => {
          const selected = props.selectedAssets.has(a.id);
          const thumb = props.thumbFor(a);
          const isRenaming = props.renaming?.kind === 'asset' && props.renaming.id === a.id;
          return (
            <WithMenu key={a.id} menu={props.menuFor('asset', a.id)}>
              <div
                className={cn(
                  'flex cursor-default flex-col items-center gap-1.5 rounded-lg p-2 select-none',
                  selected ? 'bg-accent text-accent-foreground' : 'hover:bg-muted/60',
                )}
                {...itemHandlers(props, 'asset', a.id, ordered)}
              >
                {a.is_image ? (
                  thumb ? (
                    <img
                      alt={a.name}
                      src={thumb}
                      className="size-12 shrink-0 rounded border object-cover"
                    />
                  ) : (
                    <span className="grid size-12 shrink-0 place-items-center rounded border">
                      <Spinner className="size-4 text-muted-foreground" />
                    </span>
                  )
                ) : (
                  <FileGlyph
                    asset={a}
                    className="size-12 shrink-0 text-muted-foreground"
                    strokeWidth={1.25}
                  />
                )}
                {isRenaming ? (
                  <RenameInput
                    defaultName={a.name}
                    onSubmit={props.onRenameSubmit}
                    onCancel={props.onRenameCancel}
                  />
                ) : (
                  <span className="w-full truncate text-center text-[13px]" title={a.name}>
                    {a.name}
                  </span>
                )}
                <StatusBits asset={a} compact />
              </div>
            </WithMenu>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 分栏视图（macOS Finder columns）
// ---------------------------------------------------------------------------

export interface FinderColumnsViewProps extends FinderViewProps {
  /** 列链（下钻的文件夹 id 序列，不含根；受控于 app）。 */
  path: string[];
  /** 每列的文件夹与资产来源（含 null = 根；app 经 useColumnsFolderAssets 提供）。 */
  foldersOf: (folderId: string | null) => FinderFolder[];
  assetsOf: (folderId: string | null) => FinderAsset[];
  /** 在第 depth 列点击文件夹：path 截断到 depth 后下钻（depth 0 = 根列）。 */
  onDescend: (depth: number, folderId: string) => void;
  /** 选中文件的快捷预览面板（列视图最右）。 */
  previewAsset: FinderAsset | null;
}

export function FinderColumnsView(props: FinderColumnsViewProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const columnCount = props.path.length + 1;
  // 下钻出新列后滚到最右（Finder 行为：新列始终可见）
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [columnCount]);
  // 列序列：第 0 列 = 根内容；第 depth 列（1..path.length）= path[depth-1] 的内容
  const columns: React.ReactNode[] = Array.from(
    { length: props.path.length + 1 },
    (_, depth) => {
      const fid = depth === 0 ? null : props.path[depth - 1];
      return <Column key={fid ?? 'root'} props={props} folderId={fid} depth={depth} />;
    },
  );
  return (
    <div
      ref={scrollRef}
      className="flex min-h-64 flex-1 overflow-x-auto overflow-y-hidden rounded-md border"
    >
      {columns}
      {props.previewAsset && (
        <ColumnPreview asset={props.previewAsset} thumbFor={props.thumbFor} />
      )}
    </div>
  );
}

function Column({
  props,
  folderId,
  depth,
}: {
  props: FinderColumnsViewProps;
  folderId: string | null;
  depth: number;
}) {
  const folders = props.foldersOf(folderId);
  const assets = props.assetsOf(folderId);
  const ordered = finderOrderedItems(folders, assets);
  const onPath = (id: string) => props.path.includes(id);
  const { t } = useTranslation();
  return (
    <div
      className={cn(
        'flex w-56 shrink-0 flex-col overflow-y-auto',
        depth < props.path.length ? 'border-r' : 'bg-muted/30',
      )}
      {...dropZone((e) => props.onDropOnFolder(e, folderId))}
    >
      {folders.map((f) => {
        const selected = props.selectedFolders.has(f.id);
        const isRenaming = props.renaming?.kind === 'folder' && props.renaming.id === f.id;
        return (
          <WithMenu key={f.id} menu={props.menuFor('folder', f.id)}>
            <div
              className={cn(
                'flex cursor-default items-center gap-2 px-2 py-1.5 text-[13px] select-none',
                selected || onPath(f.id) ? 'bg-accent text-accent-foreground' : 'hover:bg-muted/60',
              )}
              {...itemHandlers(props, 'folder', f.id, ordered)}
              onClick={(e) => {
                e.stopPropagation();
                const additive = e.metaKey || e.ctrlKey;
                const range = e.shiftKey;
                props.onSelect('folder', f.id, { additive, range }, ordered);
                // 加减选 / 连续选时不下钻，避免 path 变更清空选中
                if (!additive && !range) props.onDescend(depth, f.id);
              }}
            >
              <Folder className="size-4 shrink-0 text-muted-foreground" />
              {isRenaming ? (
                <RenameInput
                  defaultName={f.name}
                  onSubmit={props.onRenameSubmit}
                  onCancel={props.onRenameCancel}
                />
              ) : (
                <span className="truncate">{f.name}</span>
              )}
              <ChevronRight className="ml-auto size-3.5 shrink-0 text-muted-foreground/60" />
            </div>
          </WithMenu>
        );
      })}
      {assets.map((a) => {
        const selected = props.selectedAssets.has(a.id);
        const isRenaming = props.renaming?.kind === 'asset' && props.renaming.id === a.id;
        return (
          <WithMenu key={a.id} menu={props.menuFor('asset', a.id)}>
            <div
              className={cn(
                'flex cursor-default items-center gap-2 px-2 py-1.5 text-[13px] select-none',
                selected ? 'bg-accent text-accent-foreground' : 'hover:bg-muted/60',
              )}
              {...itemHandlers(props, 'asset', a.id, ordered)}
            >
              <FileGlyph asset={a} className="size-4 shrink-0 text-muted-foreground" />
              {isRenaming ? (
                <RenameInput
                  defaultName={a.name}
                  onSubmit={props.onRenameSubmit}
                  onCancel={props.onRenameCancel}
                />
              ) : (
                <span className="truncate">{a.name}</span>
              )}
            </div>
          </WithMenu>
        );
      })}
      {folders.length === 0 && assets.length === 0 && (
        <p className="px-2 py-3 text-xs text-muted-foreground">{t('finder.emptyFolder')}</p>
      )}
    </div>
  );
}

/** 列视图最右预览面板：缩略图 + 元信息。 */
function ColumnPreview({
  asset,
  thumbFor,
}: {
  asset: FinderAsset;
  thumbFor: (a: FinderAsset) => string | null;
}) {
  const thumb = asset.is_image ? thumbFor(asset) : null;
  return (
    <div className="hidden w-64 shrink-0 flex-col gap-3 overflow-y-auto border-l p-3 md:flex">
      {asset.is_image ? (
        thumb ? (
          <img alt={asset.name} src={thumb} className="w-full rounded border object-cover" />
        ) : (
          <div className="grid aspect-square w-full place-items-center rounded border">
            <Spinner className="size-5 text-muted-foreground" />
          </div>
        )
      ) : (
        <FileGlyph asset={asset} className="mx-auto size-16 text-muted-foreground" strokeWidth={1} />
      )}
      <div className="space-y-1 text-xs">
        <p className="break-all font-medium">{asset.name}</p>
        <p className="text-muted-foreground">{formatBytes(asset.size_bytes)}</p>
        <p className="text-muted-foreground">{asset.ext || asset.mime_type}</p>
        <StatusBits asset={asset} />
        <p className="text-muted-foreground">{new Date(asset.created_at).toLocaleString()}</p>
      </div>
    </div>
  );
}
