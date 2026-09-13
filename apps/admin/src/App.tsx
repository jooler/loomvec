import { ShieldAlert } from 'lucide-react';
import { Navigate, Route, Routes, useNavigate } from 'react-router';
import { AdminLayout } from '@/layouts/AdminLayout';
import { LoginPage } from '@/pages/Login';
import { OverviewPage } from '@/pages/Overview';
import { SystemPage } from '@/pages/System';
import { TenantListPage } from '@/pages/tenants/TenantList';
import { TenantDetailPage } from '@/pages/tenants/TenantDetail';
import { UserListPage } from '@/pages/UserList';
import { SpaceListPage } from '@/pages/spaces/SpaceList';
import { SpaceDetailPage } from '@/pages/spaces/SpaceDetail';
import { ReviewQueuePage } from '@/pages/ReviewQueue';
import { ContentReviewPage } from '@/pages/ContentReview';
import { PipelineListPage } from '@/pages/pipeline/PipelineList';
import { PipelineDetailPage } from '@/pages/pipeline/PipelineDetail';
import { ModelsPage } from '@/pages/Models';
import { RetrievalPage } from '@/pages/Retrieval';
import { ApiKeysPage } from '@/pages/open/ApiKeys';
import { OauthClientsPage } from '@/pages/open/OauthClients';
import { WebhooksPage } from '@/pages/open/Webhooks';
import { SettingsPage } from '@/pages/SettingsPage';
import { AuditLogsPage } from '@/pages/AuditLogs';
import { PLATFORM_ROLES, useAuth, usePerm } from '@/auth';
import { Button } from '@loomvec/ui/components/ui/button';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

function FullPageLoading() {
  return (
    <div className="grid min-h-svh place-items-center">
      <Spinner className="size-6 text-muted-foreground" />
    </div>
  );
}

/** 403 展示（替代 antd Result）。 */
function AccessDenied(props: { title: string; description: string }) {
  const navigate = useNavigate();
  return (
    <div className="grid min-h-svh place-items-center p-6">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        <div className="flex size-12 items-center justify-center rounded-full bg-muted">
          <ShieldAlert className="size-6 text-muted-foreground" />
        </div>
        <h2 className="text-lg font-semibold">{props.title}</h2>
        <p className="text-sm text-muted-foreground">{props.description}</p>
        <Button variant="outline" onClick={() => navigate('/overview')}>
          返回总览
        </Button>
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
