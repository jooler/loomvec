import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Folder, FolderPlus } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@loomvec/ui/components/ui/button';
import { Input } from '@loomvec/ui/components/ui/input';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { useOpenProject, useWorkspaceTree, type AgentProjectItem } from '@/hooks';

/**
 * 新建项目弹框（P5.6）：打开 workspace 根下已有文件夹，或新建一个文件夹。
 * 项目即「已打开的一级目录」，会话经项目行内的新建按钮绑定该目录。
 */
export function ProjectDialog(props: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projects: AgentProjectItem[];
}) {
  const { t } = useTranslation('layout');
  const [name, setName] = useState('');
  const openProject = useOpenProject();
  // 根目录树（prefix 空）：取一级目录供「打开已有文件夹」
  const tree = useWorkspaceTree('', props.open);

  const open = (folderName: string) => {
    openProject.mutate(folderName, {
      onSuccess: () => {
        setName('');
        props.onOpenChange(false);
      },
      onError: (e) => toast.error(e.message),
    });
  };

  // 根下一级、且尚未打开为项目的目录
  const candidates = useMemo(
    () =>
      (tree.data ?? []).filter(
        (n) =>
          n.type === 'dir' &&
          !n.path.includes('/') &&
          !props.projects.some((p) => p.path === n.path),
      ),
    [tree.data, props.projects],
  );

  const draft = name.trim();
  const invalid = draft.includes('/') || draft.startsWith('.');

  return (
    <Dialog
      open={props.open}
      onOpenChange={(o) => {
        if (!o) setName('');
        props.onOpenChange(o);
      }}
    >
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t('project.createTitle')}</DialogTitle>
          <DialogDescription>{t('project.createDesc')}</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2">
          <Input
            value={name}
            placeholder={t('project.namePlaceholder')}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing && draft && !invalid) open(draft);
            }}
          />
          <Button
            disabled={!draft || invalid || openProject.isPending}
            onClick={() => open(draft)}
          >
            {openProject.isPending && <Spinner className="size-4" />}
            <FolderPlus className="size-4" />
            {t('project.createAndOpen')}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">{t('project.existingLabel')}</p>
        <div className="max-h-56 space-y-0.5 overflow-y-auto rounded-md border p-1">
          {tree.isLoading ? (
            <div className="flex justify-center py-4">
              <Spinner className="size-4 text-muted-foreground" />
            </div>
          ) : tree.isError ? (
            <p className="px-2 py-2 text-xs text-destructive">{tree.error.message}</p>
          ) : candidates.length === 0 ? (
            <p className="px-2 py-2 text-xs text-muted-foreground">{t('project.noExisting')}</p>
          ) : (
            candidates.map((d) => (
              <button
                key={d.path}
                type="button"
                className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left transition-colors hover:bg-muted/60"
                disabled={openProject.isPending}
                onClick={() => open(d.path)}
              >
                <Folder className="size-3.5 shrink-0 text-muted-foreground" />
                <span className="truncate font-mono text-xs">{d.path}</span>
              </button>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
