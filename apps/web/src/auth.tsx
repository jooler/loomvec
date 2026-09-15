import { createContext, useContext, useEffect, useState } from 'react';
import { api, clearToken, getStoredToken, storeToken } from '@loomvec/sdk-ts';
import { useTranslation } from 'react-i18next';
import { extractApiError } from './utils';

export interface MeInfo {
  user_id: string;
  username: string;
  tenant_id: string | null;
  roles: string[];
  /** P2：通知未读数与租户名（/me 返回） */
  unread_notifications?: number;
  tenant_name?: string | null;
}

interface AuthState {
  token: string | null;
  ready: boolean;
  me: MeInfo | null;
  loginDev: (username: string, roles?: string[]) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [me, setMe] = useState<MeInfo | null>(null);
  const [ready, setReady] = useState(false);
  const { t } = useTranslation('auth');

  useEffect(() => {
    const existing = getStoredToken();
    if (!existing) {
      setReady(true);
      return;
    }
    setToken(existing);
    api
      .GET('/api/v1/me')
      .then((resp) => {
        if (resp.data) {
          setMe(resp.data as unknown as MeInfo);
          return;
        }
        // 旧 JWT 已失效（如后端密钥轮换、token 过期）：openapi-fetch 对 401 不抛错，
        // 必须连同 React state 一起清空，否则 RequireAuth 仍认为已登录而进入脏状态。
        // 仅 401 清登录态：5xx / 临时故障保留 token，避免把用户误登出。
        if (resp.response?.status === 401) {
          clearToken();
          setToken(null);
          setMe(null);
        }
      })
      .catch(() => {
        // 网络异常：保留 token（进入应用后由各查询重试；401 由 SDK 中间件兜底清理）
      })
      .finally(() => setReady(true));
  }, []);

  const loginDev = async (username: string, roles?: string[]) => {
    // dev 登录：签发测试 JWT（P0 认证骨架）
    const resp = await fetch('/api/v1/auth/dev/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Request-Id': crypto.randomUUID() },
      body: JSON.stringify({ username, roles: roles ?? ['user'] }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => null);
      throw new Error(extractApiError(err, t('loginFailed')));
    }
    const data = (await resp.json()) as { access_token: string };
    storeToken(data.access_token);
    setToken(data.access_token);
    const meResp = await api.GET('/api/v1/me');
    setMe((meResp.data ?? null) as MeInfo | null);
  };

  const logout = () => {
    clearToken();
    setToken(null);
    setMe(null);
  };

  return (
    <AuthContext.Provider value={{ token, ready, me, loginDev, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
