/**
 * Finder 式资产管理共享类型（packages/ui 单源，web/ops 两端共用）。
 */

/** 文件夹（目录树节点；root = parent_id 为空）。 */
export interface FinderFolder {
  id: string;
  parent_id: string | null;
  name: string;
  created_at: string;
}

/** 资产（列表项；folder_id 挂靠目录树）。 */
export interface FinderAsset {
  id: string;
  name: string;
  ext: string;
  mime_type: string;
  size_bytes: number;
  status: string;
  status_reason: string | null;
  review_status: string | null;
  is_image: boolean;
  folder_id: string | null;
  created_at: string;
}

/** 视图模式：list（列表）/ gallery（图标预览）/ columns（分栏）。 */
export type FinderViewMode = 'list' | 'gallery' | 'columns';

/** 结构拖拽的 dataTransfer MIME（条目 → 文件夹/列/面包屑移动）。 */
export const FINDER_DRAG_MIME = 'application/x-loomvec-finder-items';

/** 剪贴板（拷贝/剪切），粘贴时由目标文件夹接收。 */
export interface FinderClipboard {
  mode: 'copy' | 'cut';
  folderIds: string[];
  assetIds: string[];
}

/** Finder 依赖 app 注入的写操作（数据层在 useFinderData，UI 层零 API 依赖）。 */
export interface FinderActions {
  createFolder(parentId: string | null, name: string): Promise<void>;
  renameFolder(folderId: string, name: string): Promise<void>;
  renameAsset(assetId: string, name: string): Promise<void>;
  /** 移动/复制到目标文件夹（null = 根目录）；后端防环校验兜底。 */
  moveItems(folderIds: string[], assetIds: string[], targetParentId: string | null): Promise<void>;
  copyItems(folderIds: string[], assetIds: string[], targetParentId: string | null): Promise<void>;
  deleteFolders(folderIds: string[]): Promise<void>;
  deleteAssets(assetIds: string[]): Promise<void>;
  retryAssets(assetIds: string[]): Promise<void>;
}
