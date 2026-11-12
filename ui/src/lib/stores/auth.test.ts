import { afterEach, describe, expect, it, vi } from 'vitest';
import 'fake-indexeddb/auto';
import { IDBDatabase } from 'fake-indexeddb';
import { documentCoordination, CoordinationUnavailableError } from '$lib/document-coordination';

vi.mock('$app/environment', () => ({ browser: true }));

import { auth } from './auth';

const jsonHeaders = { 'Content-Type': 'application/json' };

describe('MFA authentication flow', () => {
  afterEach(async () => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    await auth.clear();
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

  it('does not authorize an old MFA challenge under a newer login intent', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    for (const token of ['first-challenge-token', 'second-challenge-token']) {
      fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
        status: 'mfa_required', method: 'totp', challenge_token: token, expires_in: 300,
      }), { status: 200, headers: jsonHeaders }));
      await auth.login('user@example.com', 'password');
    }
    await expect(auth.completeMfa('first-challenge-token', '123456', false))
      .rejects.toThrow('account or server changed');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(auth.getSnapshot().status).toBe('anonymous');
  });

  it('releases shared bootstrap after a stalled response body and allows fresh verification', async () => {
    const user = { email: 'user@example.com', name: 'User', role: 'user' };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({
      status: 'authenticated', user, expires_at: '2099-01-01T00:00:00Z',
    }), { status: 200, headers: jsonHeaders }));
    await auth.login(user.email, 'password');
    const cancelled = vi.fn();
    const response = new Response(new ReadableStream({ cancel: cancelled }), { status: 200 });
    const getReader = vi.spyOn(response.body!, 'getReader');
    fetchMock.mockResolvedValueOnce(response);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const first = auth.bootstrap();
    const joined = auth.bootstrap();
    await vi.waitFor(() => expect(getReader).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(15_001);
    await Promise.all([first, joined]);
    expect(cancelled).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(user), { status: 200, headers: jsonHeaders }));
    await auth.bootstrap();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(auth.getSnapshot()).toMatchObject({ status: 'authenticated', offline: false, user });
  });

  it('revokes a stalled coordination lease and verifies afresh without redirecting to login', async () => {
    const user = { email: 'user@example.com', name: 'User', role: 'user' };
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(JSON.stringify({
        status: 'authenticated', user, expires_at: '2099-01-01T00:00:00Z',
      }), { status: 200, headers: jsonHeaders }))
      .mockResolvedValueOnce(new Response(JSON.stringify(user), { status: 200, headers: jsonHeaders }));
    await auth.login(user.email, 'password');
    const coordination = documentCoordination();
    const oldLease = coordination.capture();
    const transaction = vi.spyOn(IDBDatabase.prototype, 'transaction').mockReturnValueOnce({
      objectStore: () => ({ get: () => ({}) }),
      abort: () => {},
    } as unknown as IDBTransaction);
    try {
      await expect(oldLease.validate()).rejects.toBeInstanceOf(CoordinationUnavailableError);
      expect(oldLease.signal.aborted).toBe(true);
      expect(auth.getSnapshot().status).toBe('loading');
      expect(JSON.parse(localStorage.getItem('cognis-auth-last-verified-user')!).user).toEqual(user);
      expect(() => coordination.capture()).toThrow();
      await vi.waitFor(() => expect(auth.getSnapshot()).toMatchObject({
        status: 'authenticated', offline: false, user,
      }));
      await coordination.capture().validate();
      expect(oldLease.signal.aborted).toBe(true);
    } finally {
      transaction.mockRestore();
    }
  });

  it('refreshes an expired access cookie during startup before declaring the session expired', async () => {
    const user = { email: 'user@example.com', name: 'User', role: 'user' };
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response('{}', { status: 401, headers: jsonHeaders }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        status: 'authenticated', user, expires_at: '2099-01-01T00:00:00Z',
      }), { status: 200, headers: jsonHeaders }));
    await auth.bootstrap();
    expect(auth.getSnapshot()).toMatchObject({ status: 'authenticated', user });
    expect(fetchMock.mock.calls.map(([url]) => String(url).split('/').at(-1))).toEqual(['me', 'refresh']);
    await documentCoordination().capture().validate();
  });

  it('keeps storage outages fail-closed and retries verification when the app returns online', async () => {
    const user = { email: 'user@example.com', name: 'User', role: 'user' };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      new Response(JSON.stringify(user), { status: 200, headers: jsonHeaders }));
    await auth.bootstrap();
    const coordination = documentCoordination(), oldLease = coordination.capture();
    const open = vi.spyOn(indexedDB, 'open').mockImplementation(() => {
      throw new DOMException('Database connection unavailable', 'InvalidStateError');
    });
    try {
      await expect(oldLease.validate()).rejects.toThrow('coordination');
      await vi.waitFor(() => expect(open.mock.calls.length).toBeGreaterThanOrEqual(2));
      expect(auth.getSnapshot()).toMatchObject({ status: 'loading', user: null });
      expect(oldLease.signal.aborted).toBe(true);
      expect(() => coordination.capture()).toThrow();
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(localStorage.getItem('cognis-auth-last-verified-user')).not.toBeNull();
      open.mockRestore();
      window.dispatchEvent(new Event('online'));
      await vi.waitFor(() => expect(auth.getSnapshot()).toMatchObject({ status: 'authenticated', user }));
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(oldLease.signal.aborted).toBe(true);
      await coordination.capture().validate();
    } finally { open.mockRestore(); }
  });

  it('bounds a stalled refresh body and releases bootstrap and the session lock', async () => {
    const user = { email: 'user@example.com', name: 'User', role: 'user' };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify({
      status: 'authenticated', user, expires_at: '2099-01-01T00:00:00Z',
    }), { status: 200, headers: jsonHeaders }));
    await auth.login(user.email, 'password');
    const cancelled = vi.fn();
    const response = new Response(new ReadableStream({ cancel: cancelled }), { status: 200 });
    const getReader = vi.spyOn(response.body!, 'getReader');
    fetchMock.mockResolvedValueOnce(new Response('{}', { status: 401, headers: jsonHeaders }))
      .mockResolvedValueOnce(response);
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const first = auth.bootstrap(), joined = auth.bootstrap();
    await vi.waitFor(() => expect(getReader).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(15_001);
    await Promise.all([first, joined]);
    expect(cancelled).toHaveBeenCalledOnce();
    vi.useRealTimers();
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(user), { status: 200, headers: jsonHeaders }));
    await auth.bootstrap();
    expect(auth.getSnapshot()).toMatchObject({ status: 'authenticated', user, offline: false });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
