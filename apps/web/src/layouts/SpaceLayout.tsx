import { NavLink, Outlet, useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useMySpaces } from '@/hooks';
import { ROLE_TONE } from '@/utils';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { cn } from 'cn';

/**
 * 空间详情布局：空间管理卡片进入后的容器。左侧页签组织资产 / 检索 / 图谱 / 审核，
 * 成员 / 设置作为独立页签放在页签栏右侧。viewer 只读：owner/editor 专属页签不渲染。
 */
export function SpaceLayout() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const spaces = useMySpaces();
  const { t } = useTranslation('layout');
  const space = (spaces.data ?? []).find((s) => s.id === spaceId);
  const role = space?.my_role ?? '';

  const mainTabs = [
    { to: 'assets', label: t('tab.assets') },
    { to: 'search', label: t('tab.search') },
    { to: 'graph', label: t('tab.graph') },
    ...(role === 'owner' || role === 'editor' ? [{ to: 'review', label: t('tab.review') }] : []),
  ];
  // 成员 / 设置：管理向页签，固定在页签栏右侧
  const adminTabs =
    role === 'owner'
      ? [
          { to: 'members', label: t('tab.members') },
          { to: 'settings', label: t('tab.settings') },
        ]
      : [];

  const tabClass = ({ isActive }: { isActive: boolean }) =>
    cn(
      '-mb-px border-b-2 px-3 py-2 text-sm transition-colors',
      isActive
        ? 'border-primary font-medium text-foreground'
        : 'border-transparent text-muted-foreground hover:text-foreground',
    );

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        {spaces.isLoading ? (
          <Spinner className="size-5 text-muted-foreground" />
        ) : (
          <h1 className="truncate text-lg font-semibold">{space?.name ?? t('spaceFallback')}</h1>
        )}
        {space && (
          <StatusBadge tone={ROLE_TONE[role]}>
            {t(`role.${role}`, { defaultValue: role })}
          </StatusBadge>
        )}
        {space?.review_required && <StatusBadge tone="amber">{t('reviewRequired')}</StatusBadge>}
      </div>
      <nav className="flex items-center gap-1 border-b">
        {mainTabs.map((t) => (
          <NavLink key={t.to} to={t.to} className={tabClass}>
            {t.label}
          </NavLink>
        ))}
        {adminTabs.length > 0 && (
          <div className="ml-auto flex items-center gap-1">
            {adminTabs.map((t) => (
              <NavLink key={t.to} to={t.to} className={tabClass}>
                {t.label}
              </NavLink>
            ))}
          </div>
        )}
      </nav>
      <Outlet />
    </div>
  );
}
