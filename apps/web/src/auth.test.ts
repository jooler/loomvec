import { afterEach, describe, expect, it } from 'vitest';
import { clearToken, getStoredToken, storeToken } from '@loomvec/sdk-ts';

describe('token storage', () => {
  afterEach(() => clearToken());

  it('stores and clears the access token', () => {
    expect(getStoredToken()).toBeNull();
    storeToken('token-abc');
    expect(getStoredToken()).toBe('token-abc');
    clearToken();
    expect(getStoredToken()).toBeNull();
  });
});
