/**
 * 运营端信息架构（P5 对齐 docs/12 运营端与公共空间设计）。
 *
 * 与 admin 不同：运营端左侧栏的主体是"公共空间"动态列表（OpsLayout 从
 * /api/v1/ops/spaces 取数渲染，点击进入对应空间管理界面），本文件只声明
 * 固定导航项（总览 / 用户分组）与面包屑工具。
 */
import { LayoutDashboard, Users, type LucideIcon } from 'lucide-react';
import { t } from '@/i18n';

export interface OpsMenuItem {
  path: string;
  name: string;
  icon?: LucideIcon;
}

// 模块级文案（菜单名）：main.tsx 已先初始化 i18n，import 阶段取值安全
export const OPS_MENU: OpsMenuItem[] = [
  { path: '/overview', name: t('layout:menu.overview'), icon: LayoutDashboard },
  { path: '/groups', name: t('layout:menu.groups'), icon: Users },
];

/** 面包屑展示名（空间路由由 OpsLayout 动态注入空间名）。 */
export function staticMenuName(pathname: string): string | null {
  const item = OPS_MENU.find((m) => m.path === pathname);
  if (item) return item.name;
  if (pathname.startsWith('/s/')) return t('layout:breadcrumb.publicSpace');
  if (pathname.startsWith('/groups')) return t('layout:menu.groups');
  return null;
}

/** 菜单 + 空间路由的路径守卫清单（测试用）。 */
export const OPS_ROUTE_PREFIXES = ['/overview', '/groups', '/s/'];
