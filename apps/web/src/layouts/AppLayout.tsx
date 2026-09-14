import { Bell, ChevronDown, FolderKanban, Search } from 'lucide-react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router';
import { useAuth } from '@/auth';
import { useNotifications } from '@/hooks';
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
import { ThemeToggle } from '@loomvec/ui/components/mode-toggle';
import { ChatSessions } from '@/layouts/chat-sessions';
import { cn } from 'cn';

/** 常规导航条目：固定在侧栏底部（对话区之上不设条目，对话经侧栏上部会话列表直达）。 */
const NAV_ITEMS = [
  {
    to: '/spaces',
    label: '空间管理',
    icon: FolderKanban,
    match: (p: string) => p === '/spaces' || p.startsWith('/s/'),
  },
  { to: '/search', label: '检索', icon: Search, match: (p: string) => p.startsWith('/search') },
  {
    to: '/notifications',
    label: '通知',
    icon: Bell,
    match: (p: string) => p.startsWith('/notifications'),
    showUnread: true,
  },
];

/**
 * 用户端布局：侧栏上部为对话区（新建对话 + 历史会话，支持重命名 / 删除，见
 * ChatSessions），底部为空间管理 / 检索 / 通知导航与用户菜单（明暗切换靠右）。
 * 对话为常驻入口，不再单列条目，也不依赖当前空间。对话路由下主区域为全高聊天
 * 界面，其余页面保持 max-w 文档流布局。通知为独立页面管理全部通知。
 */
export function AppLayout() {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // 30s 轮询：仅供侧栏「通知」条目的未读徽标
  const notifications = useNotifications();
  const unread = notifications.data?.unread_count ?? 0;

  // 对话路由下主区域为全高聊天界面，不做 max-w 文档包装
  const isChatRoute =
    location.pathname === '/chat' || location.pathname.startsWith('/chat/');

  return (
    <div className="flex min-h-svh bg-muted/30">
      <aside className="sticky top-0 flex h-svh w-56 shrink-0 flex-col border-r bg-background">
        <div className="flex h-14 items-center px-4">
          <Button
            variant="ghost"
            className="px-2 font-semibold"
            onClick={() => navigate('/')}
          >
            LoomVec
          </Button>
        </div>
        {/* 对话区：新建对话 + 历史会话（重命名 / 删除） */}
        <ChatSessions />
        <nav className="space-y-1 border-t px-2 py-2">
          {NAV_ITEMS.map(({ to, label, icon: Icon, match, showUnread }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors',
                  isActive || match(location.pathname)
                    ? 'bg-muted font-medium'
                    : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
                )
              }
            >
              <Icon className="size-4" />
              {label}
              {showUnread && unread > 0 && (
                <Badge className="ml-auto h-4 min-w-4 rounded-full px-1 text-[10px]">
                  {unread}
                </Badge>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="flex items-center gap-1 border-t p-2">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" className="h-9 min-w-0 flex-1 justify-start gap-1.5 px-2">
                <Avatar className="size-6">
                  <AvatarFallback className="text-xs">
                    {(me?.username ?? '?').slice(0, 1).toUpperCase()}
                  </AvatarFallback>
                </Avatar>
                <span className="truncate text-sm">{me?.username ?? '未登录'}</span>
                <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-44">
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
          {/* 明暗切换：固定在底栏右侧 */}
          <ThemeToggle />
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        {isChatRoute ? (
          <Outlet />
        ) : (
          <div className="mx-auto max-w-6xl p-6">
            <Outlet />
          </div>
        )}
      </main>
    </div>
  );
}
