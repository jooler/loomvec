import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { MessageSquarePlus, MoreHorizontal, Pencil, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
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
import { useAgentSessions, type ChatSessionItem } from '@/hooks';
import { extractApiError } from '@/utils';
import { cn } from 'cn';

/**
 * 侧栏对话区（P5）：新建对话 + 智能体会话列表（占满剩余高度、仅此区域滚动）。
 * 会话条目悬停出现「更多」菜单：重命名（PATCH title）、删除（确认后 DELETE，
 * 删除当前打开的会话时回到新对话页）。数据源为 agent 会话（dsh 全量接管）。
 */
export function ChatSessions() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const { t } = useTranslation('layout');
  const sessions = useAgentSessions();
  // 重命名弹窗目标会话 + 标题草稿；删除确认目标会话
  const [renaming, setRenaming] = useState<ChatSessionItem | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [deleting, setDeleting] = useState<ChatSessionItem | null>(null);

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ['agent-sessions'] });

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

  return (
    <>
      <div className="px-2 pb-1">
        <Button className="w-full" onClick={() => navigate('/chat')}>
          <MessageSquarePlus className="size-4" />
          {t('chat.newChat')}
        </Button>
      </div>
      {/* 会话列表：占满侧栏剩余高度，滚动仅限此区域 */}
      <nav className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
        {sessions.isLoading ? (
          <div className="flex justify-center py-4">
            <Spinner className="size-4 text-muted-foreground" />
          </div>
        ) : (
          (sessions.data?.items ?? []).map((s) => {
            const active = location.pathname === `/chat/${s.session_id}`;
            return (
              <div key={s.session_id} className="group relative flex items-center">
                <button
                  type="button"
                  className={cn(
                    'min-w-0 flex-1 truncate rounded-md py-2 pl-3 pr-8 text-left text-sm transition-colors',
                    active
                      ? 'bg-muted font-medium'
                      : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
                  )}
                  onClick={() => navigate(`/chat/${s.session_id}`)}
                >
                  {s.title || t('chat.untitled')}
                </button>
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
          })
        )}
        {sessions.data && (sessions.data.items?.length ?? 0) === 0 && (
          <p className="px-3 py-2 text-sm text-muted-foreground">{t('chat.noHistory')}</p>
        )}
      </nav>

      {/* 重命名弹窗 */}
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

      {/* 删除确认 */}
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
