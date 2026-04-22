import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '$lib/api/client';
import { auth } from '$lib/stores/auth';

const jsonHeaders = { 'Content-Type': 'application/json' };

describe('api client session handling', () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    auth.clear();
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('clears auth state when a protected request returns 401', async () => {
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            user: { email: 'user@example.com', name: 'User', role: 'user' },
            expires_at: new Date(Date.now() + 3_600_000).toISOString()
          }),
          { status: 200, headers: jsonHeaders }
        )
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ error: { code: 'unauthorized', message: 'expired' } }), {
          status: 401,
          headers: jsonHeaders
        })
      );

    global.fetch = fetchMock;

    await auth.login('user@example.com', 'password123');
    await expect(api.auth.me()).rejects.toMatchObject({ status: 401, code: 'unauthorized' });
    expect(auth.getSnapshot().status).toBe('anonymous');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
