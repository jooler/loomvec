/**
 * 运维端信息架构菜单（P4 对齐 docs/04 运维前端设计 §四，含子路由）。
 * superAdminOnly 的页面（系统配置）仅 super_admin 可见——角色双闸之路由闸。
 */
import {
  Bot,
  Boxes,
  FileCheck2,
  FileSearch,
  HeartPulse,
  LayoutDashboard,
  Plug,
  Settings,
  Users,
  Workflow,
  type LucideIcon,
} from 'lucide-react';
import { t } from '@/i18n';

export interface AdminMenuItem {
  path: string;
  name: string;
  icon?: LucideIcon;
  /** 子路由（侧边栏二级菜单） */
  children?: AdminMenuItem[];
  /** 仅 super_admin 可见 */
  superAdminOnly?: boolean;
}

export const ADMIN_MENU: AdminMenuItem[] = [
  { path: '/overview', name: t('layout:menu.overview'), icon: LayoutDashboard },
  { path: '/tenants', name: t('layout:menu.tenants'), icon: Boxes },
  { path: '/users', name: t('layout:menu.users'), icon: Users },
  { path: '/spaces', name: t('layout:menu.spaces'), icon: Boxes },
  { path: '/reviews', name: t('layout:menu.reviews'), icon: FileCheck2 },
  { path: '/pipeline', name: t('layout:menu.pipeline'), icon: Workflow },
  {
    // 分组锚点仅作菜单 key，不得与任何子路由 path 相同（否则菜单 key 冲突）
    path: '/models-group',
    name: t('layout:menu.modelsGroup'),
    icon: Bot,
    children: [
      { path: '/models', name: t('layout:menu.models') },
      { path: '/retrieval', name: t('layout:menu.retrieval') },
    ],
  },
  {
    path: '/open',
    name: t('layout:menu.open'),
    icon: Plug,
    children: [
      { path: '/open/api-keys', name: t('layout:menu.apiKeys') },
      { path: '/open/oauth', name: t('layout:menu.oauth') },
      { path: '/open/webhooks', name: t('layout:menu.webhooks') },
    ],
  },
  {
    path: '/settings',
    name: t('layout:menu.settings'),
    icon: Settings,
    superAdminOnly: true,
    children: [
      { path: '/settings/ai', name: t('layout:menu.settingsAi') },
      { path: '/settings/retrieval', name: t('layout:menu.settingsRetrieval') },
      { path: '/settings/upload', name: t('layout:menu.settingsUpload') },
      { path: '/settings/sso', name: t('layout:menu.settingsSso') },
      { path: '/settings/extensions', name: t('layout:menu.settingsExtensions') },
    ],
  },
  { path: '/audit', name: t('layout:menu.audit'), icon: FileSearch },
  { path: '/system', name: t('layout:menu.system'), icon: HeartPulse },
];

/** 菜单树展开为路径列表（测试/守卫用）；有子菜单时父级仅为分组锚点，不再计入。 */
export function flattenMenuPaths(items: AdminMenuItem[] = ADMIN_MENU): string[] {
  return items.flatMap((m) => (m.children ? flattenMenuPaths(m.children) : [m.path]));
}

/** 按当前路径求面包屑轨迹：精确匹配优先，其次最长前缀匹配（详情页 /tenants/:id → 租户管理）。 */
export function findMenuTrail(pathname: string): AdminMenuItem[] {
  for (const m of ADMIN_MENU) {
    if (!m.children) {
      if (m.path === pathname) return [m];
    } else {
      const child = m.children.find((c) => c.path === pathname);
      if (child) return [m, child];
    }
  }
  let best: AdminMenuItem[] = [];
  for (const m of ADMIN_MENU) {
    if (!m.children) {
      if (pathname.startsWith(m.path + '/') && m.path.length > (best[0]?.path.length ?? 0)) {
        best = [m];
      }
    } else {
      for (const c of m.children) {
        if (
          pathname.startsWith(c.path + '/') &&
          c.path.length > (best[best.length - 1]?.path.length ?? 0)
        ) {
          best = [m, c];
        }
      }
    }
  }
  return best;
}
