import { NavLink, Outlet, useLocation, useParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { api, unwrap } from '@/api';
import { StatusBadge } from '@loomvec/ui/components/status-badge';
import { Spinner } from '@loomvec/ui/components/ui/spinner';
import { cn } from 'cn';

/**
 * 公共空间管理布局（运营端，对齐用户端 SpaceLayout 的页签组织）：
 * 资产 / 审核 / 图谱 + 可见性 / 设置。用户端的「成员」页签在运营端
 * 语义替换为「可见性」（勾选用户分组 = 空间对该分组公开）；
 * 无「检索」「通知」「对话」——运营端是纯知识库维护业务（docs/12）。
 */

const MAIN_TABS = [
  { to: 'assets', label: '资产' },
  { to: 'review', label: '审核' },
  { to: 'graph', label: '图谱' },
];

const ADMIN_TABS = [
  { to: 'visibility', label: '可见性' },
  { to: 'settings', label: '设置' },
];

interface OpsSpaceDetail {
  id: string;
  name: string;
  description: string | null;
  review_required: boolean;
}

export function SpaceLayout() {
  const { spaceId } = useParams<{ spaceId: string }>();
  const location = useLocation();
  const current = location.pathname.split('/').pop() ?? '';

  const space = useQuery({
    queryKey: ['ops-space', spaceId],
    queryFn: () =>
      unwrap<OpsSpaceDetail>(
        api.GET('/api/v1/ops/spaces/{space_id}', { params: { path: { space_id: spaceId! } } }),
      ),
    enabled: !!spaceId,
  });

  const tabClass = (to: string) =>
    cn(
      '-mb-px border-b-2 px-3 py-2 text-sm transition-colors',
      current === to
        ? 'border-primary font-medium text-foreground'
        : 'border-transparent text-muted-foreground hover:text-foreground',
    );

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        {space.isLoading ? (
          <Spinner className="size-5 text-muted-foreground" />
        ) : (
          <h1 className="truncate text-lg font-semibold">{space.data?.name ?? '公共空间'}</h1>
        )}
        {space.data && <StatusBadge tone="purple">公共空间</StatusBadge>}
        {space.data?.review_required && <StatusBadge tone="amber">需审核</StatusBadge>}
      </div>
      <nav className="flex items-center gap-1 border-b">
        {MAIN_TABS.map((t) => (
          <NavLink key={t.to} to={t.to} className={() => tabClass(t.to)}>
            {t.label}
          </NavLink>
        ))}
        <div className="ml-auto flex items-center gap-1">
          {ADMIN_TABS.map((t) => (
            <NavLink key={t.to} to={t.to} className={() => tabClass(t.to)}>
              {t.label}
            </NavLink>
          ))}
        </div>
      </nav>
      <Outlet />
    </div>
  );
}
