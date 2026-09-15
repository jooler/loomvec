import { FolderOpen, Plus, ShieldAlert } from 'lucide-react';
import { lazy, Suspense } from 'react';
import { LogOut, UserRound } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Link, Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router';
import { api } from './api';
import { OPS_ROLES, useAuth } from './auth';
import { OPS_MENU, staticMenuName } from './menu';
import { Avatar, AvatarFallback } from '@loomvec/ui/components/ui/avatar';
import { Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage } from '@loomvec/ui/components/ui/breadcrumb';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@loomvec/ui/components/ui/dropdown-menu';
import { Separator } from '@loomvec/ui/components/ui/separator';
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
} from '@loomvec/ui/components/ui/sidebar';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { ThemeToggle } from '@loomvec/ui/components/mode-toggle';
import { cn } from 'cn';

// 路由级代码分割：业务页面按需加载（登录页保持同步，保证首屏直出）。
const OverviewPage = lazy(() =>
  import('@/pages/overview/Overview').then((m) => ({ default: m.OverviewPage })),
);
const SpaceLayout = lazy(() =>
  import('@/pages/spaces/SpaceLayout').then((m) => ({ default: m.SpaceLayout })),
);
const AssetsPage = lazy(() =>
  import('@/pages/spaces/AssetsPage').then((m) => ({ default: m.AssetsPage })),
);
const ReviewPage = lazy(() =>
  import('@/pages/spaces/ReviewPage').then((m) => ({ default: m.ReviewPage })),
);
const GraphPage = lazy(() =>
  import('@/pages/spaces/GraphPage').then((m) => ({ default: m.GraphPage })),
);
const VisibilityPage = lazy(() =>
  import('@/pages/spaces/VisibilityPage').then((m) => ({ default: m.VisibilityPage })),
);
const SpaceSettingsPage = lazy(() =>
  import('@/pages/spaces/SpaceSettingsPage').then((m) => ({ default: m.SpaceSettingsPage })),
);
const GroupsPage = lazy(() =>
  import('@/pages/groups/GroupsPage').then((m) => ({ default: m.GroupsPage })),
);
const LoginPage = lazy(() =>
  import('@/pages/login/Login').then((m) => ({ default: m.LoginPage })),
);

function FullPageLoading() {
  return (
    <div className="grid min-h-svh place-items-center">
      <Spinner className="size-6 text-muted-foreground" />
    </div>
  );
}

/** 403 展示：无运营角色账号的出口（重新登录）。 */
function AccessDenied() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="grid min-h-svh place-items-center p-6">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        <div className="flex size-12 items-center justify-center rounded-full bg-muted">
          <ShieldAlert className="size-6 text-muted-foreground" />
        </div>
        <h2 className="text-lg font-semibold">无运营角色权限</h2>
        <p className="text-sm text-muted-foreground">
          运营端面向公共知识库的运营人员（operator / super_admin）。
          当前账号未被授予运营角色，请联系平台管理员在运维端分配。
        </p>
        <Button
          variant="outline"
          onClick={() => {
            logout();
            navigate('/login', { replace: true });
          }}
        >
          重新登录
        </Button>
      </div>
    </div>
  );
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { token, ready } = useAuth();
  if (!ready) return <FullPageLoading />;
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** 运营角色闸：me.roles 无 operator/super_admin 则拒绝（auditor/普通用户不可进）。 */
function RequireOpsRole({ children }: { children: React.ReactNode }) {
  const { me, ready } = useAuth();
  if (!ready) return <FullPageLoading />;
  const roles = me?.roles ?? [];
  if (!roles.some((r) => (OPS_ROLES as readonly string[]).includes(r))) {
    return <AccessDenied />;
  }
  return <>{children}</>;
}

/**
 * 侧边栏：固定导航（总览/用户分组）+ 公共空间动态列表（点击进入空间管理界面）。
 * 空间列表经 queryKey 失效即时更新（新建/删除空间后统一 invalidate ['ops-spaces']）。
 */
function OpsSidebar() {
  const location = useLocation();
  const pathname = location.pathname;
  const spaces = useQuery({
    queryKey: ['ops-spaces'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/v1/ops/spaces');
      if (error) throw new Error('加载公共空间失败');
      return data.items;
    },
    staleTime: 10_000,
  });

  return (
    <Sidebar>
      <SidebarHeader className="border-b px-4 py-3">
        <Link to="/overview" className="flex items-center gap-2 font-semibold">
          <span className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
            L
          </span>
          LoomVec 运营端
        </Link>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {OPS_MENU.map((item) => (
                <SidebarMenuItem key={item.path}>
                  <SidebarMenuButton asChild isActive={pathname === item.path}>
                    <Link to={item.path}>
                      {item.icon && <item.icon />}
                      {item.name}
                    </Link>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel className="flex items-center justify-between">
            公共空间
            <Button asChild size="xs" variant="ghost" className="h-6 gap-1 px-2">
              <Link to="/overview?create=1">
                <Plus /> 新建
              </Link>
            </Button>
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {spaces.isLoading ? (
                <div className="grid place-items-center py-4">
                  <Spinner className="size-4 text-muted-foreground" />
                </div>
              ) : (spaces.data?.length ?? 0) === 0 ? (
                <p className="px-2 py-1.5 text-xs text-muted-foreground">
                  还没有公共空间，点击「新建」创建
                </p>
              ) : (
                spaces.data?.map((s) => (
                  <SidebarMenuItem key={s.id}>
                    <SidebarMenuButton
                      asChild
                      isActive={pathname === `/s/${s.id}` || pathname.startsWith(`/s/${s.id}/`)}
                    >
                      <Link to={`/s/${s.id}`}>
                        <FolderOpen />
                        <span className="truncate">{s.name}</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  );
}

function OpsBreadcrumb() {
  const location = useLocation();
  const name = staticMenuName(location.pathname);
  return (
    <Breadcrumb>
      <BreadcrumbList>
        <BreadcrumbItem>
          <BreadcrumbPage>{name ?? 'LoomVec 运营端'}</BreadcrumbPage>
        </BreadcrumbItem>
      </BreadcrumbList>
    </Breadcrumb>
  );
}

function UserMenu() {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className="h-8 gap-2 px-2">
          <Avatar className="size-6">
            <AvatarFallback>
              <UserRound className="size-3.5" />
            </AvatarFallback>
          </Avatar>
          <span className="text-sm">{me?.username ?? '未登录'}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{me?.username}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => {
            logout();
            toast.success('已退出登录');
            navigate('/login');
          }}
        >
          <LogOut /> 退出登录
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** 运营端布局：shadcn Sidebar（固定导航 + 公共空间列表）+ 顶栏。 */
export function OpsLayout() {
  return (
    <SidebarProvider>
      <OpsSidebar />
      <SidebarInset>
        <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b bg-background px-4">
          <SidebarTrigger />
          <Separator orientation="vertical" className="!h-4" />
          <OpsBreadcrumb />
          <div className="ml-auto flex items-center gap-1">
            <ThemeToggle />
            <UserMenu />
          </div>
        </header>
        <main className={cn('flex-1 p-6')}>
          <Suspense
            fallback={
              <div className="grid min-h-[50vh] place-items-center">
                <Spinner className="size-5 text-muted-foreground" />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}

/** 运营端全部页面（docs/12 §五 IA）。 */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <RequireOpsRole>
              <OpsLayout />
            </RequireOpsRole>
          </RequireAuth>
        }
      >
        <Route path="/overview" element={<OverviewPage />} />
        <Route path="/s/:spaceId" element={<SpaceLayout />}>
          <Route index element={<Navigate to="assets" replace />} />
          <Route path="assets" element={<AssetsPage />} />
          <Route path="review" element={<ReviewPage />} />
          <Route path="graph" element={<GraphPage />} />
          <Route path="visibility" element={<VisibilityPage />} />
          <Route path="settings" element={<SpaceSettingsPage />} />
        </Route>
        <Route path="/groups" element={<GroupsPage />} />
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Route>
    </Routes>
  );
}
