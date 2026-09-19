import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, ChevronDown, ChevronRight, FileText, Folder, FolderOpen } from 'lucide-react';
import { Button } from '@loomvec/ui/components/ui/button';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { useWorkspaceTree, type AgentProjectItem, type WorkspaceNode } from '@/hooks';
import { cn } from 'cn';

/**
 * 项目文件树视图（P5.6）：侧栏整体切换为该项目的 workspace 子树
 * （tree 接口按 prefix 服务端过滤）。只读展示，返回按钮回到对话列表。
 */

interface TreeNode extends WorkspaceNode {
  children: TreeNode[];
}

/** 扁平路径列表 → 嵌套树；父节点缺失（500 截断等）时挂到根，保证不丢节点。 */
function buildTree(items: WorkspaceNode[]): TreeNode[] {
  const sorted = [...items].sort((a, b) => a.path.localeCompare(b.path));
  const byPath = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];
  for (const item of sorted) {
    const node: TreeNode = { ...item, children: [] };
    byPath.set(item.path, node);
    const parentPath = item.path.slice(0, item.path.lastIndexOf('/'));
    const parent = parentPath ? byPath.get(parentPath) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

function TreeRows({ nodes, depth }: { nodes: TreeNode[]; depth: number }) {
  const { t } = useTranslation('layout');
  // 目录默认展开：记录被手动折叠的路径
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  return (
    <>
      {nodes.map((node) => {
        const isDir = node.type === 'dir';
        const open = isDir && !collapsed.has(node.path);
        return (
          <div key={node.path}>
            {isDir ? (
              <button
                type="button"
                className="flex w-full items-center gap-1 rounded py-1 pr-2 text-left text-xs transition-colors hover:bg-muted/60"
                style={{ paddingLeft: 4 + depth * 12 }}
                aria-expanded={open}
                onClick={() => toggle(node.path)}
              >
                {open ? (
                  <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
                ) : (
                  <ChevronRight className="size-3 shrink-0 text-muted-foreground" />
                )}
                {open ? (
                  <FolderOpen className="size-3.5 shrink-0 text-muted-foreground" />
                ) : (
                  <Folder className="size-3.5 shrink-0 text-muted-foreground" />
                )}
                <span className="truncate">{node.name}</span>
              </button>
            ) : (
              <div
                className="flex items-center gap-1 py-1 pr-2 text-xs text-muted-foreground"
                style={{ paddingLeft: 16 + depth * 12 }}
                title={t('chat.fileSize', { size: node.size })}
              >
                <FileText className="size-3.5 shrink-0" />
                <span className="truncate">{node.name}</span>
              </div>
            )}
            {isDir && open && node.children.length > 0 && (
              <TreeRows nodes={node.children} depth={depth + 1} />
            )}
          </div>
        );
      })}
    </>
  );
}

export function ProjectFiles(props: {
  project: AgentProjectItem;
  onBack: () => void;
}) {
  const { t } = useTranslation('layout');
  const tree = useWorkspaceTree(props.project.path);
  // 子树含项目目录本身（path == prefix），根层仅展示其子节点
  const roots = useMemo(
    () => buildTree((tree.data ?? []).filter((n) => n.path !== props.project.path)),
    [tree.data, props.project.path],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-1 px-2 pb-1">
        <Button
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          aria-label={t('chat.backToChats')}
          onClick={props.onBack}
        >
          <ArrowLeft className="size-4" />
        </Button>
        <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium">
          <FolderOpen className="size-4 shrink-0 text-muted-foreground" />
          <span className="truncate">{props.project.path}</span>
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {tree.isLoading ? (
          <div className="flex justify-center py-4">
            <Spinner className="size-4 text-muted-foreground" />
          </div>
        ) : tree.isError ? (
          <p className="px-2 py-2 text-xs text-destructive">{tree.error.message}</p>
        ) : roots.length === 0 ? (
          <p className="px-2 py-2 text-xs text-muted-foreground">{t('chat.filesEmpty')}</p>
        ) : (
          <div className={cn('space-y-0.5')}>
            <TreeRows nodes={roots} depth={0} />
          </div>
        )}
      </div>
    </div>
  );
}
