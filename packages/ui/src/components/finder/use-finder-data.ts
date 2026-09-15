/**
 * Finder 数据层：目录树/当前文件夹资产查询 + 结构与资产写操作（web/ops 共用）。
 *
 * client 由 app 注入（web 用 SDK 默认单例，ops 用自带 401 处理的实例）；
 * 写操作完成后自动失效相关查询（folders / assets 两棵子树）。
 */
import { useMutation, useQuery, useQueryClient, useQueries } from '@tanstack/react-query';
import { toast } from 'sonner';
import type { ApiClient } from '@loomvec/sdk-ts';
import { extractApiError } from '../../lib/format';
import { UPLOAD_SKIPPED, uploadFile } from '../../lib/upload';
import { t } from '../../i18n';
import type { FinderAsset, FinderFolder } from './types';

/** 资产筛选（与资产列表 API 的 query 参数一一对应；空值 = 不过滤）。 */
export interface FinderAssetFilters {
  ext?: string;
  status?: string;
  review_status?: string;
  tagIds?: string[];
  categoryId?: string;
}

const ASSET_PAGE_LIMIT = 100;

function folderKey(folderId: string | null): string {
  return folderId ?? 'root';
}

export function useFinderFolders(client: ApiClient, spaceId?: string) {
  return useQuery({
    queryKey: ['finder-folders', spaceId],
    enabled: !!spaceId,
    queryFn: async () => {
      const { data, error } = await client.GET('/api/v1/spaces/{space_id}/folders', {
        params: { path: { space_id: spaceId! } },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.loadFoldersFailed')));
      return data as FinderFolder[];
    },
  });
}

export function useFinderAssets(
  client: ApiClient,
  spaceId: string | undefined,
  folderId: string | null,
  filters: FinderAssetFilters = {},
) {
  return useQuery({
    queryKey: ['finder-assets', spaceId, folderKey(folderId), filters],
    enabled: !!spaceId,
    queryFn: () => fetchFolderAssets(client, spaceId!, folderId, filters),
    // 处理中轮询（与资产列表页一致：P1 轮询，SSE/Webhook 后置）
    refetchInterval: (query) =>
      query.state.data?.items.some((a) => ['pending', 'processing'].includes(a.status))
        ? 3000
        : false,
  });
}

/** 指定文件夹的资产页（queryKey 与 useFinderAssets 同源，缓存互通）。 */
async function fetchFolderAssets(
  client: ApiClient,
  spaceId: string,
  folderId: string | null,
  filters: FinderAssetFilters,
) {
  const { data, error } = await client.GET('/api/v1/assets', {
    params: {
      query: {
        limit: ASSET_PAGE_LIMIT,
        space_id: spaceId,
        folder_id: folderKey(folderId),
        ext: filters.ext || undefined,
        status: filters.status || undefined,
        review_status: filters.review_status || undefined,
        category_id: filters.categoryId || undefined,
        tag_ids:
          filters.tagIds && filters.tagIds.length > 0 ? filters.tagIds.join(',') : undefined,
      },
    },
  });
  if (error) throw new Error(extractApiError(error, t('ui:finderData.loadAssetsFailed')));
  return data as { items: FinderAsset[] };
}

/**
 * 分栏视图：列链上每个文件夹（含根 null）各一份资产查询，
 * 返回按 folderId 取资产的访问器（缓存 key 与 useFinderAssets 互通）。
 */
export function useColumnsFolderAssets(
  client: ApiClient,
  spaceId: string | undefined,
  columnFolderIds: Array<string | null>,
  filters: FinderAssetFilters = {},
) {
  const queries = useQueries({
    queries: columnFolderIds.map((fid) => ({
      queryKey: ['finder-assets', spaceId, folderKey(fid), filters],
      enabled: !!spaceId,
      queryFn: () => fetchFolderAssets(client, spaceId!, fid, filters),
      refetchInterval: (query: { state: { data?: { items: FinderAsset[] } } }) =>
        query.state.data?.items.some((a) =>
          ['pending', 'processing'].includes(a.status),
        )
          ? 3000
          : false,
    })),
  });
  const map = new Map<string | null, FinderAsset[]>();
  columnFolderIds.forEach((fid, i) => map.set(fid, queries[i]?.data?.items ?? []));
  return (fid: string | null): FinderAsset[] => map.get(fid) ?? [];
}

/**
 * Finder 写操作集合：结构（文件夹 CRUD/移动/复制/删除）+ 资产（重命名/移动/复制/删除/重试）。
 * 失败统一 toast；成功方由调用处提示。
 */export function useFinderMutations(client: ApiClient, spaceId: string | undefined) {
  const queryClient = useQueryClient();

  const invalidateAll = () => {
    void queryClient.invalidateQueries({ queryKey: ['finder-folders', spaceId] });
    void queryClient.invalidateQueries({ queryKey: ['finder-assets', spaceId] });
    // 聚合资产视图（检索页/资产详情）联动刷新
    void queryClient.invalidateQueries({ queryKey: ['assets'] });
  };

  const fail = (e: unknown, fallback: string) =>
    toast.error(e instanceof Error ? e.message : fallback);

  const createFolder = useMutation({
    mutationFn: async ({ parentId, name }: { parentId: string | null; name: string }) => {
      const { error } = await client.POST('/api/v1/spaces/{space_id}/folders', {
        params: { path: { space_id: spaceId! } },
        body: { name, parent_id: parentId ?? undefined },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.createFolderFailed')));
    },
    onSuccess: () => {
      toast.success(t('ui:finderData.folderCreated'));
      invalidateAll();
    },
    onError: (e) => fail(e, t('ui:finderData.createFolderFailed')),
  });

  const renameFolder = useMutation({
    mutationFn: async ({ folderId, name }: { folderId: string; name: string }) => {
      const { error } = await client.PATCH('/api/v1/folders/{folder_id}', {
        params: { path: { folder_id: folderId } },
        body: { name, unset_category: false, unset_folder: false },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.renameFailed')));
    },
    onSuccess: () => {
      toast.success(t('ui:finderData.renamed'));
      invalidateAll();
    },
    onError: (e) => fail(e, t('ui:finderData.renameFailed')),
  });

  const renameAsset = useMutation({
    mutationFn: async ({ assetId, name }: { assetId: string; name: string }) => {
      const { error } = await client.PATCH('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId } },
        body: { name, unset_category: false, unset_folder: false },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.renameFailed')));
    },
    onSuccess: () => {
      toast.success(t('ui:finderData.renamed'));
      invalidateAll();
    },
    onError: (e) => fail(e, t('ui:finderData.renameFailed')),
  });

  const moveFolder = useMutation({
    mutationFn: async ({
      folderId,
      parentId,
    }: {
      folderId: string;
      parentId: string | null;
    }) => {
      const { error } = await client.POST('/api/v1/folders/{folder_id}/move', {
        params: { path: { folder_id: folderId } },
        body: parentId
          ? { parent_id: parentId, unset_parent: false }
          : { unset_parent: true },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.moveFolderFailed')));
    },
    onSuccess: () => {
      toast.success(t('ui:finderData.folderMoved'));
      invalidateAll();
    },
    onError: (e) => fail(e, t('ui:finderData.moveFolderFailed')),
  });

  const moveAsset = useMutation({
    mutationFn: async ({
      assetId,
      folderId,
    }: {
      assetId: string;
      folderId: string | null;
    }) => {
      const { error } = await client.PATCH('/api/v1/assets/{asset_id}/location', {
        params: { path: { asset_id: assetId } },
        body: folderId ? { folder_id: folderId, unset_folder: false } : { unset_folder: true },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.moveAssetFailed')));
    },
    onError: (e) => fail(e, t('ui:finderData.moveAssetFailed')),
  });

  const copyAsset = useMutation({
    mutationFn: async ({
      assetId,
      folderId,
    }: {
      assetId: string;
      folderId: string | null;
    }) => {
      const { error } = await client.POST('/api/v1/assets/{asset_id}/copy', {
        params: { path: { asset_id: assetId } },
        body: { target_folder_id: folderId ?? undefined },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.copyAssetFailed')));
    },
    onError: (e) => fail(e, t('ui:finderData.copyAssetFailed')),
  });

  const copyFolder = useMutation({
    mutationFn: async ({
      folderId,
      parentId,
    }: {
      folderId: string;
      parentId: string | null;
    }) => {
      const { error } = await client.POST('/api/v1/folders/{folder_id}/copy', {
        params: { path: { folder_id: folderId } },
        body: { parent_id: parentId ?? undefined },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.copyFolderFailed')));
    },
    onError: (e) => fail(e, t('ui:finderData.copyFolderFailed')),
  });

  const deleteFolder = useMutation({
    mutationFn: async (folderId: string) => {
      const { error } = await client.DELETE('/api/v1/folders/{folder_id}', {
        params: { path: { folder_id: folderId } },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.deleteFolderFailed')));
    },
    onSuccess: () => {
      toast.success(t('ui:finderData.folderDeleted'));
      invalidateAll();
    },
    onError: (e) => fail(e, t('ui:finderData.deleteFolderFailed')),
  });

  const deleteAsset = useMutation({
    mutationFn: async (assetId: string) => {
      const { error } = await client.DELETE('/api/v1/assets/{asset_id}', {
        params: { path: { asset_id: assetId } },
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.deleteFailed')));
    },
    onError: (e) => fail(e, t('ui:finderData.deleteFailed')),
  });

  const retryAsset = useMutation({
    mutationFn: async (assetId: string) => {
      const { error } = await client.POST('/api/v1/assets/{asset_id}/retry', {
        params: { path: { asset_id: assetId } },
        body: {},
      });
      if (error) throw new Error(extractApiError(error, t('ui:finderData.retryFailed')));
    },
    onSuccess: () => {
      toast.success(t('ui:finderData.requeued'));
      invalidateAll();
    },
    onError: (e) => fail(e, t('ui:finderData.retryFailed')),
  });

  return {
    createFolder,
    renameFolder,
    renameAsset,
    moveFolder,
    moveAsset,
    copyAsset,
    copyFolder,
    deleteFolder,
    deleteAsset,
    retryAsset,
    invalidateAll,
  };
}

export type FinderMutations = ReturnType<typeof useFinderMutations>;

/**
 * 图片资产缩略图：列表 API 不含 renditions，为图片资产批量取详情中的 thumbnail URL。
 * （沿用资产页缩略图策略；非图片与超量的不取。）
 */
export function useFinderThumbs(client: ApiClient, assets: FinderAsset[], cap = 48) {
  const imageIds = assets.filter((a) => a.is_image).slice(0, cap).map((a) => a.id);
  const queries = useQueries({
    queries: imageIds.map((id) => ({
      queryKey: ['finder-thumb', id],
      queryFn: async () => {
        const { data, error } = await client.GET('/api/v1/assets/{asset_id}', {
          params: { path: { asset_id: id } },
        });
        if (error) throw new Error(extractApiError(error, t('ui:finderData.loadThumbFailed')));
        return data.renditions.find((r) => r.kind === 'thumbnail')?.url ?? null;
      },
      staleTime: 10 * 60_000,
    })),
  });
  const map = new Map<string, string | null>();
  imageIds.forEach((id, i) => map.set(id, queries[i]?.data ?? null));
  return (asset: FinderAsset): string | null =>
    asset.is_image ? (map.get(asset.id) ?? null) : null;
}

/** 上传到指定文件夹（串行逐个；去重确认与配额提示在 upload lib 内）。 */
export async function uploadFilesToFolder(
  files: File[],
  opts: { spaceId: string; folderId: string | null },
  hooks: {
    onProgress?: (pct: number) => void;
    onDuplicate: (names: string[]) => Promise<boolean>;
  },
): Promise<number> {
  let done = 0;
  let ok = 0;
  for (const file of files) {
    const base = (done / files.length) * 100;
    try {
      await uploadFile(file, {
        spaceId: opts.spaceId,
        folderId: opts.folderId,
        onProgress: (pct) => hooks.onProgress?.(Math.round(base + pct / files.length)),
        onDuplicate: hooks.onDuplicate,
      });
      ok += 1;
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('ui:finderData.uploadFailed');
      if (msg === UPLOAD_SKIPPED) toast.info(t('ui:finderData.skippedDuplicate', { name: file.name }));
      else toast.error(msg);
    }
    done += 1;
  }
  return ok;
}
