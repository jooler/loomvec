import { describe, expect, it } from 'vitest';
import { ADMIN_MENU, flattenMenuPaths } from './menu';
import type { AdminMenuItem } from './menu';

describe('admin menu', () => {
  it('covers the IA routes from docs/04 §四', () => {
    const paths = flattenMenuPaths();
    expect(paths).toContain('/overview');
    expect(paths).toContain('/tenants');
    expect(paths).toContain('/users');
    expect(paths).toContain('/spaces');
    expect(paths).toContain('/reviews');
    expect(paths).toContain('/pipeline');
    expect(paths).toContain('/models');
    expect(paths).toContain('/retrieval');
    expect(paths).toContain('/open/api-keys');
    expect(paths).toContain('/open/oauth');
    expect(paths).toContain('/open/webhooks');
    expect(paths).toContain('/settings/ai');
    expect(paths).toContain('/settings/retrieval');
    expect(paths).toContain('/settings/upload');
    expect(paths).toContain('/settings/sso');
    expect(paths).toContain('/settings/extensions');
    expect(paths).toContain('/audit');
    expect(paths).toContain('/system');
  });

  it('has unique paths', () => {
    const paths = flattenMenuPaths();
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('has unique menu keys including parent group anchors', () => {
    // 侧边栏以 path 作 key（SidebarMenuItem/SidebarMenuSubItem），
    // 父级分组锚点与子路由 path 撞车会造成 React key 冲突
    const allPaths: string[] = [];
    const walk = (items: AdminMenuItem[]): void => {
      for (const m of items) {
        allPaths.push(m.path);
        if (m.children) walk(m.children);
      }
    };
    walk(ADMIN_MENU);
    expect(new Set(allPaths).size).toBe(allPaths.length);
  });

  it('marks system settings as super_admin only', () => {
    const settings = ADMIN_MENU.find((m) => m.path === '/settings');
    expect(settings?.superAdminOnly).toBe(true);
  });
});
