import { ChevronRight } from 'lucide-react';
import { Fragment, Suspense } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router';
import { LogOut, UserRound } from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { useAuth, usePerm } from '@/auth';
import { ADMIN_MENU, findMenuTrail } from '@/menu';
import type { AdminMenuItem } from '@/menu';
import { Avatar, AvatarFallback } from '@loomvec/ui/components/ui/avatar';
import { Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator } from '@loomvec/ui/components/ui/breadcrumb';
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
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarProvider,
  SidebarTrigger,
} from '@loomvec/ui/components/ui/sidebar';
import { cn } from 'cn';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { ThemeToggle } from '@loomvec/ui/components/mode-toggle';

/** 侧边栏菜单：按 docs/04 §四 IA，super_admin 过滤 + 二级子菜单。 */
function AppSidebar({ menu }: { menu: AdminMenuItem[] }) {
  const location = useLocation();
  const pathname = location.pathname;
  const { t } = useTranslation('layout');

  return (
    <Sidebar>
      <SidebarHeader className="border-b px-4 py-3">
        <Link to="/overview" className="flex items-center gap-2 font-semibold">
          <span className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
            L
          </span>
          {t('brand')}
        </Link>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {menu.map((item) =>
                item.children ? (
                  <SidebarMenuItem key={item.path}>
                    <p className="flex items-center gap-2 px-2 py-1.5 text-xs font-medium text-muted-foreground">
                      {item.icon && <item.icon className="size-4" />}
                      {item.name}
                    </p>
                    <SidebarMenuSub>
                      {item.children.map((child) => (
                        <SidebarMenuSubItem key={child.path}>
                          <SidebarMenuSubButton asChild isActive={pathname === child.path}>
                            <Link to={child.path}>{child.name}</Link>
                          </SidebarMenuSubButton>
                        </SidebarMenuSubItem>
                      ))}
                    </SidebarMenuSub>
                  </SidebarMenuItem>
                ) : (
                  <SidebarMenuItem key={item.path}>
                    <SidebarMenuButton asChild isActive={pathname === item.path}>
                      <Link to={item.path}>
                        {item.icon && <item.icon />}
                        {item.name}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ),
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  );
}

/** 顶部面包屑：按菜单树求轨迹，详情路由（如 /tenants/:id）走最长前缀匹配。 */
function AdminBreadcrumb() {
  const location = useLocation();
  const trail = findMenuTrail(location.pathname);
  if (trail.length === 0) return <span className="text-sm text-muted-foreground">LoomVec</span>;
  return (
    <Breadcrumb>
      <BreadcrumbList>
        {trail.map((item, i) => (
          // Fragment 保证 ol 的直接子元素是 li（BreadcrumbItem/Separator 均为 li）
          <Fragment key={item.path}>
            {i > 0 && <BreadcrumbSeparator className="[&>svg]:size-3.5" />}
            <BreadcrumbItem>
              {i === trail.length - 1 ? (
                <BreadcrumbPage>{item.name}</BreadcrumbPage>
              ) : (
                <Link to={item.path} className="text-sm text-muted-foreground transition-colors hover:text-foreground">
                  {item.name}
                </Link>
              )}
            </BreadcrumbItem>
          </Fragment>
        ))}
      </BreadcrumbList>
    </Breadcrumb>
  );
}

function UserMenu() {
  const { me, logout } = useAuth();
  const navigate = useNavigate();
  const { t } = useTranslation('layout');
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className="h-8 gap-2 px-2">
          <Avatar className="size-6">
            <AvatarFallback>
              <UserRound className="size-3.5" />
            </AvatarFallback>
          </Avatar>
          <span className="text-sm">{me?.username ?? t('notLoggedIn')}</span>
          <ChevronRight className="size-3.5 rotate-90 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{me?.username}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => {
            logout();
            toast.success(t('logoutSuccess'));
            navigate('/login');
          }}
        >
          <LogOut /> {t('logout')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** 运维端布局：shadcn Sidebar + 顶栏（面包屑 + 用户菜单），菜单按 docs/04 §四 IA。 */
export function AdminLayout() {
  const { isSuperAdmin } = usePerm();
  // 路由闸：系统配置组仅 super_admin 可见
  const menu = ADMIN_MENU.filter((m) => !m.superAdminOnly || isSuperAdmin);

  return (
    <SidebarProvider>
      <AppSidebar menu={menu} />
      <SidebarInset>
        <header className="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-2 border-b bg-background px-4">
          <SidebarTrigger />
          <Separator orientation="vertical" className="!h-4" />
          <AdminBreadcrumb />
          <div className="ml-auto flex items-center gap-1">
            <ThemeToggle />
            <UserMenu />
          </div>
        </header>
        <main className={cn('flex-1 p-6')}>
          {/* 懒加载页面的唯一 Suspense 边界：布局保持稳定，仅内容区出加载态 */}
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
