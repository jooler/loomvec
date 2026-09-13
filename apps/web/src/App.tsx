import { Navigate, Route, Routes } from 'react-router';
import { AppLayout } from '@/layouts/AppLayout';
import { LoginPage } from '@/pages/Login';
import { HomePage } from '@/pages/Home';
import { SpacesPage } from '@/pages/Spaces';
import { SearchPage } from '@/pages/SearchPage';
import { AssetsPage } from '@/pages/AssetsPage';
import { AssetDetailPage } from '@/pages/AssetDetailPage';
import { UploadPage } from '@/pages/UploadPage';
import { ReviewPage } from '@/pages/ReviewPage';
import { SpaceSettingsPage } from '@/pages/SpaceSettingsPage';
import { SpaceMembersPage } from '@/pages/SpaceMembersPage';
import { ChatPage } from '@/pages/ChatPage';
import { GraphPage } from '@/pages/GraphPage';
import { ProfilePage } from '@/pages/ProfilePage';
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
        <Route path="/" element={<HomePage />} />
        <Route path="/spaces" element={<SpacesPage />} />
        {/* 聚合模式：跨我的全部空间检索 / 资产 */}
        <Route path="/search" element={<SearchPage />} />
        <Route path="/assets" element={<AssetsPage />} />
        <Route path="/a/:assetId" element={<AssetDetailPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        {/* 空间模式：空间切换 / 上传 / 检索 / 审核 / 成员 / 设置 */}
        <Route path="/s/:spaceId/search" element={<SearchPage />} />
        <Route path="/s/:spaceId/assets" element={<AssetsPage />} />
        <Route path="/s/:spaceId/upload" element={<UploadPage />} />
        <Route path="/s/:spaceId/review" element={<ReviewPage />} />
        <Route path="/s/:spaceId/members" element={<SpaceMembersPage />} />
        <Route path="/s/:spaceId/settings" element={<SpaceSettingsPage />} />
        <Route path="/s/:spaceId/chat" element={<ChatPage />} />
        <Route path="/s/:spaceId/graph" element={<GraphPage />} />
      </Route>
    </Routes>
  );
}
