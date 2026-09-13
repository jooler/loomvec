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
  { path: '/overview', name: '总览', icon: LayoutDashboard },
  { path: '/tenants', name: '租户管理', icon: Boxes },
  { path: '/users', name: '用户与权限', icon: Users },
  { path: '/spaces', name: '空间治理', icon: Boxes },
  { path: '/reviews', name: '内容审核', icon: FileCheck2 },
  { path: '/pipeline', name: '管线监控', icon: Workflow },
  {
    // 分组锚点仅作菜单 key，不得与任何子路由 path 相同（否则菜单 key 冲突）
    path: '/models-group',
    name: '模型与检索',
    icon: Bot,
    children: [
      { path: '/models', name: '重嵌入任务' },
      { path: '/retrieval', name: '索引状态' },
    ],
  },
  {
    path: '/open',
    name: '开放平台',
    icon: Plug,
    children: [
      { path: '/open/api-keys', name: 'API Key' },
      { path: '/open/oauth', name: 'OAuth 应用' },
      { path: '/open/webhooks', name: 'Webhook' },
    ],
  },
  {
    path: '/settings',
    name: '系统配置',
    icon: Settings,
    superAdminOnly: true,
    children: [
      { path: '/settings/ai', name: 'AI 供方' },
      { path: '/settings/retrieval', name: '检索参数' },
      { path: '/settings/upload', name: '上传策略' },
      { path: '/settings/sso', name: 'SSO/OIDC' },
      { path: '/settings/extensions', name: '扩展插件' },
    ],
  },
  { path: '/audit', name: '审计日志', icon: FileSearch },
  { path: '/system', name: '系统状态', icon: HeartPulse },
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
