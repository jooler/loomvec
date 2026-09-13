import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { Link } from 'react-router';
import { api } from '@loomvec/sdk-ts';
import { useMySpaces } from '@/hooks';
import { ROLE_META } from '@/utils';
import { EmptyState } from '@loomvec/ui/components/empty-state';
import { StatusBadge, type BadgeTone } from '@loomvec/ui/components/status-badge';
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';

/** ROLE_META 的 antd 色 → StatusBadge tone。 */
const ROLE_COLOR_TONE: Record<string, BadgeTone> = {
  gold: 'amber',
  blue: 'blue',
  default: 'gray',
};

/** 工作台：用户问候 + 检索/资产入口 + 我的空间（P2 实数据）。 */
export function HomePage() {
  const navigate = useNavigate();
  const { data: meResp } = useQuery({
    queryKey: ['me'],
    queryFn: () => api.GET('/api/v1/me'),
  });
  const me = meResp?.data as
    | { user_id: string; username: string; tenant_id: string | null; roles: string[] }
    | undefined;
  const spaces = useMySpaces();

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">你好，{me?.username ?? '…'}</CardTitle>
          <CardDescription>
            P1 检索闭环已就绪：上传文档，数分钟后即可语义检索并回溯页码。
          </CardDescription>
        </CardHeader>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>语义检索</CardTitle>
            <CardAction>
              <Link
                to="/search"
                className="text-sm text-primary underline-offset-4 hover:underline"
              >
                前往
              </Link>
            </CardAction>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">dense + BM25 混合召回，精排可开关。</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>资产</CardTitle>
            <CardAction>
              <Link
                to="/assets"
                className="text-sm text-primary underline-offset-4 hover:underline"
              >
                前往
              </Link>
            </CardAction>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">上传与处理状态，失败可单步重跑。</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>我的空间</CardTitle>
          <CardAction>
            <Link
              to="/spaces"
              className="text-sm text-primary underline-offset-4 hover:underline"
            >
              管理空间
            </Link>
          </CardAction>
        </CardHeader>
        <CardContent>
          {spaces.isLoading ? (
            <p className="text-sm text-muted-foreground">加载中…</p>
          ) : (spaces.data?.length ?? 0) === 0 ? (
            <EmptyState description="还没有空间，去「管理空间」创建一个吧" />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {(spaces.data ?? []).map((s) => {
                const roleMeta = ROLE_META[s.my_role ?? ''];
                return (
                  <Card
                    key={s.id}
                    className="cursor-pointer gap-3 py-4 transition-shadow hover:shadow-md"
                    onClick={() => navigate(`/s/${s.id}/assets`)}
                  >
                    <CardHeader>
                      <CardTitle className="truncate text-base">{s.name}</CardTitle>
                      <CardAction className="flex flex-col items-end gap-1">
                        {s.review_required && <StatusBadge tone="amber">需审核</StatusBadge>}
                        <StatusBadge tone={roleMeta ? ROLE_COLOR_TONE[roleMeta.color] : undefined}>
                          {roleMeta?.text ?? s.my_role}
                        </StatusBadge>
                      </CardAction>
                    </CardHeader>
                  </Card>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
