import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '$lib/api/client';
import { auth } from '$lib/stores/auth';

const jsonHeaders = { 'Content-Type': 'application/json' };

describe('api client auth retry', () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    window.localStorage?.clear?.();
    auth.clear();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('refreshes once and retries the protected request', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            token: 'access-1',
            refresh_token: 'refresh-1',
            expires_in: 3600,
            user: { email: 'user@example.com', name: 'User', role: 'user' }
          }),
          { status: 200, headers: jsonHeaders }
        )
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: 'unauthorized', message: 'expired' } }), {
          status: 401,
          headers: jsonHeaders
        })
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            token: 'access-2',
            refresh_token: 'refresh-2',
            expires_in: 3600,
            user: { email: 'user@example.com', name: 'User', role: 'user' }
          }),
          { status: 200, headers: jsonHeaders }
        )
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ email: 'user@example.com', name: 'User', role: 'user' }),
          { status: 200, headers: jsonHeaders }
        )
      );

    global.fetch = fetchMock;

    await auth.login('user@example.com', 'password123');
    const me = await api.auth.me();

    expect(me.email).toBe('user@example.com');
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(String(fetchMock.mock.calls[2]?.[0])).toContain('/api/auth/refresh');
    expect(String(fetchMock.mock.calls[3]?.[0])).toContain('/api/auth/me');
  });
});
