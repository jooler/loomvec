import { Navigate, Route, Routes } from 'react-router';
import { AppLayout } from '@/layouts/AppLayout';
import { SpaceLayout } from '@/layouts/SpaceLayout';
import { LoginPage } from '@/pages/Login';
import { SpacesPage } from '@/pages/SpacesPage';
import { SearchPage } from '@/pages/SearchPage';
import { AssetsPage } from '@/pages/assets/AssetsPage';
import { AssetDetailPage } from '@/pages/asset-detail/AssetDetailPage';
import { ReviewPage } from '@/pages/ReviewPage';
import { SpaceSettingsPage } from '@/pages/SpaceSettingsPage';
import { SpaceMembersPage } from '@/pages/SpaceMembersPage';
import { ChatPage } from '@/pages/ChatPage';
import { GraphPage } from '@/pages/GraphPage';
import { ProfilePage } from '@/pages/ProfilePage';
import { NotificationsPage } from '@/pages/NotificationsPage';
import { useAuth } from '@/auth';
import { Spinner } from '@loomvec/ui/components/ui/spinner';

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { token, ready } = useAuth();
  if (!ready)
    return (
      <div className="grid min-h-svh place-items-center">
        <Spinner className="size-6 text-muted-foreground" />
      </div>
    );
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        {/* 工作台已移除（功能与其它条目重复），根路径直达对话 */}
        <Route path="/" element={<Navigate to="/chat" replace />} />
        {/* 对话：会话列表常驻侧栏上部，历史会话经 /chat/:sessionId 载入 */}
        <Route path="/chat" element={<ChatPage />} />
        <Route path="/chat/:sessionId" element={<ChatPage />} />
        {/* 空间管理：卡片列表 → 空间详情（页签：资产/检索/图谱/审核/成员/设置） */}
        <Route path="/spaces" element={<SpacesPage />} />
        <Route path="/search" element={<SearchPage />} />
        <Route path="/a/:assetId" element={<AssetDetailPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/notifications" element={<NotificationsPage />} />
        <Route path="/s/:spaceId" element={<SpaceLayout />}>
          <Route index element={<Navigate to="assets" replace />} />
          <Route path="assets" element={<AssetsPage />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="review" element={<ReviewPage />} />
          <Route path="members" element={<SpaceMembersPage />} />
          <Route path="settings" element={<SpaceSettingsPage />} />
          <Route path="graph" element={<GraphPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
