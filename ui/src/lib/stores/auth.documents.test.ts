import { afterEach, describe, expect, it, vi } from 'vitest';
import { IDBFactory } from 'fake-indexeddb';
import { CoordinationUnavailableError, DocumentCoordination, StaleDocumentError } from '$lib/document-coordination';
import { TestWebLocks } from '../../test/web-locks';

vi.mock('$app/environment', () => ({ browser: true }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const user = { email: 'coordination@cognis.test', name: 'Coordination', role: 'user' };
function json(value: unknown) { return new Response(JSON.stringify(value), { status: 200 }); }

describe('independently mounted auth documents', () => {
  const owners: DocumentCoordination[] = [];
  const changed = new Set<() => void>();
  let locks = new TestWebLocks();
  let indexedDB = new IDBFactory();
  async function mount() {
    const owner = new DocumentCoordination({
      storage: localStorage,
      indexedDB,
      locks: locks as unknown as LockManager,
      profile: () => 'https://coordination.invalid',
      generation: () => 'isolated-document',
      uuid: () => crypto.randomUUID(),
      subscribe(listener) { changed.add(listener); return () => { changed.delete(listener); }; },
      publish() { for (const listener of changed) listener(); },
    });
    owners.push(owner);
    vi.resetModules();
    vi.doMock('$lib/document-coordination', () => ({
      DocumentCoordination,
      StaleDocumentError,
      CoordinationUnavailableError,
      documentCoordination: () => owner,
    }));
    const { auth } = await import('./auth');
    await auth.bootstrap();
    return { auth, owner };
  }

  afterEach(() => {
    owners.splice(0).forEach((owner) => owner.dispose());
    changed.clear();
    locks = new TestWebLocks();
    indexedDB = new IDBFactory();
    localStorage.clear();
    vi.doUnmock('$lib/document-coordination');
    vi.restoreAllMocks();
  });

  it('coalesces overlapping refresh across two stores; the waiter verifies cookies, not broadcast credentials', async () => {
    localStorage.clear();
    const refresh = deferred<Response>();
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) =>
      String(url).endsWith('/refresh') ? refresh.promise : json(user));
    const first = await mount();
    const second = await mount();
    const a = first.auth.refreshSession();
    await vi.waitFor(() => expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/refresh'))).toHaveLength(1));
    const b = second.auth.refreshSession();
    refresh.resolve(json({ user, expires_at: '2030-01-01T00:00:00Z' }));
    expect(await Promise.all([a, b])).toEqual([true, true]);
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/refresh'))).toHaveLength(1);
    expect(first.auth.getSnapshot().user?.email).toBe(user.email);
    expect(second.auth.getSnapshot().user?.email).toBe(user.email);
  });

  it('revokes both stores immediately on logout and rejects a late refresh completion', async () => {
    localStorage.clear();
    const refresh = deferred<Response>();
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) =>
      String(url).endsWith('/refresh') ? refresh.promise : json(user));
    const first = await mount();
    const second = await mount();
    localStorage.setItem('cognis-startup-appearance', '{"v":1}');
    const pending = first.auth.refreshSession();
    await vi.waitFor(() => expect(first.auth.getSnapshot().status).toBe('authenticated'));
    const logout = second.auth.logout();
    await vi.waitFor(() => expect(first.auth.getSnapshot().status).toBe('anonymous'));
    expect(second.auth.getSnapshot().status).toBe('anonymous');
    refresh.resolve(json({ user, expires_at: '2030-01-01T00:00:00Z' }));
    expect(await pending).toBe(false);
    await logout;
    expect(first.auth.getSnapshot().status).toBe('anonymous');
    expect(second.auth.getSnapshot().status).toBe('anonymous');
    expect(localStorage.getItem('cognis-auth-last-verified-user')).toBeNull();
    // Explicit logout also forgets the device's cosmetic startup appearance.
    expect(localStorage.getItem('cognis-startup-appearance')).toBeNull();
  });

  it('a late logout transport completion cannot clear a newer explicit login', async () => {
    const heldLogout = deferred<Response>();
    const newer = { ...user, email: 'newer@cognis.test' };
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/logout')) return heldLogout.promise;
      if (String(url).endsWith('/login')) return json({
        status: 'authenticated', user: newer, expires_at: '2030-01-01T00:00:00Z',
      });
      return json(user);
    });
    const first = await mount();
    const logout = first.auth.logout();
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/logout'))).toBe(true));
    const login = first.auth.login(newer.email, 'isolated-test-password');
    await vi.waitFor(() => expect(first.auth.getSnapshot().status).toBe('loading'));
    heldLogout.resolve(json({}));
    await logout;
    await login;
    expect(first.auth.getSnapshot()).toMatchObject({ status: 'authenticated', user: newer });
    await first.owner.capture().validate();
  });

  it('keeps an explicit logout local even if storage fails before shared revocation commits', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) =>
      String(url).endsWith('/login')
        ? json({ status: 'authenticated', user, expires_at: '2099-01-01T00:00:00Z' }) : json(user));
    const first = await mount(), oldLease = first.owner.capture();
    localStorage.setItem('cognis-startup-appearance', 'retained-cosmetic-hint');
    const open = vi.spyOn(indexedDB, 'open').mockImplementation(() => {
      throw new DOMException('Database unavailable', 'InvalidStateError');
    });
    await expect(first.auth.logout()).rejects.toBeInstanceOf(CoordinationUnavailableError);
    open.mockRestore();
    window.dispatchEvent(new Event('online'));
    await first.auth.bootstrap();
    expect(first.auth.getSnapshot()).toMatchObject({ status: 'anonymous', user: null });
    expect(oldLease.signal.aborted).toBe(true);
    expect(() => first.owner.capture()).toThrow();
    expect(localStorage.getItem('cognis-auth-last-verified-user')).toBeNull();
    expect(localStorage.getItem('cognis-startup-appearance')).toBeNull();
    expect(fetcher).toHaveBeenCalledOnce();
    await first.auth.login(user.email, 'isolated-test-password');
    expect(first.auth.getSnapshot()).toMatchObject({ status: 'authenticated', user });
  });

  it('automatically recovers an initially unavailable document after storage returns', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => json(user));
    const open = vi.spyOn(indexedDB, 'open').mockImplementation(() => {
      throw new DOMException('Database unavailable', 'InvalidStateError');
    });
    const first = await mount();
    await vi.waitFor(() => expect(open.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(first.auth.getSnapshot()).toMatchObject({ status: 'loading', user: null });
    expect(fetcher).not.toHaveBeenCalled();
    open.mockRestore();
    window.dispatchEvent(new Event('online'));
    await vi.waitFor(() => expect(first.auth.getSnapshot()).toMatchObject({ status: 'authenticated', user }));
    await first.owner.capture().validate();
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it('a late refresh failure cannot clear a newer login without event delivery', async () => {
    const heldRefresh = deferred<Response>();
    const newer = { ...user, email: 'newer@cognis.test' };
    const fetcher = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (String(url).endsWith('/refresh')) return heldRefresh.promise;
      if (String(url).endsWith('/login')) return json({
        status: 'authenticated', user: newer, expires_at: '2030-01-01T00:00:00Z',
      });
      return json(user);
    });
    const first = await mount(), second = await mount();
    changed.clear();
    const refresh = first.auth.refreshSession();
    await vi.waitFor(() => expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/refresh'))).toBe(true));
    const login = second.auth.login(newer.email, 'isolated-test-password');
    await vi.waitFor(() => expect(second.auth.getSnapshot().status).toBe('loading'));
    heldRefresh.resolve(new Response('{}', { status: 401 }));
    expect(await refresh).toBe(false);
    await login;
    expect(second.auth.getSnapshot()).toMatchObject({ status: 'authenticated', user: newer });
    await second.owner.capture().validate();
  });
});
