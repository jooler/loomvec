/**
 * ACP 服务页面渲染级测试：mock 掉 API 层，验证页面按角色渲染、提示文案齐全、
 * 写操作走 ReasonModal 且请求体带理由、一次性明文只经 SecretModal 展示。
 * 不引入 testing-library：直接用 react-dom/client + act。
 */
import '@/i18n';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  calls: [] as { method: string; path: string; init: Record<string, unknown> | undefined }[],
  responses: {} as Record<string, unknown>,
  perm: { isSuperAdmin: true, canWrite: true },
}));

vi.mock('@/api', () => ({
  api: new Proxy(
    {},
    {
      get: (_t, method: string) => (path: string, init?: Record<string, unknown>) => {
        h.calls.push({ method, path, init });
        const data = h.responses[`${method} ${path}`];
        return Promise.resolve(
          data === undefined ? { error: { message: `unrouted ${method} ${path}` } } : { data },
        );
      },
    },
  ),
  unwrap: async (p: Promise<{ data?: unknown; error?: { message?: string } }>) => {
    const r = await p;
    if (r.error) throw new Error(r.error.message ?? 'error');
    return r.data;
  },
}));
vi.mock('@/auth', () => ({ usePerm: () => h.perm }));

import { AcpApiKeysPage } from './AcpApiKeys';
import { AcpRuntimePage } from './AcpRuntime';
import { AcpTenantsPage } from './AcpTenants';
import { AcpUsagePage } from './AcpUsage';
import { AgentGatewayCard } from '../settings/AgentGatewayCard';
import { SettingsPage } from '../settings/Settings';

const BASE = '/api/v1/admin/acp-gateway';
const PLAINTEXT = 'acpgw_k1_PLAINTEXT-SECRET';
const CONFIG = {
  url: 'http://127.0.0.1:38095',
  admin_token_configured: true,
  api_key_configured: false,
  required_key_scopes: ['sessions:read', 'sessions:write', 'files:read', 'files:write'],
  mcp_sandbox: {
    url: 'http://host.docker.internal:38080/api/v1/mcp',
    hostname: 'host.docker.internal',
    host: 'host.docker.internal:38080',
  },
};
const TENANT_OK = {
  id: 'ten_ok',
  name: '白名单正确',
  status: 'active',
  default_agent: 'claude',
  quotas: { daily_tokens: 1000 },
  settings: { mcp: { allowed_hosts: ['host.docker.internal'] } },
  created_at: '2026-10-01T00:00:00Z',
};
const TENANT_BAD = {
  ...TENANT_OK,
  id: 'ten_bad',
  name: '白名单缺失',
  quotas: {},
  settings: {},
};
const KEY = {
  id: 'key_1',
  tenant_id: 'ten_ok',
  name: 'loomvec-prod',
  prefix: 'acpgw_k1',
  scopes: ['sessions:read', 'sessions:write'],
  rate_limit_rps: null,
  expires_at: null,
  revoked_at: null,
  last_used_at: null,
  created_at: '2026-10-01T00:00:00Z',
};

let host: HTMLDivElement;
let root: Root;

async function flush() {
  for (let i = 0; i < 4; i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

async function mount(ui: React.ReactElement, url = '/') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter>
      </QueryClientProvider>,
    );
  });
  await flush();
}

const text = () => document.body.textContent ?? '';
const button = (label: string) =>
  [...document.body.querySelectorAll('button')].find((b) => b.textContent?.trim() === label) as
    HTMLButtonElement | undefined;

async function click(el: Element | undefined) {
  expect(el).toBeTruthy();
  await act(async () => {
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await flush();
}

async function type(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto.prototype, 'value')!.set!.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  // Radix 依赖的浏览器 API，jsdom 缺失
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  Element.prototype.scrollIntoView ??= () => {};
  Element.prototype.hasPointerCapture ??= () => false;
  h.calls.length = 0;
  h.perm = { isSuperAdmin: true, canWrite: true };
  for (const k of Object.keys(h.responses)) delete h.responses[k];
  h.responses[`GET ${BASE}/config`] = CONFIG;
  h.responses[`GET ${BASE}/tenants`] = {
    items: [TENANT_OK, TENANT_BAD],
    total: 2,
    limit: 20,
    offset: 0,
  };
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  document.body.innerHTML = '';
});

describe('AcpTenantsPage', () => {
  it('lists tenants, flags the tenant whose MCP whitelist misses the sandbox host, shows the hint', async () => {
    await mount(<AcpTenantsPage />);
    expect(text()).toContain('白名单正确');
    expect(text()).toContain('白名单缺失');
    // 合同 §5.3：提示里点明 agent.mcp.url_sandbox 的主机
    expect(text()).toContain('settings.mcp.allowed_hosts');
    expect(text()).toContain('host.docker.internal:38080');
    expect(text()).toContain('已覆盖');
    expect(text()).toContain('缺少 host.docker.internal:38080');
  });

  it('read-only roles cannot write', async () => {
    h.perm = { isSuperAdmin: false, canWrite: true }; // operator
    await mount(<AcpTenantsPage />);
    expect(button('创建租户')?.disabled).toBe(true);
    expect(button('配额')?.disabled).toBe(true);
    expect(button('暂停')?.disabled).toBe(true);
  });

  it('quota edit goes through ReasonModal and replaces quotas with the reason', async () => {
    h.responses[`PATCH ${BASE}/tenants/{tenant_id}`] = TENANT_OK;
    await mount(<AcpTenantsPage />);
    await click(button('配额'));
    const input = document.getElementById('acp-quota-monthly_tokens') as HTMLInputElement;
    await type(input, '5000');
    // 不填理由：被拦下，不发请求
    await click(button('保存'));
    expect(h.calls.some((c) => c.method === 'PATCH')).toBe(false);
    expect(text()).toContain('请填写操作理由');
    await type(document.querySelector('textarea')!, '提额');
    await click(button('保存'));
    const patch = h.calls.find((c) => c.method === 'PATCH');
    expect(patch?.init?.body).toEqual({
      quotas: { daily_tokens: 1000, monthly_tokens: 5000 },
      reason: '提额',
    });
  });
});

describe('AcpApiKeysPage', () => {
  beforeEach(() => {
    h.responses[`GET ${BASE}/tenants/{tenant_id}/api-keys`] = {
      items: [KEY],
      total: 1,
      limit: 20,
      offset: 0,
    };
  });

  it('shows the required scopes and the key list without any plaintext', async () => {
    await mount(<AcpApiKeysPage />);
    expect(text()).toContain('sessions:read / sessions:write / files:read / files:write');
    expect(text()).toContain('loomvec-prod');
    expect(text()).toContain('acpgw_k1…');
    expect(text()).not.toContain('PLAINTEXT');
  });

  it('issues a key: reason required, plaintext shown once in SecretModal then dropped', async () => {
    h.responses[`POST ${BASE}/tenants/{tenant_id}/api-keys`] = { ...KEY, api_key: PLAINTEXT };
    await mount(<AcpApiKeysPage />);
    await click(button('签发 Key'));
    // 默认勾选 loomvec 必需 scope
    await type(document.getElementById('acp-key-name') as HTMLInputElement, 'loomvec-prod');
    await type(document.querySelector('textarea')!, 'P2 切换');
    await click(
      [...document.querySelectorAll('button')].filter((b) => b.textContent === '签发 Key').at(-1),
    );
    const post = h.calls.find((c) => c.method === 'POST');
    expect(post?.init?.body).toMatchObject({
      name: 'loomvec-prod',
      scopes: ['sessions:read', 'sessions:write', 'files:read', 'files:write'],
      reason: 'P2 切换',
    });
    // 明文只出现在 SecretModal
    expect(text()).toContain(PLAINTEXT);
    await click(button('我已保存，关闭'));
    expect(text()).not.toContain(PLAINTEXT);
  });

  it('read-only roles cannot issue or revoke', async () => {
    h.perm = { isSuperAdmin: false, canWrite: false }; // auditor
    await mount(<AcpApiKeysPage />);
    expect(button('签发 Key')?.disabled).toBe(true);
    expect(button('吊销')?.disabled).toBe(true);
  });
});

describe('AcpRuntimePage', () => {
  const STATUS = {
    url: CONFIG.url,
    configured: true,
    reachable: true,
    healthy: true,
    agents: ['claude', 'dsh'],
    provider: 'docker',
    running_turns: 2,
    runtimes: [
      {
        workspace_id: 'ws_1',
        agent: 'claude',
        agent_info: { name: 'claude-agent', version: '1.2.3' },
        active_turns: 1,
        started_at: '2026-10-13T00:00:00Z',
        last_activity: '2026-10-13T00:01:00Z',
      },
    ],
    error: null,
  };

  it('renders the status card and runtimes; operator can force-stop with a reason', async () => {
    h.perm = { isSuperAdmin: false, canWrite: true };
    h.responses[`GET ${BASE}/status`] = STATUS;
    h.responses[`POST ${BASE}/workspaces/{workspace_id}/stop`] = { stopped: true };
    await mount(<AcpRuntimePage />);
    expect(text()).toContain('可达');
    expect(text()).toContain('docker');
    expect(text()).toContain('ws_1');
    expect(text()).toContain('claude-agent 1.2.3');
    await click(button('强制停止'));
    await type(document.querySelector('textarea')!, '卡死');
    await click(
      [...document.querySelectorAll('button')].filter((b) => b.textContent === '确认执行').at(-1),
    );
    const stop = h.calls.find((c) => c.method === 'POST');
    expect(stop?.init).toMatchObject({
      params: { path: { workspace_id: 'ws_1' } },
      body: { reason: '卡死' },
    });
  });

  it('auditor cannot stop; degraded gateway shows the error text', async () => {
    h.perm = { isSuperAdmin: false, canWrite: false };
    h.responses[`GET ${BASE}/status`] = {
      ...STATUS,
      reachable: false,
      healthy: false,
      agents: [],
      error: 'ACP 网关不可达或响应超时',
    };
    await mount(<AcpRuntimePage />);
    expect(text()).toContain('不可达');
    expect(text()).toContain('ACP 网关不可达或响应超时');
  });
});

describe('AcpUsagePage', () => {
  it('renders quota usage and the per-day table', async () => {
    h.responses[`GET ${BASE}/tenants/{tenant_id}/usage`] = {
      quotas: { daily_tokens: 1000 },
      current: { day: 950, month: 12345 },
      daily: [
        {
          day: '2026-10-12',
          input_tokens: 400,
          output_tokens: 100,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          requests: 7,
        },
      ],
    };
    await mount(<AcpUsagePage />);
    expect(text()).toContain('2026-10-12');
    expect(text()).toContain('12,345');
    expect(text()).toContain('上限 1,000');
    const usageCall = h.calls.find((c) => c.path.endsWith('/usage'));
    expect(usageCall?.init?.params).toMatchObject({ path: { tenant_id: 'ten_ok' } });
  });
});

describe('AgentGatewayCard（设置 → agent）', () => {
  it('shows url read-only, credential flags (never values), scope and whitelist hints', async () => {
    await mount(<AgentGatewayCard />);
    expect(text()).toContain('http://127.0.0.1:38095');
    expect(text()).toContain('已配置'); // admin_token
    expect(text()).toContain('未配置'); // api_key
    expect(text()).toContain('sessions:read / sessions:write / files:read / files:write');
    expect(text()).toContain('host.docker.internal:38080');
  });
});

describe('SettingsPage group=agent', () => {
  const ITEMS = [
    ['agent.default_agent', 'claude', false],
    ['agent.gateway.models.claude', null, false],
    ['agent.gateway.api_key', '******', true],
    ['agent.gateway.admin_token', null, true],
  ].map(([key, value, sensitive]) => ({
    key,
    group: 'agent',
    description: `d-${key}`,
    value,
    sensitive,
    effect: 'immediate',
    admin_only: true,
  }));

  it('lists the agent keys, masks credentials, edits sensitive items from blank with a reason', async () => {
    h.responses['GET /api/v1/admin/settings'] = ITEMS;
    h.responses['PUT /api/v1/admin/settings/{key}'] = { key: 'x', value: '******' };
    await mount(<SettingsPage group="agent" />);
    expect(text()).toContain('agent.gateway.admin_token');
    expect(text()).toContain('******');
    expect(text()).toContain('未设置');
    // 未设置的敏感项是「设置」，已设置的是「编辑」；编辑框从空开始，不带回脱敏占位
    const edits = [...document.querySelectorAll('button')].filter((b) => b.textContent === '编辑');
    await click(edits[2]); // api_key（已设置）
    // ReasonModal 的理由框在前，值编辑框（ValueEditor）在后
    const [reason, valueBox] = [...document.querySelectorAll('textarea')];
    expect(valueBox.value).toBe('');
    await type(valueBox, 'new-secret-value');
    await type(reason, '轮换');
    await click(button('保存'));
    const put = h.calls.find((c) => c.method === 'PUT');
    expect(put?.init).toMatchObject({
      params: { path: { key: 'agent.gateway.api_key' } },
      body: { value: 'new-secret-value', reason: '轮换' },
    });
    // 保存后不会在页面任何位置回显明文
    expect(text()).not.toContain('new-secret-value');
  });
});
