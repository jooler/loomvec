/**
 * ACP 服务页面共用的纯函数（无 React 依赖，便于单测）。
 * 规则以网关为准：配额项见网关 docs/api.md「租户 quotas」，MCP 白名单匹配见网关 src/sessions/mcp.ts。
 */
import type { AcpApiKey, AcpQuotas } from '@/types';

/** 配额项（表单字段顺序）；label 在 acp 命名空间 quota.<key>。 */
export const QUOTA_KEYS = [
  'max_workspaces',
  'max_concurrent_turns',
  'daily_tokens',
  'monthly_tokens',
  'upstream_rpm',
  'upstream_concurrency',
  'max_turn_seconds',
  'max_disk_bytes',
] as const satisfies readonly (keyof AcpQuotas)[];

export type QuotaKey = (typeof QUOTA_KEYS)[number];

/** 配额表单值（字符串，空串 = 不限制）。 */
export type QuotaFormValues = Record<QuotaKey, string>;

export function quotasToForm(quotas: AcpQuotas | undefined): QuotaFormValues {
  const out = {} as QuotaFormValues;
  for (const k of QUOTA_KEYS) out[k] = quotas?.[k] === undefined ? '' : String(quotas[k]);
  return out;
}

/** 表单值 → 配额对象（PATCH 整体替换语义）；返回 null 表示有不合法的值。 */
export function formToQuotas(values: QuotaFormValues): AcpQuotas | null {
  const out: AcpQuotas = {};
  for (const k of QUOTA_KEYS) {
    const raw = values[k].trim();
    if (raw === '') continue;
    const n = Number(raw);
    const min = k === 'max_turn_seconds' ? 1 : 0;
    if (!Number.isInteger(n) || n < min) return null;
    out[k] = n;
  }
  return out;
}

/** 与网关 hostAllowed 同口径：`host`、`host:port` 或 `*.suffix`（大小写不敏感）。 */
export function mcpHostAllowed(
  allowedHosts: readonly string[] | undefined,
  target: { hostname: string | null; host: string | null },
): boolean {
  const hostname = target.hostname?.toLowerCase();
  const host = target.host?.toLowerCase();
  if (!hostname) return false;
  return (allowedHosts ?? []).some((entry) => {
    const e = entry.trim().toLowerCase();
    if (e.startsWith('*.')) return hostname.endsWith(e.slice(1));
    return e === hostname || e === host;
  });
}

/** 多行文本 → 白名单条目（去空行 / 去重 / 保序）。 */
export function parseHostLines(text: string): string[] {
  return [
    ...new Set(
      text
        .split(/[\n,]/)
        .map((s) => s.trim())
        .filter(Boolean),
    ),
  ];
}

export type KeyStatus = 'active' | 'revoked' | 'expired';

export function keyStatus(
  key: Pick<AcpApiKey, 'revoked_at' | 'expires_at'>,
  now = Date.now(),
): KeyStatus {
  if (key.revoked_at) return 'revoked';
  if (key.expires_at && Date.parse(key.expires_at) <= now) return 'expired';
  return 'active';
}

/** 选中日期（YYYY-MM-DD，本地时区）当天 23:59:59 → ISO（UTC）。空串 = 永不过期。 */
export function expiryFromDate(date: string): string | undefined {
  if (!date) return undefined;
  const d = new Date(`${date}T23:59:59`);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** 缺失的必需 scope（签发 loomvec 租户 Key 时的校验提示）。 */
export function missingScopes(selected: readonly string[], required: readonly string[]): string[] {
  return required.filter((s) => !selected.includes(s));
}

/** 近 N 天的 [from, to) 日期串（to 为明天，网关的 to 是开区间上界）。 */
export function lastDaysRange(days: number, now = new Date()): { from: string; to: string } {
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  const to = new Date(now.getTime() + 86_400_000);
  const from = new Date(to.getTime() - days * 86_400_000);
  return { from: iso(from), to: iso(to) };
}
