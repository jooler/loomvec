import { ShieldAlert } from 'lucide-react';
import { lazy } from 'react';
import { Navigate, Route, Routes, useNavigate } from 'react-router';
import { AdminLayout } from '@/layouts/AdminLayout';
import { LoginPage } from '@/pages/login/Login';
import { PLATFORM_ROLES, useAuth, usePerm } from '@/auth';
import { Button } from '@loomvec/ui/components/ui/button';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

// 路由级代码分割：业务页面按需加载（登录页保持同步，保证首屏直出）。
// Suspense 边界统一放在 AdminLayout 的内容区 Outlet 处，路由元素无需各自包裹。
const OverviewPage = lazy(() =>
  import('@/pages/overview/Overview').then((m) => ({ default: m.OverviewPage })),
);
const SystemPage = lazy(() =>
  import('@/pages/system/System').then((m) => ({ default: m.SystemPage })),
);
const TenantListPage = lazy(() =>
  import('@/pages/tenants/TenantList').then((m) => ({ default: m.TenantListPage })),
);
const TenantDetailPage = lazy(() =>
  import('@/pages/tenants/TenantDetail').then((m) => ({ default: m.TenantDetailPage })),
);
const UserListPage = lazy(() =>
  import('@/pages/users/UserList').then((m) => ({ default: m.UserListPage })),
);
const SpaceListPage = lazy(() =>
  import('@/pages/spaces/SpaceList').then((m) => ({ default: m.SpaceListPage })),
);
const SpaceDetailPage = lazy(() =>
  import('@/pages/spaces/SpaceDetail').then((m) => ({ default: m.SpaceDetailPage })),
);
const ReviewQueuePage = lazy(() =>
  import('@/pages/reviews/ReviewQueue').then((m) => ({ default: m.ReviewQueuePage })),
);
const ContentReviewPage = lazy(() =>
  import('@/pages/reviews/ContentReview').then((m) => ({ default: m.ContentReviewPage })),
);
const PipelineListPage = lazy(() =>
  import('@/pages/pipeline/PipelineList').then((m) => ({ default: m.PipelineListPage })),
);
const PipelineDetailPage = lazy(() =>
  import('@/pages/pipeline/PipelineDetail').then((m) => ({ default: m.PipelineDetailPage })),
);
const ModelsPage = lazy(() =>
  import('@/pages/models/Models').then((m) => ({ default: m.ModelsPage })),
);
const RetrievalPage = lazy(() =>
  import('@/pages/models/Retrieval').then((m) => ({ default: m.RetrievalPage })),
);
const ApiKeysPage = lazy(() =>
  import('@/pages/open/ApiKeys').then((m) => ({ default: m.ApiKeysPage })),
);
const OauthClientsPage = lazy(() =>
  import('@/pages/open/OauthClients').then((m) => ({ default: m.OauthClientsPage })),
);
const WebhooksPage = lazy(() =>
  import('@/pages/open/Webhooks').then((m) => ({ default: m.WebhooksPage })),
);
const SettingsPage = lazy(() =>
  import('@/pages/settings/Settings').then((m) => ({ default: m.SettingsPage })),
);
const AuditLogsPage = lazy(() =>
  import('@/pages/audit/AuditLogs').then((m) => ({ default: m.AuditLogsPage })),
);

function FullPageLoading() {
  return (
    <div className="grid min-h-svh place-items-center">
      <Spinner className="size-6 text-muted-foreground" />
    </div>
  );
}

/** 403 展示（替代 antd Result）。附带“重新登录”出口，避免无权限账号被困在本页。 */
function AccessDenied(props: { title: string; description: string }) {
  const navigate = useNavigate();
  const { logout } = useAuth();
  return (
    <div className="grid min-h-svh place-items-center p-6">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        <div className="flex size-12 items-center justify-center rounded-full bg-muted">
          <ShieldAlert className="size-6 text-muted-foreground" />
        </div>
        <h2 className="text-lg font-semibold">{props.title}</h2>
        <p className="text-sm text-muted-foreground">{props.description}</p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => navigate('/overview')}>
            返回总览
          </Button>
          <Button
            variant="outline"
            onClick={() => {
              logout();
              navigate('/login', { replace: true });
            }}
          >
            重新登录
          </Button>
        </div>
      </div>
    </div>
  );
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { token, ready } = useAuth();
  if (!ready) return <FullPageLoading />;
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** 角色闸：me.roles 无平台角色（super_admin/operator/auditor）则禁止进入运维端。 */
function RequirePlatformRole({ children }: { children: React.ReactNode }) {
  const { me, ready } = useAuth();
  if (!ready) return <FullPageLoading />;
  const roles = me?.roles ?? [];
  if (!roles.some((r) => (PLATFORM_ROLES as readonly string[]).includes(r))) {
    return (
      <AccessDenied
        title="无平台角色权限"
        description="当前账号未被授予平台角色（super_admin / operator / auditor），无法进入运维端。"
      />
    );
  }
  return <>{children}</>;
}

/** 系统配置组页面仅 super_admin 可达（docs/04 §三）。 */
function SuperAdminOnly({ children }: { children: React.ReactNode }) {
  const { isSuperAdmin } = usePerm();
  if (!isSuperAdmin) {
    return <AccessDenied title="无访问权限" description="该页面仅 super_admin 可见。" />;
  }
  return <>{children}</>;
}

/** 运维端全部页面（docs/04 §四 IA），P4-ADM-01~08 全量落地。 */
export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <RequirePlatformRole>
              <AdminLayout />
            </RequirePlatformRole>
          </RequireAuth>
        }
      >
        <Route path="/overview" element={<OverviewPage />} />
        <Route path="/tenants" element={<TenantListPage />} />
        <Route path="/tenants/:tenantId" element={<TenantDetailPage />} />
        <Route path="/users" element={<UserListPage />} />
        <Route path="/spaces" element={<SpaceListPage />} />
        <Route path="/spaces/:spaceId" element={<SpaceDetailPage />} />
        <Route path="/reviews" element={<ReviewQueuePage />} />
        <Route path="/content/:assetId" element={<ContentReviewPage />} />
        <Route path="/pipeline" element={<PipelineListPage />} />
        <Route path="/pipeline/:jobId" element={<PipelineDetailPage />} />
        <Route path="/models" element={<ModelsPage />} />
        <Route path="/retrieval" element={<RetrievalPage />} />
        <Route path="/open/api-keys" element={<ApiKeysPage />} />
        <Route path="/open/oauth" element={<OauthClientsPage />} />
        <Route path="/open/webhooks" element={<WebhooksPage />} />
        <Route
          path="/settings/ai"
          element={
            <SuperAdminOnly>
              <SettingsPage group="ai" />
            </SuperAdminOnly>
          }
        />
        <Route
          path="/settings/retrieval"
          element={
            <SuperAdminOnly>
              <SettingsPage group="retrieval" />
            </SuperAdminOnly>
          }
        />
        <Route
          path="/settings/upload"
          element={
            <SuperAdminOnly>
              <SettingsPage group="upload" />
            </SuperAdminOnly>
          }
        />
        <Route
          path="/settings/sso"
          element={
            <SuperAdminOnly>
              <SettingsPage group="sso" />
            </SuperAdminOnly>
          }
        />
        <Route
          path="/settings/extensions"
          element={
            <SuperAdminOnly>
              <SettingsPage group="extensions" />
            </SuperAdminOnly>
          }
        />
        <Route path="/audit" element={<AuditLogsPage />} />
        <Route path="/system" element={<SystemPage />} />
        <Route path="*" element={<Navigate to="/overview" replace />} />
      </Route>
    </Routes>
  );
}
