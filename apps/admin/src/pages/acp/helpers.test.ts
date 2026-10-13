import { describe, expect, it } from 'vitest';
import {
  QUOTA_KEYS,
  expiryFromDate,
  formToQuotas,
  keyStatus,
  lastDaysRange,
  mcpHostAllowed,
  missingScopes,
  parseHostLines,
  quotasToForm,
} from './helpers';

const target = { hostname: 'host.docker.internal', host: 'host.docker.internal:38080' };

describe('mcpHostAllowed（与网关 hostAllowed 同口径）', () => {
  it('matches hostname, host:port and wildcard suffix, case-insensitively', () => {
    expect(mcpHostAllowed(['host.docker.internal'], target)).toBe(true);
    expect(mcpHostAllowed(['Host.Docker.Internal:38080'], target)).toBe(true);
    expect(mcpHostAllowed(['*.docker.internal'], target)).toBe(true);
  });

  it('rejects other hosts, wrong ports and a missing whitelist', () => {
    expect(mcpHostAllowed(['host.docker.internal:9999'], target)).toBe(false);
    expect(mcpHostAllowed(['evil.com', '*.example.com'], target)).toBe(false);
    expect(mcpHostAllowed(undefined, target)).toBe(false);
    expect(mcpHostAllowed([], target)).toBe(false);
    expect(mcpHostAllowed(['*'], { hostname: null, host: null })).toBe(false);
  });
});

describe('quota form <-> payload', () => {
  it('round-trips and treats blank as unlimited', () => {
    const form = quotasToForm({ daily_tokens: 1000, max_workspaces: 5 });
    expect(form.daily_tokens).toBe('1000');
    expect(form.monthly_tokens).toBe('');
    expect(formToQuotas(form)).toEqual({ daily_tokens: 1000, max_workspaces: 5 });
    expect(formToQuotas(quotasToForm(undefined))).toEqual({});
    expect(QUOTA_KEYS).toHaveLength(8);
  });

  it('rejects negative, fractional and zero max_turn_seconds', () => {
    const base = quotasToForm(undefined);
    expect(formToQuotas({ ...base, daily_tokens: '-1' })).toBeNull();
    expect(formToQuotas({ ...base, daily_tokens: '1.5' })).toBeNull();
    expect(formToQuotas({ ...base, daily_tokens: 'abc' })).toBeNull();
    expect(formToQuotas({ ...base, max_turn_seconds: '0' })).toBeNull();
    expect(formToQuotas({ ...base, max_workspaces: '0' })).toEqual({ max_workspaces: 0 });
  });
});

describe('misc helpers', () => {
  it('parseHostLines dedupes and drops blanks', () => {
    expect(parseHostLines('a.com\n\n b.com ,a.com\n')).toEqual(['a.com', 'b.com']);
  });

  it('keyStatus: revoked beats expired beats active', () => {
    const now = Date.parse('2026-10-13T00:00:00Z');
    expect(keyStatus({ revoked_at: '2026-10-01T00:00:00Z', expires_at: null }, now)).toBe(
      'revoked',
    );
    expect(keyStatus({ revoked_at: null, expires_at: '2026-10-12T00:00:00Z' }, now)).toBe(
      'expired',
    );
    expect(keyStatus({ revoked_at: null, expires_at: '2026-10-14T00:00:00Z' }, now)).toBe('active');
    expect(keyStatus({ revoked_at: null, expires_at: null }, now)).toBe('active');
  });

  it('missingScopes lists the required scopes that were not selected', () => {
    expect(missingScopes(['sessions:read'], ['sessions:read', 'files:write'])).toEqual([
      'files:write',
    ]);
  });

  it('expiryFromDate returns ISO or undefined', () => {
    expect(expiryFromDate('')).toBeUndefined();
    expect(expiryFromDate('2099-01-01')).toMatch(/^2099-01-0[12]T/);
  });

  it('lastDaysRange spans N days with an exclusive upper bound of tomorrow', () => {
    const r = lastDaysRange(30, new Date('2026-10-13T08:00:00Z'));
    expect(r).toEqual({ from: '2026-09-14', to: '2026-10-14' });
  });
});
