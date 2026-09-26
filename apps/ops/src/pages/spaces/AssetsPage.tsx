import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useParams } from 'react-router';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { api, unwrap } from '@/api';
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
  type FinderAssetFilters,
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';

/**
 * 公共空间资产管理（运营端「资产」页签，Finder 式）：目录树三视图 + 右键
 * 菜单（拷贝/剪切/粘贴/移动/删除）+ 拖拽（结构移动 / 上传到当前文件夹）+
 * 全幅覆盖层查看（原始文件 / MinerU 结果 / chunk 管理——替代原右侧抽屉）。
 * 运营者是空间 owner 成员，复用用户域资产接口，写权限恒开（后端兜底）。
 */

const ALL = '__all__';

interface OpsSpaceDetail {
  id: string;
  name: string;
}

export function AssetsPage() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const { t } = useTranslation('spaces');
  const [filters, setFilters] = useState<FinderAssetFilters>({});
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

  const space = useQuery({
    queryKey: ['ops-space', spaceId],
    queryFn: () =>
      unwrap<OpsSpaceDetail>(
        api.GET('/api/v1/ops/spaces/{space_id}', { params: { path: { space_id: spaceId! } } }),
      ),
    enabled: !!spaceId,
  });

  const folders = useFinderFolders(api, spaceId);
  const assets = useFinderAssets(api, spaceId, currentFolderId, filters);
  // 分栏视图：列链上每级文件夹（含根）各一份资产查询（缓存与列表视图互通）
  const columnsAssetsOf = useColumnsFolderAssets(api, spaceId, [null, ...path], filters);
  const thumbFor = useFinderThumbs(api, assets.data?.items ?? []);
  const m = useFinderMutations(api, spaceId);

  // 覆盖层上一/下一个：分栏视图合并列链资产，避免深目录打开后 prev/next 失效
  const viewList = useMemo(() => {
    if (columnsAssetsOf) {
      const seen = new Set<string>();
      const out: FinderAsset[] = [];
      for (const fid of [null, ...path]) {
        for (const a of columnsAssetsOf(fid)) {
          if (seen.has(a.id)) continue;
          seen.add(a.id);
          out.push(a);
        }
      }
      return out;
    }
    return assets.data?.items ?? [];
  }, [assets.data, columnsAssetsOf, path]);
  const viewIndex = viewList.findIndex((a) => a.id === viewingId);

  // 运营者对公共空间恒为 owner 语义（后端 can_manage_asset 兜底）
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
        toast.success(t('assets.copyStarted'));
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
      {/*
       * Finder 高度上下文：视口减去 Finder 之外的页头（顶栏 56 + p-6 上 24 + 标题 28 +
       * 间距 32 + 页签 37 + 底部 p-6 24 ≈ 201px），工具栏/筛选/内容区都在容器内
       */}
      <div className="flex h-[calc(100dvh-201px)] min-h-96 flex-col">
      <Finder
        key={spaceId}
        spaceName={space.data?.name}
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
        canWrite
        actions={actions}
        onUploadFiles={(files) => void onUploadFiles(files)}
        onRefresh={() => {
          void folders.refetch();
          void assets.refetch();
        }}
        thumbFor={thumbFor}
        onOpenAsset={(id) => setViewingId(id)}
        toolbarExtra={
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Select
                value={filters.ext ?? ALL}
                onValueChange={(v) => setFilters((f) => ({ ...f, ext: v === ALL ? undefined : v }))}
              >
                <SelectTrigger className="w-32">
                  <SelectValue placeholder={t('chunks.allTypes')} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>{t('chunks.allTypes')}</SelectItem>
                  <SelectItem value=".pdf">PDF</SelectItem>
                  <SelectItem value=".docx">DOCX</SelectItem>
                  <SelectItem value=".md">MD</SelectItem>
                  <SelectItem value=".txt">TXT</SelectItem>
                  <SelectItem value=".png">PNG</SelectItem>
                </SelectContent>
              </Select>
              <Select
                value={filters.status ?? ALL}
                onValueChange={(v) =>
                  setFilters((f) => ({ ...f, status: v === ALL ? undefined : v }))
                }
              >
                <SelectTrigger className="w-32">
                  <SelectValue placeholder={t('assets.allStatuses')} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>{t('assets.allStatuses')}</SelectItem>
                  <SelectItem value="pending">{t('assetStatus.pending')}</SelectItem>
                  <SelectItem value="processing">{t('assetStatus.processing')}</SelectItem>
                  <SelectItem value="ready">{t('assetStatus.ready')}</SelectItem>
                  <SelectItem value="failed">{t('assetStatus.failed')}</SelectItem>
                </SelectContent>
              </Select>
            </div>
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
            showChunks
            canManageChunks
          />
        }
      />
      </div>

      {/*
       * 去重确认：仍要上传 / 放弃
       */}
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
            <DialogTitle>{t('assets.dupTitle')}</DialogTitle>
            <DialogDescription>
              {t('assets.dupDesc', { names: dupConfirm?.names.join('、') ?? '' })}
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
              {t('assets.abandon')}
            </Button>
            <Button
              onClick={() => {
                dupConfirm?.resolve(true);
                setDupConfirm(null);
              }}
            >
              {t('assets.uploadAnyway')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
