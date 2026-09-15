import { describe, expect, it } from 'vitest';
import { OPS_MENU, OPS_ROUTE_PREFIXES, staticMenuName } from './menu';

describe('ops menu', () => {
  it('固定导航项与路由前缀一致', () => {
    for (const item of OPS_MENU) {
      expect(OPS_ROUTE_PREFIXES.some((p) => item.path.startsWith(p))).toBe(true);
    }
  });

  it('空间路由与分组路由可求面包屑名', () => {
    expect(staticMenuName('/groups')).toBe('用户分组');
    expect(staticMenuName('/overview')).toBe('总览');
    expect(staticMenuName('/s/any-id/assets')).toBe('公共空间');
    expect(staticMenuName('/unknown')).toBeNull();
  });
});
