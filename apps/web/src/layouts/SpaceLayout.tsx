import { NavLink, Outlet, useNavigate, useParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ArrowLeft } from 'lucide-react';
import { useSpace } from '@/hooks';
import { ROLE_TONE } from '@/utils';
import { Button } from '@loomvec/ui/components/ui/button';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { cn } from 'cn';

/**
 * 空间详情布局：空间管理卡片进入后的容器。左侧页签组织资产 / 检索 / 图谱 / 审核，
 * 成员 / 设置作为独立页签放在页签栏右侧。viewer 只读：owner/editor 专属页签不渲染。
 * 公共空间经分组可见时以虚拟 viewer 进入，同样只读。
 */
export function SpaceLayout() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const navigate = useNavigate();
  const spaceQuery = useSpace(spaceId);
  const { t } = useTranslation('layout');
  const space = spaceQuery.data;
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
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={t('backToSpaces')}
          title={t('backToSpaces')}
          onClick={() => navigate('/spaces')}
        >
          <ArrowLeft />
        </Button>
        {spaceQuery.isLoading ? (
          <Spinner className="size-5 text-muted-foreground" />
        ) : (
          <h1 className="truncate text-lg font-semibold">{space?.name ?? t('spaceFallback')}</h1>
        )}
        {space && (
          <StatusBadge tone={ROLE_TONE[role]}>
            {t(`role.${role}`, { defaultValue: role })}
          </StatusBadge>
        )}
        {space?.space_type === 'public' && <StatusBadge tone="purple">{t('publicBadge')}</StatusBadge>}
        {space?.review_required && <StatusBadge tone="amber">{t('reviewRequired')}</StatusBadge>}
      </div>
      {spaceQuery.isError ? (
        <div className="space-y-3 rounded-md border border-destructive/30 bg-destructive/5 p-6">
          <p className="text-sm text-destructive">
            {(spaceQuery.error as Error).message || t('spaceAccessDenied')}
          </p>
          <Button variant="outline" size="sm" onClick={() => navigate('/spaces')}>
            {t('backToSpaces')}
          </Button>
        </div>
      ) : (
        <>
          <nav className="flex items-center gap-1 border-b">
            {mainTabs.map((tab) => (
              <NavLink key={tab.to} to={tab.to} className={tabClass}>
                {tab.label}
              </NavLink>
            ))}
            {adminTabs.length > 0 && (
              <div className="ml-auto flex items-center gap-1">
                {adminTabs.map((tab) => (
                  <NavLink key={tab.to} to={tab.to} className={tabClass}>
                    {tab.label}
                  </NavLink>
                ))}
              </div>
            )}
          </nav>
          <Outlet />
        </>
      )}
    </div>
  );
}
