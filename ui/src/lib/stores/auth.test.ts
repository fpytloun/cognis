import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$app/environment', () => ({ browser: true }));

import { auth } from './auth';

const jsonHeaders = { 'Content-Type': 'application/json' };

describe('MFA authentication flow', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    auth.clear();
  });

  it('does not authenticate before a challenge completes', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({
        status: 'mfa_required',
        method: 'totp',
        challenge_token: 'challenge-token-with-sufficient-length',
        expires_in: 300
      }), { status: 200, headers: jsonHeaders }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        status: 'authenticated',
        user: { email: 'user@example.com', name: 'User', role: 'user' },
        expires_at: '2026-08-14T12:00:00Z',
        recovery_codes: null
      }), { status: 200, headers: jsonHeaders }));

    const challenge = await auth.login('user@example.com', 'password');
    expect(challenge?.status).toBe('mfa_required');
    expect(auth.getSnapshot().status).toBe('anonymous');

    await auth.completeMfa(challenge!.challenge_token, '123456', false);
    expect(auth.getSnapshot().status).toBe('authenticated');
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('starts required enrollment through the challenge-bound endpoint', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({
      status: 'mfa_setup',
      method: 'totp',
      challenge_token: 'challenge-token-with-sufficient-length',
      secret: 'JBSWY3DPEHPK3PXP',
      provisioning_uri: 'otpauth://totp/Cognis:user'
    }), { status: 200, headers: jsonHeaders }));

    const setup = await auth.startMfaSetup('challenge-token-with-sufficient-length');
    expect(setup.secret).toBe('JBSWY3DPEHPK3PXP');
  });
});
