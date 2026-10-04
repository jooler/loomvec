import { ChevronDown, ChevronRight, FolderTree } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { cn } from 'cn';
import type { KnowledgeBaseNode } from '@/hooks';

type TreeNode = KnowledgeBaseNode & { children: TreeNode[] };

function buildTree(nodes: KnowledgeBaseNode[]): TreeNode[] {
  const byId = new Map(nodes.map((node) => [node.id, { ...node, children: [] as TreeNode[] }]));
  const roots: TreeNode[] = [];
  for (const node of byId.values()) {
    const parent = node.parent_id ? byId.get(node.parent_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const sort = (items: TreeNode[]) => {
    items.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
    items.forEach((item) => sort(item.children));
  };
  sort(roots);
  return roots;
}

function TreeBranch({
  node,
  depth,
  selectedId,
  onSelect,
}: {
  node: TreeNode;
  depth: number;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(true);
  const hasChildren = node.children.length > 0;
  return (
    <div>
      <div className="flex items-center gap-1">
        <button
          type="button"
          className="grid size-6 place-items-center rounded hover:bg-muted"
          style={{ marginLeft: depth * 14 }}
          onClick={() => hasChildren && setOpen((value) => !value)}
          aria-label={hasChildren ? (open ? '收起' : '展开') : undefined}
        >
          {hasChildren ? (
            open ? (
              <ChevronDown className="size-3.5" />
            ) : (
              <ChevronRight className="size-3.5" />
            )
          ) : null}
        </button>
        <button
          type="button"
          className={cn(
            'flex min-w-0 flex-1 items-center gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-muted',
            selectedId === node.id && 'bg-primary/10 font-medium text-primary',
          )}
          onClick={() => onSelect(node.id)}
        >
          <FolderTree className="size-4 shrink-0" />
          <span className="truncate">{node.name}</span>
        </button>
      </div>
      {open &&
        node.children.map((child) => (
          <TreeBranch
            key={child.id}
            node={child}
            depth={depth + 1}
            selectedId={selectedId}
            onSelect={onSelect}
          />
        ))}
    </div>
  );
}

export function KnowledgeBaseTree({
  nodes,
  selectedId,
  onSelect,
}: {
  nodes: KnowledgeBaseNode[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const { t } = useTranslation('assets');
  const roots = useMemo(() => buildTree(nodes), [nodes]);
  return (
    <aside className="w-56 shrink-0 overflow-y-auto rounded-md border bg-card p-2">
      <button
        type="button"
        className={cn(
          'mb-1 flex w-full items-center gap-2 rounded px-2 py-2 text-left text-sm hover:bg-muted',
          selectedId === null && 'bg-primary/10 font-medium text-primary',
        )}
        onClick={() => onSelect(null)}
      >
        <FolderTree className="size-4" />
        {t('allKnowledgeBases')}
      </button>
      {roots.map((node) => (
        <TreeBranch
          key={node.id}
          node={node}
          depth={0}
          selectedId={selectedId}
          onSelect={(id) => onSelect(id)}
        />
      ))}
      {roots.length === 0 && (
        <p className="px-2 py-3 text-xs text-muted-foreground">{t('noKnowledgeBases')}</p>
      )}
    </aside>
  );
}
