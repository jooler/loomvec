import { describe, expect, it } from 'vitest';
import { t } from '@/i18n';
import { OPS_MENU, OPS_ROUTE_PREFIXES, staticMenuName } from './menu';

describe('ops menu', () => {
  it('fixed menu items align with route prefixes', () => {
    for (const item of OPS_MENU) {
      expect(OPS_ROUTE_PREFIXES.some((p) => item.path.startsWith(p))).toBe(true);
    }
  });

  it('breadcrumb names resolve for space and group routes', () => {
    expect(staticMenuName('/groups')).toBe(t('layout:menu.groups'));
    expect(staticMenuName('/overview')).toBe(t('layout:menu.overview'));
    expect(staticMenuName('/s/any-id/assets')).toBe(t('layout:breadcrumb.publicSpace'));
    expect(staticMenuName('/unknown')).toBeNull();
  });
});
