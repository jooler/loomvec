import { createContext, useContext, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { clearToken, getStoredToken, storeToken } from '@loomvec/sdk-ts';
import { toast } from 'sonner';
import { api, setUnauthorizedHandler } from './api';

export interface MeInfo {
  user_id: string;
  username: string;
  tenant_id: string | null;
  roles: string[];
}

interface AuthState {
  token: string | null;
  ready: boolean;
  me: MeInfo | null;
  loginDev: (username: string, roles?: string[]) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

/** 平台角色（与 core/constants.PLATFORM_ROLES 对齐），与租户内 RBAC 相互独立（docs/04 §三）。 */
export const PLATFORM_ROLES = ['super_admin', 'operator', 'auditor'] as const;

/** 角色双闸之按钮闸：canWrite=可执行治理动作（operator 及以上，auditor 全局只读）；isSuperAdmin=系统配置/角色分配。 */
export function usePerm(): { roles: string[]; canWrite: boolean; isSuperAdmin: boolean } {
  const { me } = useAuth();
  const roles = me?.roles ?? [];
  const isSuperAdmin = roles.includes('super_admin');
  return { roles, canWrite: isSuperAdmin || roles.includes('operator'), isSuperAdmin };
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [me, setMe] = useState<MeInfo | null>(null);
  const [ready, setReady] = useState(false);
  const queryClient = useQueryClient();

  const logout = () => {
    clearToken();
    setToken(null);
    setMe(null);
    // 清空查询缓存，避免换账号后上一账号的数据闪现
    queryClient.clear();
  };

  // 会话中途 401（token 过期/吊销）：SDK 中间件已清 localStorage，这里同步 React
  // 登录态并回登录页。AuthProvider 在 Router 外，用 hash 直跳（HashRouter 会响应）。
  useEffect(() => {
    setUnauthorizedHandler(() => {
      logout();
      toast.error('登录已过期，请重新登录', { id: 'auth-expired' });
      if (window.location.hash !== '#/login') window.location.hash = '#/login';
    });
    return () => setUnauthorizedHandler(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
        // 必须连同 React state 一起清空，否则 RequireAuth 仍认为已登录而卡在 403 页。
        clearToken();
        setToken(null);
        setMe(null);
      })
      .catch(() => {
        clearToken();
        setToken(null);
        setMe(null);
      })
      .finally(() => setReady(true));
  }, []);

  const loginDev = async (username: string, roles?: string[]) => {
    const resp = await fetch('/api/v1/auth/dev/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Request-Id': crypto.randomUUID() },
      // 运维端 dev 登录默认挂平台角色（路由守卫按平台角色生效于 P4）
      body: JSON.stringify({ username, roles: roles ?? ['super_admin'] }),
    });
    if (!resp.ok) {
      throw new Error((await resp.json().catch(() => null))?.message ?? '登录失败');
    }
    const data = (await resp.json()) as { access_token: string };
    storeToken(data.access_token);
    setToken(data.access_token);
    const meResp = await api.GET('/api/v1/me');
    setMe((meResp.data ?? null) as MeInfo | null);
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
