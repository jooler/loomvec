import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router';
import { toast } from 'sonner';
import { ExternalLink } from 'lucide-react';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { useMySpaces, useSpaceCategories, useSpaceTags } from '@/hooks';
import {
  AssetViewerOverlay,
  Finder,
  uploadFilesToFolder,
  useColumnsFolderAssets,
  useFinderAssets,
  useFinderFolders,
  useFinderMutations,
  useFinderThumbs,
  type FinderActions,
  type FinderAsset,
} from '@loomvec/ui/components/finder';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Button } from '@loomvec/ui/components/ui/button';
import { Progress } from '@loomvec/ui/components/ui/progress';
import { AssetsFiltersBar } from './AssetsFiltersBar';
import { EMPTY_FILTERS, type AssetFilters } from './use-assets-list';

/**
 * 空间资产管理（Finder 式，挂在空间详情布局「资产」页签下）：目录树浏览
 * （列表/图标/分栏三视图）+ 右键菜单（拷贝/剪切/粘贴/移动/删除）+ 拖拽
 * （结构移动、拖拽上传到当前文件夹）+ 全幅覆盖层查看（替代跳转详情页；
 * 深链 /a/:id 仍保留给检索命中）。
 */
export function AssetsPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const { t } = useTranslation('assets');
  const [filters, setFilters] = useState<AssetFilters>(EMPTY_FILTERS);
  // 目录链受控（app 持有：当前文件夹取数 + 分栏每列取数都依赖它）
  const [path, setPath] = useState<string[]>([]);
  const currentFolderId = path.length > 0 ? path[path.length - 1] : null;
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [viewingId, setViewingId] = useState<string | null>(null);
  // 去重确认：resolve(true)=仍要上传，resolve(false)=放弃
  const [dupConfirm, setDupConfirm] = useState<{
    names: string[];
    resolve: (ok: boolean) => void;
  } | null>(null);

  const spaces = useMySpaces();
  const space = useMemo(
    () => (spaces.data ?? []).find((s) => s.id === spaceId),
    [spaces.data, spaceId],
  );
  // viewer 只读（Finder 隐藏全部写入口，后端权限兜底）
  const canWrite = space?.my_role === 'owner' || space?.my_role === 'editor';

  const tags = useSpaceTags(spaceId);
  const categories = useSpaceCategories(spaceId);

  const folders = useFinderFolders(api, spaceId);
  const assets = useFinderAssets(api, spaceId, currentFolderId, {
    ext: filters.ext,
    status: filters.status,
    review_status: filters.review_status,
    tagIds: filters.tagIds,
    categoryId: filters.categoryId,
  });
  // 分栏视图：列链上每级文件夹（含根）各一份资产查询（缓存与列表视图互通）
  const columnsAssetsOf = useColumnsFolderAssets(
    api,
    spaceId,
    [null, ...path],
    {
      ext: filters.ext,
      status: filters.status,
      review_status: filters.review_status,
      tagIds: filters.tagIds,
      categoryId: filters.categoryId,
    },
  );
  const thumbFor = useFinderThumbs(api, assets.data?.items ?? []);
  const m = useFinderMutations(api, spaceId);

  // 覆盖层查看的上一/下一个（当前文件夹资产顺序）
  const viewList = useMemo(() => assets.data?.items ?? [], [assets.data]);
  const viewIndex = viewList.findIndex((a) => a.id === viewingId);

  const actions: FinderActions = useMemo(
    () => ({
      createFolder: async (parentId, name) => {
        await m.createFolder.mutateAsync({ parentId, name });
      },
      renameFolder: async (folderId, name) => {
        await m.renameFolder.mutateAsync({ folderId, name });
      },
      renameAsset: async (assetId, name) => {
        await m.renameAsset.mutateAsync({ assetId, name });
      },
      moveItems: async (folderIds, assetIds, target) => {
        for (const fid of folderIds)
          await m.moveFolder.mutateAsync({ folderId: fid, parentId: target });
        for (const aid of assetIds)
          await m.moveAsset.mutateAsync({ assetId: aid, folderId: target });
        m.invalidateAll();
      },
      copyItems: async (folderIds, assetIds, target) => {
        for (const fid of folderIds)
          await m.copyFolder.mutateAsync({ folderId: fid, parentId: target });
        for (const aid of assetIds)
          await m.copyAsset.mutateAsync({ assetId: aid, folderId: target });
        m.invalidateAll();
        toast.success(t('copyStarted'));
      },
      deleteFolders: async (folderIds) => {
        for (const fid of folderIds) await m.deleteFolder.mutateAsync(fid);
      },
      deleteAssets: async (assetIds) => {
        for (const aid of assetIds) await m.deleteAsset.mutateAsync(aid);
        m.invalidateAll();
        toast.success(t('finder.deletedAssets', { count: assetIds.length }));
      },
      retryAssets: async (assetIds) => {
        for (const aid of assetIds) await m.retryAsset.mutateAsync(aid);
      },
      autoRenameAssets: async (assetIds) => {
        await m.autoRenameAssets.mutateAsync(assetIds);
      },
    }),
    [m, t],
  );

  const onUploadFiles = async (files: File[]) => {
    if (files.length === 0 || uploading || !spaceId) return;
    setUploading(true);
    setProgress(0);
    const ok = await uploadFilesToFolder(
      files,
      { spaceId, folderId: currentFolderId },
      {
        onProgress: setProgress,
        onDuplicate: (names) =>
          new Promise<boolean>((resolve) => {
            setDupConfirm({ names, resolve });
          }),
      },
    );
    setUploading(false);
    if (ok > 0) m.invalidateAll();
  };

  return (
    <>
      {/* Finder 高度上下文：视口减去 Finder 之外的页头（p-6 上 24 + 标题行 32（含返回按钮）
          + 间距 32 + 页签 37 + 底部 p-6 24 ≈ 149px），工具栏/筛选/内容区都在容器内 */}
      <div className="flex h-[calc(100dvh-149px)] min-h-96 flex-col">
        <Finder
        key={spaceId}
        spaceName={space?.name}
        folders={folders.data ?? []}
        assets={assets.data?.items ?? []}
        path={path}
        onPathChange={setPath}
        columnsAssetsOf={columnsAssetsOf}
        loading={folders.isLoading || assets.isLoading}
        error={
          folders.isError
            ? (folders.error as Error).message
            : assets.isError
              ? (assets.error as Error).message
              : null
        }
        canWrite={canWrite}
        actions={actions}
        onUploadFiles={(files) => void onUploadFiles(files)}
        onRefresh={() => {
          void folders.refetch();
          void assets.refetch();
        }}
        thumbFor={thumbFor}
        onOpenAsset={(a: FinderAsset) => setViewingId(a.id)}
        toolbarExtra={
          <>
            <AssetsFiltersBar
              spaceId={spaceId}
              filters={filters}
              onFilterChange={(patch) => setFilters((f) => ({ ...f, ...patch }))}
              tags={tags.data ?? []}
              tagsLoading={tags.isLoading}
              categories={categories.data ?? []}
              onRefresh={() => void assets.refetch()}
            />
            {uploading && <Progress value={progress} className="w-full max-w-md" />}
          </>
        }
        viewer={
          <AssetViewerOverlay
            client={api}
            assetId={viewingId}
            onClose={() => setViewingId(null)}
            hasPrev={viewIndex > 0}
            hasNext={viewIndex >= 0 && viewIndex < viewList.length - 1}
            onPrev={() => setViewingId(viewList[viewIndex - 1]?.id ?? null)}
            onNext={() => setViewingId(viewList[viewIndex + 1]?.id ?? null)}
            renderMedia={(assetId) => (
              <p className="text-right">
                <Link
                  to={`/a/${assetId}`}
                  className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:underline"
                >
                  <ExternalLink className="size-3" /> {t('openDetail')}
                </Link>
              </p>
            )}
          />
        }
      />
      </div>

      {/* 去重确认：仍要上传 / 放弃 */}
      <Dialog
        open={dupConfirm !== null}
        onOpenChange={(open) => {
          if (!open) {
            dupConfirm?.resolve(false);
            setDupConfirm(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('dupTitle')}</DialogTitle>
            <DialogDescription>
              {t('dupDesc', { names: dupConfirm?.names.join('、') ?? '' })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                dupConfirm?.resolve(false);
                setDupConfirm(null);
              }}
            >
              {t('abandon')}
            </Button>
            <Button
              onClick={() => {
                dupConfirm?.resolve(true);
                setDupConfirm(null);
              }}
            >
              {t('uploadAnyway')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
