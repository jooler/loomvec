import { createContext, useContext, useEffect, useState } from 'react';
import { api, clearToken, getStoredToken, storeToken } from '@loomvec/sdk-ts';

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

  useEffect(() => {
    const existing = getStoredToken();
    if (!existing) {
      setReady(true);
      return;
    }
    setToken(existing);
    api
      .GET('/api/v1/me')
      .then((resp) => setMe((resp.data ?? null) as MeInfo | null))
      .catch(() => clearToken())
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
      throw new Error((await resp.json().catch(() => null))?.message ?? '登录失败');
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
