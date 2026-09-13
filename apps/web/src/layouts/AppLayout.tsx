import { Bell, ChevronDown, Search } from 'lucide-react';
import { useMemo } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api } from '@loomvec/sdk-ts';
import { useAuth } from '@/auth';
import { useMySpaces, useNotifications } from '@/hooks';
import { extractApiError } from '@/utils';
import { Avatar, AvatarFallback } from '@loomvec/ui/components/ui/avatar';
import { Badge } from '@loomvec/ui/components/ui/badge';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@loomvec/ui/components/ui/dropdown-menu';
import { Input } from '@loomvec/ui/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@loomvec/ui/components/ui/popover';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@loomvec/ui/components/ui/select';
import { Separator } from '@loomvec/ui/components/ui/separator';
import { ThemeToggle } from '@loomvec/ui/components/mode-toggle';
import { EmptyState } from '@loomvec/ui/components/empty-state';

const ALL_SPACES = '__all__';

/**
 * 用户端布局（P2）：顶部全局条 = 空间切换器 + 聚合搜索入口 + 通知铃铛（30s 轮询未读）+ 用户菜单。
 */
export function AppLayout() {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();

  const spaces = useMySpaces();
  const notifications = useNotifications();

  // 当前所在空间（/s/:spaceId/*），用于空间切换器回显
  const currentSpaceId = useMemo(() => {
    const m = location.pathname.match(/^\/s\/([^/]+)/);
    return m?.[1] ?? null;
  }, [location.pathname]);

  const markRead = useMutation({
    mutationFn: async (id: string) => {
      const { error } = await api.POST('/api/v1/notifications/{notification_id}/read', {
        params: { path: { notification_id: id } },
      });
      if (error) throw new Error(extractApiError(error, '标记已读失败'));
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notifications'] });
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
    onError: (e) => toast.error(e.message),
  });

  const markAllRead = useMutation({
    mutationFn: async () => {
      const { error } = await api.POST('/api/v1/notifications/read-all');
      if (error) throw new Error(extractApiError(error, '全部已读失败'));
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notifications'] });
      void queryClient.invalidateQueries({ queryKey: ['me'] });
    },
    onError: (e) => toast.error(e.message),
  });

  const searchTarget = currentSpaceId ? `/s/${currentSpaceId}/search` : '/search';

  return (
    <div className="min-h-svh bg-muted/30">
      <header className="sticky top-0 z-10 flex h-14 items-center gap-3 border-b bg-background px-6">
        <Button variant="ghost" className="px-2 font-semibold" onClick={() => navigate('/')}>
          LoomVec
        </Button>
        <Button variant="ghost" onClick={() => navigate('/spaces')}>
          我的空间
        </Button>
        {/* 空间切换器：切换空间即切换知识源，跳到该空间的资产页 */}
        <Select
          value={currentSpaceId ?? ALL_SPACES}
          onValueChange={(v) => {
            if (v === ALL_SPACES) navigate('/assets');
            else navigate(`/s/${v}/assets`);
          }}
        >
          <SelectTrigger className="w-52">
            <SelectValue placeholder={spaces.isLoading ? '加载中…' : '全部空间（聚合）'} />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_SPACES}>全部空间（聚合）</SelectItem>
            {(spaces.data ?? []).map((s) => (
              <SelectItem key={s.id} value={s.id}>
                {s.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {/* 聚合检索入口（只读输入框样式，点击跳转检索页） */}
        <button
          type="button"
          onClick={() => navigate(searchTarget)}
          className="relative hidden max-w-sm flex-1 cursor-pointer text-left sm:block"
        >
          <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            readOnly
            tabIndex={-1}
            placeholder="搜索全部空间（聚合检索）"
            className="cursor-pointer pl-8"
          />
        </button>
        <div className="ml-auto flex items-center gap-1">
          <ThemeToggle />
          {/* 通知铃铛：未读徽标 + 最近通知下拉 */}
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" className="relative" aria-label="通知">
                <Bell />
                {(notifications.data?.unread_count ?? 0) > 0 && (
                  <Badge className="absolute -top-0.5 -right-0.5 h-4 min-w-4 rounded-full px-1 text-[10px]">
                    {notifications.data!.unread_count}
                  </Badge>
                )}
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-80 p-0">
              <div className="max-h-80 overflow-y-auto p-1">
                {(notifications.data?.items ?? []).length === 0 ? (
                  <EmptyState title="暂无通知" className="py-6" />
                ) : (
                  (notifications.data?.items ?? []).slice(0, 8).map((n) => (
                    <button
                      key={n.id}
                      type="button"
                      className="block w-full max-w-full rounded-md px-2 py-2 text-left hover:bg-accent"
                      onClick={() => {
                        if (!n.read) markRead.mutate(n.id);
                      }}
                    >
                      <span className="flex items-center gap-1.5 text-sm">
                        {!n.read && <span className="size-1.5 shrink-0 rounded-full bg-blue-500" />}
                        <span className={n.read ? 'font-normal' : 'font-semibold'}>{n.title}</span>
                      </span>
                      <span className="mt-0.5 block text-xs text-muted-foreground">
                        {new Date(n.created_at).toLocaleString()}
                      </span>
                    </button>
                  ))
                )}
              </div>
              <Separator />
              <div className="flex items-center justify-between p-1">
                <Button variant="ghost" size="sm" onClick={() => markAllRead.mutate()}>
                  全部已读
                </Button>
                <Button variant="ghost" size="sm" asChild>
                  <Link to="/profile">查看全部（个人中心）</Link>
                </Button>
              </div>
            </PopoverContent>
          </Popover>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" className="h-8 gap-1.5 px-2">
                <Avatar className="size-6">
                  <AvatarFallback className="text-xs">
                    {(me?.username ?? '?').slice(0, 1).toUpperCase()}
                  </AvatarFallback>
                </Avatar>
                <span className="text-sm">{me?.username ?? '未登录'}</span>
                <ChevronDown className="size-3.5 text-muted-foreground" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={() => navigate('/profile')}>个人中心</DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={() => {
                  logout();
                  navigate('/login');
                }}
              >
                退出登录
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </header>
      <div className="mx-auto max-w-6xl p-6">
        <Outlet />
      </div>
    </div>
  );
}
