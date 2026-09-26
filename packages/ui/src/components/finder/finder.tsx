/**
 * Finder 主容器：macOS Finder 式空间资产管理（web/ops 共用）。
 *
 * - 三视图（列表/图标预览/分栏）+ 面包屑导航；
 * - 右键菜单（打开/重命名/按论文元数据重命名/拷贝/剪切/粘贴/移到/复制到/删除/重试）；
 * - 拖拽移动（条目 → 文件夹/列/面包屑）与拖拽上传（文件 → 当前文件夹）；
 * - 键盘：⌘C/⌘X/⌘V 剪贴板、Delete 删除、Enter 打开；
 * - 查看覆盖层（viewer 插槽）占满本容器上层。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ChevronRight,
  ClipboardPaste,
  Copy,
  FolderPlus,
  HardDrive,
  List,
  LayoutGrid,
  Columns3,
  Pencil,
  RefreshCw,
  Scissors,
  Trash2,
  Upload,
  Eye,
  RotateCcw,
  WandSparkles,
} from 'lucide-react';
import { cn } from 'cn';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Spinner } from '../ui/spinner';
import { EmptyState } from '../empty-state';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuShortcut,
  ContextMenuTrigger,
} from '../ui/context-menu';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../ui/alert-dialog';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../ui/dialog';
import { FinderColumnsView, FinderGalleryView, FinderListView } from './finder-views';
import { FINDER_DRAG_MIME } from './types';
import type { FinderActions, FinderAsset, FinderClipboard, FinderFolder, FinderViewMode } from './types';

const DRAG_MIME = FINDER_DRAG_MIME;
const VIEW_STORAGE_KEY = 'loomvec.finder.view-mode';

/** mutation 失败已在数据层 toast；此处吞掉 rejection 避免未处理异常。 */
function runQuietly(p: Promise<unknown>) {
  p.catch(() => {});
}

export interface FinderProps {
  /** 空间名（面包屑根节点显示）。 */
  spaceName?: string;
  /** 全空间文件夹（扁平；本组件组树）。 */
  folders: FinderFolder[];
  /** 当前文件夹内资产（app 已按筛选过滤）。 */
  assets: FinderAsset[];
  /** 目录链（下钻的文件夹 id 序列，不含根；受控，app 持有以便分栏列查询）。 */
  path: string[];
  onPathChange: (path: string[]) => void;
  loading?: boolean;
  error?: string | null;
  canWrite: boolean;
  actions: FinderActions;
  /** 拖拽/选择文件上传（app 处理去重确认与进度，目标 = 当前文件夹）。 */
  onUploadFiles: (files: File[]) => void;
  onRefresh: () => void;
  thumbFor?: (asset: FinderAsset) => string | null;
  onOpenAsset: (asset: FinderAsset) => void;
  /** 工具栏第二行插槽（筛选条等）。 */
  toolbarExtra?: React.ReactNode;
  /** 查看覆盖层插槽（absolute 占满本容器）。 */
  viewer?: React.ReactNode;
  /** 分栏视图每列资产来源（app 经 useColumnsFolderAssets 提供；缺省仅链末列有资产）。 */
  columnsAssetsOf?: (folderId: string | null) => FinderAsset[];
}

function loadViewMode(): FinderViewMode {
  if (typeof localStorage === 'undefined') return 'list';
  const v = localStorage.getItem(VIEW_STORAGE_KEY);
  return v === 'gallery' || v === 'columns' ? v : 'list';
}

export function Finder(props: FinderProps) {
  const { folders, assets, actions, canWrite } = props;
  const { t } = useTranslation();
  // 键盘快捷键仅在焦点位于 Finder 容器内时生效（可聚焦根，点击容器即聚焦）
  const rootRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<FinderViewMode>(loadViewMode);
  const [selectedFolders, setSelectedFolders] = useState<Set<string>>(new Set());
  const [selectedAssets, setSelectedAssets] = useState<Set<string>>(new Set());
  const [clipboard, setClipboard] = useState<FinderClipboard | null>(null);
  const [renaming, setRenaming] = useState<
    { kind: 'folder' | 'asset'; id: string; name: string } | null
  >(null);
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<{
    folderIds: string[];
    assetIds: string[];
  } | null>(null);
  /** 移动/复制对话框：null 关闭；mode 区分移动与复制。 */
  const [relocate, setRelocate] = useState<{
    mode: 'move' | 'copy';
    folderIds: string[];
    assetIds: string[];
  } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  /** Shift 连续选的锚点（普通/⌘ 点击更新；连续选本身不移动锚点）。 */
  const selectionAnchorRef = useRef<{ kind: 'folder' | 'asset'; id: string } | null>(null);

  const foldersById = useMemo(() => new Map(folders.map((f) => [f.id, f])), [folders]);
  const path = props.path;
  const currentFolderId = path.length > 0 ? path[path.length - 1] : null;

  /** folderId 的祖先链（含自身；不含 root）。 */
  const chainOf = useCallback(
    (folderId: string | null): string[] => {
      const chain: string[] = [];
      let cur = folderId ? foldersById.get(folderId) : undefined;
      let guard = 0;
      while (cur && guard < 64) {
        chain.unshift(cur.id);
        cur = cur.parent_id ? foldersById.get(cur.parent_id) : undefined;
        guard += 1;
      }
      return chain;
    },
    [foldersById],
  );

  const onPathChange = props.onPathChange;
  const setNavPath = useCallback(
    (next: string[]) => {
      setSelectedFolders(new Set());
      setSelectedAssets(new Set());
      setRenaming(null);
      selectionAnchorRef.current = null;
      onPathChange(next);
    },
    [onPathChange],
  );

  const navigateTo = useCallback(
    (folderId: string | null) => {
      setNavPath(chainOf(folderId));
    },
    [chainOf, setNavPath],
  );

  /** 文件夹的后代集合（含自身；拖拽目标与移动对话框的禁选依据）。 */
  const descendantsOf = useCallback(
    (folderId: string): Set<string> => {
      const childrenMap = new Map<string | null, string[]>();
      for (const f of folders) {
        const list = childrenMap.get(f.parent_id) ?? [];
        list.push(f.id);
        childrenMap.set(f.parent_id, list);
      }
      const seen = new Set<string>([folderId]);
      const queue = [folderId];
      while (queue.length > 0) {
        for (const child of childrenMap.get(queue.pop()!) ?? []) {
          if (!seen.has(child)) {
            seen.add(child);
            queue.push(child);
          }
        }
      }
      return seen;
    },
    [folders],
  );

  const sortedChildren = useCallback(
    (folderId: string | null) => {
      const childFolders = folders
        .filter((f) => f.parent_id === folderId)
        .sort((a, b) => a.name.localeCompare(b.name, 'zh'));
      const childAssets = [...assets].sort((a, b) => a.name.localeCompare(b.name, 'zh'));
      return { folders: childFolders, assets: childAssets };
    },
    [folders, assets],
  );

  // 列表/图标视图渲染当前文件夹；分栏视图按列链渲染
  const currentChildren = useMemo(() => sortedChildren(currentFolderId), [sortedChildren, currentFolderId]);

  const thumbFor = props.thumbFor ?? (() => null);

  /** 分栏视图右侧预览的资产：选中集恰为一个时，在列链各列与当前文件夹中定位。 */
  const columnsAssetsOf = props.columnsAssetsOf;
  const resolvePreviewAsset = useCallback((): FinderAsset | null => {
    if (selectedAssets.size !== 1) return null;
    const id = [...selectedAssets][0];
    if (columnsAssetsOf) {
      for (const fid of [null, ...path]) {
        const hit = columnsAssetsOf(fid).find((a) => a.id === id);
        if (hit) return hit;
      }
    }
    return assets.find((a) => a.id === id) ?? null;
  }, [assets, columnsAssetsOf, path, selectedAssets]);

  // ------------------------------------------------------------------
  // 选择 / 打开
  // ------------------------------------------------------------------

  /** 选中：普通=单选；⌘/Ctrl=加减；Shift=锚点到当前项的连续选（不移动锚点）。 */
  const onSelect = useCallback(
    (
      kind: 'folder' | 'asset',
      id: string,
      mods: { additive: boolean; range: boolean },
      orderedItems?: Array<{ kind: 'folder' | 'asset'; id: string }>,
    ) => {
      const order =
        orderedItems ??
        [
          ...currentChildren.folders.map((f) => ({ kind: 'folder' as const, id: f.id })),
          ...currentChildren.assets.map((a) => ({ kind: 'asset' as const, id: a.id })),
        ];

      if (mods.range && selectionAnchorRef.current) {
        const anchor = selectionAnchorRef.current;
        const i0 = order.findIndex((x) => x.kind === anchor.kind && x.id === anchor.id);
        const i1 = order.findIndex((x) => x.kind === kind && x.id === id);
        if (i0 >= 0 && i1 >= 0) {
          const [lo, hi] = i0 < i1 ? [i0, i1] : [i1, i0];
          const slice = order.slice(lo, hi + 1);
          const rangeFolders = new Set(
            slice.filter((x) => x.kind === 'folder').map((x) => x.id),
          );
          const rangeAssets = new Set(
            slice.filter((x) => x.kind === 'asset').map((x) => x.id),
          );
          if (mods.additive) {
            setSelectedFolders((s) => new Set([...s, ...rangeFolders]));
            setSelectedAssets((s) => new Set([...s, ...rangeAssets]));
          } else {
            setSelectedFolders(rangeFolders);
            setSelectedAssets(rangeAssets);
          }
          // 连续选保留锚点，便于再次 Shift 扩展
          return;
        }
      }

      if (mods.additive) {
        if (kind === 'folder') {
          setSelectedFolders((s) => {
            const next = new Set(s);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
          });
        } else {
          setSelectedAssets((s) => {
            const next = new Set(s);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
          });
        }
      } else {
        setSelectedFolders(kind === 'folder' ? new Set([id]) : new Set());
        setSelectedAssets(kind === 'asset' ? new Set([id]) : new Set());
      }
      selectionAnchorRef.current = { kind, id };
    },
    [currentChildren],
  );

  const openFolder = useCallback(
    (id: string) => navigateTo(id),
    [navigateTo],
  );

  const onOpen = useCallback(
    (kind: 'folder' | 'asset', id: string) => {
      if (kind === 'folder') {
        openFolder(id);
        return;
      }
      const asset = assets.find((a) => a.id === id);
      if (asset) props.onOpenAsset(asset);
    },
    [assets, openFolder, props],
  );

  // ------------------------------------------------------------------
  // 剪贴板 / 删除 / 重命名
  // ------------------------------------------------------------------

  const selectionSnapshot = useCallback(
    (kind: 'folder' | 'asset', id: string) => {
      // 仅在 onClick 等事件里调用；渲染期禁止（内部会 onSelect→setState，menuFor 渲染会死循环）
      // 菜单作用于整个选中集；右键未选中条目时先单选
      const inFolder = kind === 'folder' && selectedFolders.has(id);
      const inAsset = kind === 'asset' && selectedAssets.has(id);
      if (!inFolder && !inAsset) onSelect(kind, id, { additive: false, range: false });
      return {
        folderIds: inFolder ? [...selectedFolders] : kind === 'folder' ? [id] : [],
        assetIds: inAsset ? [...selectedAssets] : kind === 'asset' ? [id] : [],
      };
    },
    [onSelect, selectedAssets, selectedFolders],
  );

  const pasteInto = useCallback(
    async (targetFolderId: string | null) => {
      if (!clipboard) return;
      const { folderIds, assetIds, mode } = clipboard;
      if (folderIds.length === 0 && assetIds.length === 0) return;
      if (mode === 'cut') {
        await actions.moveItems(folderIds, assetIds, targetFolderId);
        setClipboard(null);
        toast.success(t('finder.moved'));
      } else {
        // 复制成功提示由 app 层 actions.copyItems 负责（含「重新走管线」语义）
        await actions.copyItems(folderIds, assetIds, targetFolderId);
      }
    },
    [actions, clipboard, t],
  );

  const submitDelete = useCallback(async () => {
    if (!deleteTarget) return;
    const { folderIds, assetIds } = deleteTarget;
    setDeleteTarget(null);
    for (const fid of folderIds) await actions.deleteFolders([fid]);
    if (assetIds.length > 0) {
      await Promise.all(assetIds.map((aid) => actions.deleteAssets([aid])));
      toast.success(t('finder.deletedAssets', { count: assetIds.length }));
    }
    setSelectedFolders(new Set());
    setSelectedAssets(new Set());
  }, [actions, deleteTarget, t]);

  const submitRename = useCallback(
    (name: string) => {
      if (!renaming) return;
      const target = renaming;
      setRenaming(null);
      const trimmed = name.trim();
      if (!trimmed || trimmed === target.name) return;
      if (target.kind === 'folder') runQuietly(actions.renameFolder(target.id, trimmed));
      else runQuietly(actions.renameAsset(target.id, trimmed));
    },
    [actions, renaming],
  );

  // ------------------------------------------------------------------
  // 拖拽（结构移动 / 文件上传）
  // ------------------------------------------------------------------

  const onDragStartItem = useCallback(
    (e: React.DragEvent, kind: 'folder' | 'asset', id: string) => {
      const snap = selectionSnapshot(kind, id);
      e.dataTransfer.setData(
        DRAG_MIME,
        JSON.stringify({ folderIds: snap.folderIds, assetIds: snap.assetIds }),
      );
      e.dataTransfer.effectAllowed = 'move';
    },
    [selectionSnapshot],
  );

  const onDropOnFolder = useCallback(
    (e: React.DragEvent, targetFolderId: string | null) => {
      const raw = e.dataTransfer.getData(DRAG_MIME);
      if (!raw) return; // 非结构拖拽（文件上传走容器层）
      e.preventDefault();
      e.stopPropagation();
      let payload: { folderIds: string[]; assetIds: string[] };
      try {
        payload = JSON.parse(raw);
      } catch {
        return;
      }
      for (const fid of payload.folderIds ?? []) {
        if (targetFolderId && descendantsOf(fid).has(targetFolderId)) {
          toast.error(t('finder.cannotMoveIntoSelf'));
          return;
        }
      }
      actions
        .moveItems(payload.folderIds ?? [], payload.assetIds ?? [], targetFolderId)
        .then(() => toast.success(t('finder.moved')))
        .catch(() => {});
    },
    [actions, descendantsOf, t],
  );

  // ------------------------------------------------------------------
  // 键盘：⌘C/⌘X/⌘V、Delete、Enter
  // 仅当焦点位于 Finder 容器内（点击容器即聚焦）且不在查看覆盖层内时生效，
  // 避免拦截页面其它区域的复制/删除等浏览器默认行为。
  // ------------------------------------------------------------------

  useEffect(() => {
    const onKeydown = (e: KeyboardEvent) => {
      const root = rootRef.current;
      const active = document.activeElement as HTMLElement | null;
      if (!root || !active || !root.contains(active)) return;
      if (active.closest('[data-finder-viewer]')) return; // 查看覆盖层打开时禁用
      if (
        active.tagName === 'INPUT' ||
        active.tagName === 'TEXTAREA' ||
        active.isContentEditable
      )
        return;
      const mod = e.metaKey || e.ctrlKey;
      const hasSelection = selectedFolders.size > 0 || selectedAssets.size > 0;
      if (mod && e.key.toLowerCase() === 'c' && hasSelection) {
        e.preventDefault();
        setClipboard({
          mode: 'copy',
          folderIds: [...selectedFolders],
          assetIds: [...selectedAssets],
        });
        toast.info(t('finder.copiedSelection'));
      } else if (mod && e.key.toLowerCase() === 'x' && hasSelection && canWrite) {
        e.preventDefault();
        setClipboard({
          mode: 'cut',
          folderIds: [...selectedFolders],
          assetIds: [...selectedAssets],
        });
        toast.info(t('finder.cutSelection'));
      } else if (mod && e.key.toLowerCase() === 'v' && clipboard && canWrite) {
        e.preventDefault();
        runQuietly(pasteInto(currentFolderId));
      } else if ((e.key === 'Delete' || e.key === 'Backspace') && hasSelection && canWrite) {
        e.preventDefault();
        setDeleteTarget({ folderIds: [...selectedFolders], assetIds: [...selectedAssets] });
      } else if (e.key === 'Enter' && hasSelection) {
        e.preventDefault();
        const fid = [...selectedFolders][0];
        if (fid) openFolder(fid);
        else {
          const aid = [...selectedAssets][0];
          if (aid) onOpen('asset', aid);
        }
      }
    };
    window.addEventListener('keydown', onKeydown);
    return () => window.removeEventListener('keydown', onKeydown);
  }, [
    canWrite,
    clipboard,
    currentFolderId,
    onOpen,
    openFolder,
    pasteInto,
    selectedAssets,
    selectedFolders,
    t,
  ]);

  // ------------------------------------------------------------------
  // 右键菜单
  // ------------------------------------------------------------------

  const menuFor = useCallback(
    (kind: 'folder' | 'asset', id: string) => (
      <ContextMenuContent>
        <ContextMenuItem onClick={() => onOpen(kind, id)}>
          <Eye /> {t('finder.open')}
          <ContextMenuShortcut>⏎</ContextMenuShortcut>
        </ContextMenuItem>
        {canWrite && (
          <>
            <ContextMenuSeparator />
            <ContextMenuItem
              onClick={() => {
                const f = kind === 'folder' ? foldersById.get(id) : assets.find((a) => a.id === id);
                if (f) setRenaming({ kind, id, name: f.name });
              }}
            >
              <Pencil /> {t('finder.rename')}
            </ContextMenuItem>
            <ContextMenuItem
              onClick={() => {
                const snap = selectionSnapshot(kind, id);
                setClipboard({ mode: 'copy', ...snap });
                toast.info(t('finder.copiedDone'));
              }}
            >
              <Copy /> {t('finder.copy')}
              <ContextMenuShortcut>⌘C</ContextMenuShortcut>
            </ContextMenuItem>
            <ContextMenuItem
              onClick={() => {
                const snap = selectionSnapshot(kind, id);
                setClipboard({ mode: 'cut', ...snap });
                toast.info(t('finder.cutDone'));
              }}
            >
              <Scissors /> {t('finder.cut')}
              <ContextMenuShortcut>⌘X</ContextMenuShortcut>
            </ContextMenuItem>
            <ContextMenuItem
              onClick={() => setRelocate({ mode: 'move', ...selectionSnapshot(kind, id) })}
            >
              {t('finder.moveTo')}
            </ContextMenuItem>
            <ContextMenuItem
              onClick={() => setRelocate({ mode: 'copy', ...selectionSnapshot(kind, id) })}
            >
              {t('finder.copyTo')}
            </ContextMenuItem>
            {kind === 'asset' &&
              (() => {
                // 仅决定菜单项是否显示；禁止在此调用 selectionSnapshot（其会 setState，渲染期会死循环）
                const previewIds = selectedAssets.has(id) ? [...selectedAssets] : [id];
                const selected = previewIds
                  .map((aid) => assets.find((x) => x.id === aid))
                  .filter((a): a is NonNullable<typeof a> => !!a);
                const showAutoRename = selected.some(
                  (a) => a.mime_type === 'application/pdf' || a.ext.toLowerCase() === 'pdf',
                );
                const showRetry = selected.some((a) => a.status === 'failed');
                return (
                  <>
                    {showAutoRename && (
                      <ContextMenuItem
                        onClick={() => {
                          const ids = selectionSnapshot(kind, id)
                            .assetIds.map((aid) => assets.find((x) => x.id === aid))
                            .filter((a): a is NonNullable<typeof a> => !!a)
                            .filter(
                              (a) =>
                                a.mime_type === 'application/pdf' ||
                                a.ext.toLowerCase() === 'pdf',
                            )
                            .map((a) => a.id);
                          if (ids.length > 0) runQuietly(actions.autoRenameAssets(ids));
                        }}
                      >
                        <WandSparkles /> {t('finder.autoRename')}
                      </ContextMenuItem>
                    )}
                    {showRetry ? (
                      <ContextMenuItem
                        onClick={() => {
                          const ids = selectionSnapshot(kind, id)
                            .assetIds.map((aid) => assets.find((x) => x.id === aid))
                            .filter((a): a is NonNullable<typeof a> => !!a)
                            .filter((a) => a.status === 'failed')
                            .map((a) => a.id);
                          if (ids.length > 0) runQuietly(actions.retryAssets(ids));
                        }}
                      >
                        <RotateCcw /> {t('finder.retry')}
                      </ContextMenuItem>
                    ) : null}
                  </>
                );
              })()}
            <ContextMenuSeparator />
            <ContextMenuItem
              variant="destructive"
              onClick={() => setDeleteTarget(selectionSnapshot(kind, id))}
            >
              <Trash2 /> {t('finder.delete')}
            </ContextMenuItem>
          </>
        )}
      </ContextMenuContent>
    ),
    [actions, assets, canWrite, foldersById, onOpen, selectedAssets, selectionSnapshot, t],
  );

  const blankMenu = (
    <ContextMenuContent>
      {canWrite && (
        <>
          <ContextMenuItem onClick={() => setNewFolderOpen(true)}>
            <FolderPlus /> {t('finder.newFolder')}
          </ContextMenuItem>
          <ContextMenuItem onClick={() => fileInputRef.current?.click()}>
            <Upload /> {t('finder.uploadFile')}
          </ContextMenuItem>
          <ContextMenuSeparator />
        </>
      )}
      <ContextMenuItem
        disabled={!clipboard}
        onClick={() => clipboard && runQuietly(pasteInto(currentFolderId))}
      >
        <ClipboardPaste /> {t('finder.paste')}
        <ContextMenuShortcut>⌘V</ContextMenuShortcut>
      </ContextMenuItem>
      <ContextMenuItem onClick={props.onRefresh}>
        <RefreshCw /> {t('finder.refresh')}
      </ContextMenuItem>
    </ContextMenuContent>
  );

  // ------------------------------------------------------------------
  // 渲染
  // ------------------------------------------------------------------

  const viewProps = {
    folders: currentChildren.folders,
    assets: currentChildren.assets,
    selectedFolders,
    selectedAssets,
    renaming,
    canWrite,
    thumbFor,
    menuFor,
    onSelect,
    onOpen,
    onRenameSubmit: submitRename,
    onRenameCancel: () => setRenaming(null),
    onDragStartItem,
    onDropOnFolder,
  };

  const total = currentChildren.folders.length + currentChildren.assets.length;
  const deletingCount = deleteTarget
    ? deleteTarget.folderIds.length + deleteTarget.assetIds.length
    : 0;

  const viewButtons: Array<{ mode: FinderViewMode; icon: React.ReactNode; label: string }> = [
    { mode: 'list', icon: <List className="size-4" />, label: t('finder.listView') },
    { mode: 'gallery', icon: <LayoutGrid className="size-4" />, label: t('finder.galleryView') },
    { mode: 'columns', icon: <Columns3 className="size-4" />, label: t('finder.columnsView') },
  ];

  return (
    // h-full 由页面级容器提供高度上下文（两端页头构成不同，偏移在 app 侧计算）；
    // 内容区 flex-1 + overflow 滚动，查看覆盖层（absolute inset-0）随之占满剩余高度
    <div
      ref={rootRef}
      tabIndex={0}
      onPointerDown={() => rootRef.current?.focus()}
      className="relative flex h-full min-h-96 flex-1 flex-col gap-3 outline-none"
    >
      {/* 工具栏：面包屑 + 动作 */}
      <div className="flex flex-wrap items-center gap-2">
        <nav className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto text-sm" aria-label={t('finder.breadcrumbLabel')}>
          <button
            className="flex shrink-0 items-center gap-1.5 rounded px-1.5 py-1 hover:bg-muted"
            onDragOver={(e) => {
              if (e.dataTransfer.types.includes(DRAG_MIME)) e.preventDefault();
            }}
            onDrop={(e) => onDropOnFolder(e, null)}
            onClick={() => navigateTo(null)}
          >
            <HardDrive className="size-4 text-muted-foreground" />
            <span className="max-w-32 truncate">{props.spaceName ?? t('finder.space')}</span>
          </button>
          {path.map((fid) => {
            const f = foldersById.get(fid);
            if (!f) return null;
            return (
              <span key={fid} className="flex shrink-0 items-center gap-0.5">
                <ChevronRight className="size-3.5 text-muted-foreground/60" />
                <button
                  className="max-w-40 truncate rounded px-1.5 py-1 hover:bg-muted"
                  onDragOver={(e) => {
                    if (e.dataTransfer.types.includes(DRAG_MIME)) e.preventDefault();
                  }}
                  onDrop={(e) => onDropOnFolder(e, fid)}
                  onClick={() => navigateTo(fid)}
                >
                  {f.name}
                </button>
              </span>
            );
          })}
        </nav>
        <div className="flex items-center gap-1 rounded-md border p-0.5">
          {viewButtons.map((vb) => (
            <button
              key={vb.mode}
              title={vb.label}
              aria-label={vb.label}
              className={cn(
                'grid size-7 place-items-center rounded-sm',
                view === vb.mode
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-muted',
              )}
              onClick={() => {
                setView(vb.mode);
                if (typeof localStorage !== 'undefined')
                  localStorage.setItem(VIEW_STORAGE_KEY, vb.mode);
              }}
            >
              {vb.icon}
            </button>
          ))}
        </div>
        {canWrite && (
          <>
            <Button variant="outline" size="sm" onClick={() => setNewFolderOpen(true)}>
              <FolderPlus /> {t('finder.newFolder')}
            </Button>
            <Button variant="outline" size="sm" onClick={() => fileInputRef.current?.click()}>
              <Upload /> {t('finder.upload')}
            </Button>
          </>
        )}
        <Button variant="outline" size="sm" onClick={props.onRefresh}>
          <RefreshCw /> {t('finder.refresh')}
        </Button>
      </div>

      {props.toolbarExtra}

      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = '';
          if (files.length > 0) props.onUploadFiles(files);
        }}
      />

      {/* 内容区：空白右键菜单 + 拖拽上传 */}
      <BlankMenuZone menu={blankMenu} onUploadFiles={props.onUploadFiles}>
        {props.error ? (
          <p className="py-10 text-center text-sm text-destructive">{props.error}</p>
        ) : props.loading ? (
          <div className="grid place-items-center py-16">
            <Spinner className="size-5 text-muted-foreground" />
          </div>
        ) : view === 'columns' ? (
          // 分栏视图始终渲染（当前列为空时仍需展示父列链，空列有自己的提示）
          <FinderColumnsView
            {...viewProps}
            path={path}
            foldersOf={(fid) =>
              folders
                .filter((f) => f.parent_id === fid)
                .sort((a, b) => a.name.localeCompare(b.name, 'zh'))
            }
            assetsOf={props.columnsAssetsOf ?? ((fid) => (fid === currentFolderId ? currentChildren.assets : []))}
            onDescend={(depth, folderId) => {
              // 第 depth 列点击：截断到 depth 后下钻（depth 0 = 根列）
              setNavPath([...path.slice(0, depth), folderId]);
            }}
            previewAsset={resolvePreviewAsset()}
          />
        ) : total === 0 ? (
          <EmptyState
            title={t('finder.folderEmptyTitle')}
            description={canWrite ? t('finder.folderEmptyHint') : undefined}
            className="flex-1"
          />
        ) : view === 'gallery' ? (
          <FinderGalleryView {...viewProps} />
        ) : (
          <FinderListView {...viewProps} />
        )}
      </BlankMenuZone>

      {/* 查看覆盖层：占满本容器上层（替代右侧抽屉）；contents 包裹层仅打标记供键盘作用域判断 */}
      <div data-finder-viewer className="contents">
        {props.viewer}
      </div>

      {/* 新建文件夹 */}
      <Dialog open={newFolderOpen} onOpenChange={setNewFolderOpen}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('finder.newFolder')}</DialogTitle>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const name = newFolderName.trim();
              if (!name) return;
              setNewFolderOpen(false);
              setNewFolderName('');
              runQuietly(actions.createFolder(currentFolderId, name));
            }}
          >
            <Input
              autoFocus
              value={newFolderName}
              placeholder={t('finder.folderNamePlaceholder')}
              onChange={(e) => setNewFolderName(e.target.value)}
            />
            <DialogFooter className="mt-4">
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setNewFolderOpen(false);
                  setNewFolderName('');
                }}
              >
                {t('action.cancel')}
              </Button>
              <Button type="submit" disabled={!newFolderName.trim()}>
                {t('action.create')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 删除确认（文件夹级联 / 资产批量） */}
      <AlertDialog open={deleteTarget !== null} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('finder.deleteTitle', { count: deletingCount })}</AlertDialogTitle>
            <AlertDialogDescription>
              {(deleteTarget?.folderIds.length ?? 0) > 0
                ? t('finder.deleteFoldersDesc')
                : t('finder.deleteAssetsDesc')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('action.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={(e) => {
                e.preventDefault();
                void submitDelete();
              }}
            >
              {t('action.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 移动/复制到（目录树选择；源文件夹子树禁选，防递归移动/复制） */}
      {relocate && (
        <RelocateDialog
          mode={relocate.mode}
          folders={folders}
          currentFolderId={currentFolderId}
          disabledIds={new Set(relocate.folderIds.flatMap((fid) => [...descendantsOf(fid)]))}
          onClose={() => setRelocate(null)}
          onConfirm={async (target) => {
            const req = relocate;
            setRelocate(null);
            if (req.mode === 'move') {
              await actions.moveItems(req.folderIds, req.assetIds, target);
              toast.success(t('finder.moved'));
            } else {
              // 复制成功提示由 app 层 actions.copyItems 负责（含「重新走管线」语义）
              await actions.copyItems(req.folderIds, req.assetIds, target);
            }
          }}
        />
      )}
    </div>
  );
}

/** 内容区外壳：空白右键菜单 + 文件拖拽上传（结构拖拽由视图内条目处理）。 */
function BlankMenuZone({
  menu,
  onUploadFiles,
  children,
}: {
  menu: React.ReactNode;
  onUploadFiles: (files: File[]) => void;
  children: React.ReactNode;
}) {
  const { t } = useTranslation();
  const dragDepth = useRef(0);
  const [uploadHover, setUploadHover] = useState(false);
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          className={cn(
            // min-h-0：允许 flex 子项收缩，列表/图标/分栏内容超出时在内部滚动而非撑破容器
            'relative flex min-h-0 flex-1 flex-col rounded-lg transition-colors',
            uploadHover && 'outline-2 outline-dashed outline-primary',
          )}
          onDragEnter={(e) => {
            if (e.dataTransfer.types.includes('Files')) {
              dragDepth.current += 1;
              setUploadHover(true);
            }
          }}
          onDragOver={(e) => {
            if (e.dataTransfer.types.includes('Files')) e.preventDefault();
          }}
          onDragLeave={() => {
            dragDepth.current = Math.max(0, dragDepth.current - 1);
            if (dragDepth.current === 0) setUploadHover(false);
          }}
          onDrop={(e) => {
            dragDepth.current = 0;
            setUploadHover(false);
            const files = Array.from(e.dataTransfer.files ?? []);
            if (files.length > 0) {
              e.preventDefault();
              e.stopPropagation();
              onUploadFiles(files);
            }
          }}
        >
          {children}
          {uploadHover && (
            <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center rounded-lg bg-primary/5">
              <p className="text-sm font-medium text-primary">{t('finder.releaseToUpload')}</p>
            </div>
          )}
        </div>
      </ContextMenuTrigger>
      {menu}
    </ContextMenu>
  );
}

/** 移动/复制目标选择：目录树单选（根目录 + 全部活文件夹；禁选移动源子树）。 */
function RelocateDialog({
  mode,
  folders,
  currentFolderId,
  disabledIds,
  onClose,
  onConfirm,
}: {
  mode: 'move' | 'copy';
  folders: FinderFolder[];
  currentFolderId: string | null;
  disabledIds: Set<string>;
  onClose: () => void;
  onConfirm: (targetFolderId: string | null) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [target, setTarget] = useState<string | null>(currentFolderId);
  const [pending, setPending] = useState(false);

  const rows = useMemo(() => {
    const byParent = new Map<string | null, FinderFolder[]>();
    for (const f of folders) {
      const list = byParent.get(f.parent_id) ?? [];
      list.push(f);
      byParent.set(f.parent_id, list);
    }
    const out: Array<{ folder: FinderFolder; depth: number }> = [];
    const walk = (parent: string | null, depth: number) => {
      for (const f of (byParent.get(parent) ?? []).sort((a, b) =>
        a.name.localeCompare(b.name, 'zh'),
      )) {
        out.push({ folder: f, depth });
        walk(f.id, depth + 1);
      }
    };
    walk(null, 0);
    return out;
  }, [folders]);

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{mode === 'move' ? t('finder.moveTitle') : t('finder.copyTitle')}</DialogTitle>
        </DialogHeader>
        <div className="max-h-80 overflow-y-auto rounded-md border p-1">
          <button
            className={cn(
              'flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm',
              target === null ? 'bg-accent text-accent-foreground' : 'hover:bg-muted',
            )}
            onClick={() => setTarget(null)}
          >
            <HardDrive className="size-4 text-muted-foreground" /> {t('finder.rootFolder')}
          </button>
          {rows.map(({ folder, depth }) => {
            const disabled = disabledIds.has(folder.id);
            // 缩进分层（Tailwind 类，避免内联样式）
            const depthClass = ['pl-4', 'pl-8', 'pl-12', 'pl-16'][
              Math.min(depth, 3)
            ];
            return (
              <button
                key={folder.id}
                disabled={disabled}
                className={cn(
                  'flex w-full items-center gap-2 rounded-sm py-1.5 pr-2 text-sm',
                  depthClass,
                  target === folder.id
                    ? 'bg-accent text-accent-foreground'
                    : 'hover:bg-muted',
                  disabled && 'cursor-not-allowed opacity-40 hover:bg-transparent',
                )}
                onClick={() => setTarget(folder.id)}
              >
                <span className="truncate">{folder.name}</span>
              </button>
            );
          })}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t('action.cancel')}
          </Button>
          <Button
            disabled={pending}
            onClick={() => {
              setPending(true);
              onConfirm(target).catch(() => {}).finally(() => setPending(false));
            }}
          >
            {pending
              ? t('action.processing')
              : mode === 'move'
                ? t('finder.move')
                : t('finder.copyAction')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
