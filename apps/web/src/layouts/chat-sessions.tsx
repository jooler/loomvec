import { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Folder,
  FolderPlus,
  FolderTree,
  MessageSquarePlus,
  MoreHorizontal,
  Pencil,
  Trash2,
} from 'lucide-react';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@loomvec/ui/components/ui/dropdown-menu';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@loomvec/ui/components/ui/alert-dialog';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@loomvec/ui/components/ui/dialog';
import { Input } from '@loomvec/ui/components/ui/input';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import {
  useAgentProjects,
  useAgentSessions,
  useRemoveProject,
  type AgentProjectItem,
  type ChatSessionItem,
} from '@/hooks';
import { extractApiError } from '@/utils';
import { ProjectDialog } from '@/layouts/project-dialog';
import { ProjectFiles } from '@/layouts/project-files';
import { cn } from 'cn';

/**
 * 侧栏对话区（P5.6）：会话按项目分组（项目 = workspace 已打开的一级目录）。
 * 「新建项目」打开/新建目录；项目行悬停出现 [更多（移除）] [查看文件] [新建对话]。
 * 新建对话进入该项目的草稿对话页（不建会话），首条消息发出时才以问题前 60 字
 * 为标题创建真实会话并绑定项目目录。未命中任何已打开项目的会话（含历史无目录
 * 会话）收进「未分组」，保证数据不丢。
 */

/** 每组默认展示的会话数，超出折叠进「显示更多」。 */
const GROUP_PREVIEW = 4;

/** 「未分组」在折叠/展开状态中的键（项目键用 UUID，不会与之冲突）。 */
const UNGROUPED_KEY = '__ungrouped__';

/** 相对时间（侧栏会话行右缘；ZCode 风格「刚刚 / 12分 / 4小时」）。 */
function relativeTime(iso: string | null | undefined, t: TFunction<'layout'>): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const minutes = Math.floor((Date.now() - then) / 60_000);
  if (minutes < 1) return t('chat.timeJustNow');
  if (minutes < 60) return t('chat.timeMinutes', { count: minutes });
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t('chat.timeHours', { count: hours });
  const days = Math.floor(hours / 24);
  if (days < 30) return t('chat.timeDays', { count: days });
  return new Date(then).toLocaleDateString();
}

interface ProjectGroup {
  project: AgentProjectItem;
  sessions: ChatSessionItem[];
}

export function ChatSessions() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const { t } = useTranslation('layout');
  const sessions = useAgentSessions();
  const projects = useAgentProjects();
  const removeProject = useRemoveProject();

  // 视图态：文件树视图目标 / 新建项目弹框 / 移除确认
  const [filesProject, setFilesProject] = useState<AgentProjectItem | null>(null);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<AgentProjectItem | null>(null);
  // 项目分组折叠（点击项目行）与「显示更多」展开
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());
  // 会话行内操作（重命名 / 删除）沿用旧交互
  const [renaming, setRenaming] = useState<ChatSessionItem | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [deleting, setDeleting] = useState<ChatSessionItem | null>(null);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });

  // 列表查询失败给全局提示（否则侧栏静默变空、会话全部落入「未分组」）
  useEffect(() => {
    if (projects.error) toast.error(projects.error.message);
  }, [projects.error]);

  useEffect(() => {
    if (sessions.error) toast.error(sessions.error.message);
  }, [sessions.error]);

  // 新建对话 → 进入该项目的「草稿对话」页（仅 UI，不建会话）；首条消息发出时
  // 才创建真实会话。同一草稿页重复点击直接忽略，始终只维持一个草稿 UI。
  const startDraftChat = (projectPath: string) => {
    const target = `/chat?project=${encodeURIComponent(projectPath)}`;
    if (`${location.pathname}${location.search}` === target) return;
    navigate(target);
  };

  const rename = useMutation({
    mutationFn: async ({ id, title }: { id: string; title: string }) => {
      const { error } = await api.PATCH('/api/v1/agent/sessions/{session_id}', {
        params: { path: { session_id: id } },
        body: { title },
      });
      if (error) throw new Error(extractApiError(error, t('finderData.renameFailed')));
    },
    onSuccess: () => {
      setRenaming(null);
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.DELETE('/api/v1/agent/sessions/{session_id}', {
        params: { path: { session_id: id } },
      });
      if (error) throw new Error(extractApiError(error, t('chat.deleteFailed')));
    },
    onSuccess: (_, id) => {
      setDeleting(null);
      // 清掉被删会话的消息缓存，避免同名 queryKey 残留脏数据
      queryClient.removeQueries({ queryKey: ['agent-messages', id] });
      if (location.pathname === `/chat/${id}`) navigate('/chat');
      invalidate();
    },
    onError: (e) => {
      setDeleting(null);
      toast.error(e.message);
    },
  });

  const groups = useMemo<ProjectGroup[]>(() => {
    const items = sessions.data?.items ?? [];
    const projectList = projects.data ?? [];
    const byProject = new Map<string, ChatSessionItem[]>(projectList.map((p) => [p.id, []]));
    for (const s of items) {
      // 归属最长匹配前缀：嵌套项目（a 与 a/b）同时打开时，会话只进最具体的一组
      let best: AgentProjectItem | undefined;
      for (const p of projectList) {
        const match = s.project_path === p.path || s.project_path.startsWith(`${p.path}/`);
        if (match && (!best || p.path.length > best.path.length)) best = p;
      }
      if (best) byProject.get(best.id)?.push(s);
    }
    return projectList.map((project) => ({
      project,
      sessions: byProject.get(project.id) ?? [],
    }));
  }, [sessions.data, projects.data]);

  // 未命中任何已打开项目的会话（无目录 / 项目被移除）→ 「未分组」
  const others = useMemo(() => {
    const claimed = new Set(groups.flatMap((g) => g.sessions.map((s) => s.session_id)));
    return (sessions.data?.items ?? []).filter((s) => !claimed.has(s.session_id));
  }, [groups, sessions.data]);

  const toggleCollapsed = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const confirmRemove = () => {
    if (!removing) return;
    const target = removing;
    removeProject.mutate(target.id, {
      onSuccess: () => {
        setRemoving(null);
        if (filesProject?.id === target.id) setFilesProject(null);
      },
    });
  };

  const renderSessionRow = (s: ChatSessionItem) => {
    const active = location.pathname === `/chat/${s.session_id}`;
    return (
      <div key={s.session_id} className="group relative flex items-center">
        <button
          type="button"
          className={cn(
            'min-w-0 flex-1 truncate rounded-md py-2 pl-6 pr-14 text-left text-sm transition-colors',
            active
              ? 'bg-muted font-medium'
              : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
          )}
          onClick={() => navigate(`/chat/${s.session_id}`)}
        >
          {s.title || t('chat.untitled')}
        </button>
        <span
          className={cn(
            'pointer-events-none absolute right-9 text-[10px] text-muted-foreground transition-opacity group-hover:opacity-0',
            active && 'text-foreground/70',
          )}
        >
          {relativeTime(s.last_message_at, t)}
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              aria-label={t('chat.moreActions', { title: s.title || t('chat.untitled') })}
              className={cn(
                'absolute top-1/2 right-1 size-6 -translate-y-1/2 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100',
                active && 'opacity-100',
              )}
            >
              <MoreHorizontal className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-32">
            <DropdownMenuItem
              onClick={() => {
                setRenaming(s);
                setRenameDraft(s.title);
              }}
            >
              <Pencil />
              {t('finder.rename')}
            </DropdownMenuItem>
            <DropdownMenuItem variant="destructive" onClick={() => setDeleting(s)}>
              <Trash2 />
              {t('action.delete')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    );
  };

  const renderGroup = ({ project, sessions: items }: ProjectGroup) => {
    const isCollapsed = collapsed.has(project.id);
    const expanded = expandedGroups.has(project.id);
    const visible = expanded ? items : items.slice(0, GROUP_PREVIEW);
    return (
      <div key={project.id}>
        <div className="group relative flex items-center">
          <button
            type="button"
            className={cn(
              'flex min-w-0 flex-1 items-center gap-1.5 rounded-md py-1.5 pl-2 pr-[86px] text-left text-sm transition-colors',
              'hover:bg-muted/60',
            )}
            aria-expanded={!isCollapsed}
            onClick={() => toggleCollapsed(project.id)}
          >
            <Folder className="size-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{project.path}</span>
            <span className="shrink-0 text-[10px] text-muted-foreground">{items.length}</span>
          </button>
          <div className="absolute top-1/2 right-1 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6 text-muted-foreground"
                  aria-label={t('chat.projectMoreActions', { name: project.path })}
                >
                  <MoreHorizontal className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-32">
                <DropdownMenuItem variant="destructive" onClick={() => setRemoving(project)}>
                  <Trash2 />
                  {t('chat.removeProject')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <Button
              variant="ghost"
              size="icon"
              className="size-6 text-muted-foreground"
              aria-label={t('chat.viewFiles')}
              onClick={() => setFilesProject(project)}
            >
              <FolderTree className="size-4" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-6 text-muted-foreground"
              aria-label={t('chat.newChatInProject', { name: project.path })}
              onClick={() => startDraftChat(project.path)}
            >
              <MessageSquarePlus className="size-4" />
            </Button>
          </div>
        </div>
        {!isCollapsed && (
          <>
            <div className="space-y-0.5">{visible.map(renderSessionRow)}</div>
            {items.length > GROUP_PREVIEW && (
              <button
                type="button"
                className="w-full rounded-md py-1.5 pl-6 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
                onClick={() =>
                  setExpandedGroups((prev) => {
                    const next = new Set(prev);
                    if (next.has(project.id)) next.delete(project.id);
                    else next.add(project.id);
                    return next;
                  })
                }
              >
                {expanded ? t('chat.showLess') : t('chat.showMore')}
              </button>
            )}
          </>
        )}
      </div>
    );
  };

  const loading = projects.isLoading || sessions.isLoading;
  const isEmpty = !loading && groups.length === 0 && others.length === 0;

  return (
    <>
      <div className="px-2 pb-1">
        <Button className="w-full" onClick={() => setCreating(true)}>
          <FolderPlus className="size-4" />
          {t('chat.newProject')}
        </Button>
      </div>

      {filesProject ? (
        <ProjectFiles project={filesProject} onBack={() => setFilesProject(null)} />
      ) : (
        <nav className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-2">
          <p className="px-2 py-1 text-xs text-muted-foreground">{t('chat.projectsLabel')}</p>
          {loading && (
            <div className="flex justify-center py-4">
              <Spinner className="size-4 text-muted-foreground" />
            </div>
          )}
          {isEmpty && <p className="px-3 py-2 text-sm text-muted-foreground">{t('chat.noProjects')}</p>}
          {!loading && groups.map(renderGroup)}
          {!loading && others.length > 0 && (
            <div>
              <p className="px-2 py-1 text-xs text-muted-foreground">{t('chat.ungrouped')}</p>
              <div className="space-y-0.5">
                {(expandedGroups.has(UNGROUPED_KEY)
                  ? others
                  : others.slice(0, GROUP_PREVIEW)
                ).map(renderSessionRow)}
              </div>
              {others.length > GROUP_PREVIEW && (
                <button
                  type="button"
                  className="w-full rounded-md py-1.5 pl-6 text-left text-xs text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
                  onClick={() =>
                    setExpandedGroups((prev) => {
                      const next = new Set(prev);
                      if (next.has(UNGROUPED_KEY)) next.delete(UNGROUPED_KEY);
                      else next.add(UNGROUPED_KEY);
                      return next;
                    })
                  }
                >
                  {expandedGroups.has(UNGROUPED_KEY) ? t('chat.showLess') : t('chat.showMore')}
                </button>
              )}
            </div>
          )}
        </nav>
      )}

      {/* 新建项目（打开已有文件夹 / 新建文件夹） */}
      <ProjectDialog open={creating} onOpenChange={setCreating} projects={projects.data ?? []} />

      {/* 移除项目确认（软删：文件与会话保留） */}
      <AlertDialog
        open={!!removing}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('chat.removeProjectTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('chat.removeProjectDesc', { name: removing?.path })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('action.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={removeProject.isPending}
              onClick={(e) => {
                e.preventDefault(); // 移除成功后再收起
                confirmRemove();
              }}
            >
              {removeProject.isPending && <Spinner className="size-4" />}
              {t('chat.removeProject')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 会话重命名 */}
      <Dialog
        open={!!renaming}
        onOpenChange={(open) => {
          if (!open) setRenaming(null);
        }}
      >
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{t('chat.renameTitle')}</DialogTitle>
            <DialogDescription>{t('chat.renameDesc')}</DialogDescription>
          </DialogHeader>
          <Input
            value={renameDraft}
            maxLength={255}
            autoFocus
            onChange={(e) => setRenameDraft(e.target.value)}
            onKeyDown={(e) => {
              const title = renameDraft.trim();
              if (e.key === 'Enter' && !e.nativeEvent.isComposing && title && renaming) {
                rename.mutate({ id: renaming.session_id, title });
              }
            }}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setRenaming(null)}>
              {t('action.cancel')}
            </Button>
            <Button
              disabled={
                !renameDraft.trim() ||
                renameDraft.trim() === (renaming?.title ?? '') ||
                rename.isPending
              }
              onClick={() => {
                if (renaming) rename.mutate({ id: renaming.session_id, title: renameDraft.trim() });
              }}
            >
              {rename.isPending && <Spinner className="size-4" />}
              {t('action.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 会话删除确认 */}
      <AlertDialog
        open={!!deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('chat.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription>
              {t('chat.deleteDesc', { title: deleting?.title || t('chat.untitled') })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('action.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={remove.isPending}
              onClick={(e) => {
                // 阻止默认关闭，删除成功后再收起
                e.preventDefault();
                if (deleting) remove.mutate(deleting.session_id);
              }}
            >
              {remove.isPending && <Spinner className="size-4" />}
              {t('action.delete')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
