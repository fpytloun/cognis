import { browser } from '$app/environment';
import { get, writable } from 'svelte/store';

import { apiUrl } from '$lib/config';
import { fetchWithTimeout, isFetchTimeoutError, readJsonWithSignal, withRequestDeadline } from '$lib/api/fetch';
import { reportError } from '$lib/errors';
import type {
  ApiErrorResponse,
  AuthLoginResponse,
  AuthSessionResponse,
  AuthStatus,
  MfaChallengeResponse,
  MfaSetupResponse,
  UserSummary
} from '$lib/types/api';
import { toErrorMessage } from '$lib/utils';
import { CoordinationUnavailableError, documentCoordination, StaleDocumentError, type DocumentLease } from '$lib/document-coordination';
import { forgetStartupAppearance } from '$lib/theme/startup-appearance';

export interface AuthState {
  status: AuthStatus;
  initialized: boolean;
  expiresAt: number | null;
  user: UserSummary | null;
  error: string | null;
  /**
   * True when the session was restored from the last verified user because
   * the server was unreachable. Re-verified as soon as the network returns.
   */
  offline?: boolean;
}

const initialState: AuthState = {
  status: 'loading',
  initialized: false,
  expiresAt: null,
  user: null,
  error: null
};

const store = writable<AuthState>(initialState);

let bootstrapPromise: Promise<void> | null = null;
let refreshPromise: Promise<boolean> | null = null;
let mfaIntent: { token: string; lease: DocumentLease } | null = null;
const WEB_PUSH_ENABLED_KEY = 'cognis_web_push_enabled';
const WEB_PUSH_WANTED_KEY = 'cognis_web_push_wanted';
const AUTH_REQUEST_TIMEOUT_MS = 15_000;
const LAST_VERIFIED_USER_KEY = 'cognis-auth-last-verified-user';
// An offline session is only trusted for a bounded time after the server
// last confirmed it; past that the user must reconnect to continue.
export const OFFLINE_SESSION_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
const OFFLINE_REVALIDATE_INTERVAL_MS = 30_000;
let offlineRevalidateTimer: ReturnType<typeof setInterval> | null = null;
let offlineListenersInstalled = false;
/** Why the session ended: explicit logout vs. expiry/rejection. */
export type AnonymousReason = 'logout' | 'expired';
const anonymousListeners = new Set<(reason: AnonymousReason) => void>();
let coordinated = false;
let localRevocation = false;
let coordinationUnavailable = false;
let recoveryGeneration = 0;
let coordinationRecovery: Promise<void> | null = null;
let automaticAuthenticationBlocked = false;
function coordination() {
  const owner = documentCoordination();
  if (!coordinated) {
    coordinated = true;
    owner.onInvalidated((reason) => {
      if (reason === 'unavailable') markCoordinationUnavailable(!localRevocation);
      else if (!localRevocation) void setAnonymous(null, 'expired', false);
    });
  }
  return owner;
}

class AuthNetworkError extends Error {}

function markCoordinationUnavailable(recover = true): void {
  coordinationUnavailable = true;
  stopOfflineRevalidation();
  store.set({ ...initialState, initialized: true,
    error: 'Local account coordination is unavailable. Reconnecting safely; your local data is preserved.' });
  installRecoveryListeners();
  if (recover) recoverCoordination();
}

function recoverCoordination(): void {
  if (!browser || automaticAuthenticationBlocked || !coordinationUnavailable || coordinationRecovery) return;
  const generation = recoveryGeneration;
  coordinationRecovery = (async () => {
    // Let aborted requests release their bootstrap and session-lock ownership.
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    if (bootstrapPromise) await bootstrapPromise;
    if (automaticAuthenticationBlocked || !coordinationUnavailable || generation !== recoveryGeneration) return;
    const owner = coordination();
    await owner.initialize();
    if (automaticAuthenticationBlocked || !coordinationUnavailable || generation !== recoveryGeneration) return;
    const lease = owner.capture(false);
    await lease.validate();
    if (!lease.signal.aborted && coordinationUnavailable && generation === recoveryGeneration) {
      await auth.bootstrap();
    }
  })().catch((error: unknown) => {
    if (!(error instanceof StaleDocumentError)) reportError('Local account coordination recovery failed', error);
  }).finally(() => { coordinationRecovery = null; });
}

interface LastVerifiedUser {
  user: UserSummary;
  verified_at: number;
}

function rememberVerifiedUser(user: UserSummary): void {
  if (!browser) return;
  try {
    window.localStorage.setItem(
      LAST_VERIFIED_USER_KEY,
      JSON.stringify({ user, verified_at: Date.now() } satisfies LastVerifiedUser)
    );
  } catch {
    // Storage full or unavailable: offline start just will not be possible.
  }
}

function forgetVerifiedUser(): void {
  if (!browser) return;
  try {
    window.localStorage.removeItem(LAST_VERIFIED_USER_KEY);
  } catch {
    // non-fatal
  }
}

export function readLastVerifiedUser(now = Date.now()): UserSummary | null {
  if (!browser) return null;
  try {
    const raw = window.localStorage.getItem(LAST_VERIFIED_USER_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LastVerifiedUser>;
    if (!parsed?.user?.email || typeof parsed.verified_at !== 'number') return null;
    if (now - parsed.verified_at > OFFLINE_SESSION_MAX_AGE_MS) return null;
    return parsed.user;
  } catch {
    return null;
  }
}

/**
 * Remove this device's push subscription. Only for an explicit logout or an
 * account switch: a mere session expiry keeps the subscription so the same
 * user does not have to re-enable notifications after logging in again.
 */
export async function clearWebPushSubscription(notifyServer: boolean): Promise<void> {
  if (!browser || !('serviceWorker' in navigator) || !('PushManager' in window)) {
    return;
  }
  try {
    const registration = await navigator.serviceWorker.getRegistration();
    if (!registration) return;
    const subscription = await registration.pushManager.getSubscription();
    if (subscription?.endpoint && notifyServer) {
      await fetchWithTimeout(apiUrl('/api/v1/push/subscriptions/unsubscribe'), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: subscription.endpoint })
      }, { timeoutMs: AUTH_REQUEST_TIMEOUT_MS }).catch((error: unknown) => {
        reportError('Push unsubscribe request failed', error);
      });
    }
    await subscription?.unsubscribe();
  } catch (error) {
    reportError('Push subscription cleanup failed', error);
  } finally {
    window.localStorage.removeItem(WEB_PUSH_ENABLED_KEY);
    window.localStorage.removeItem(WEB_PUSH_WANTED_KEY);
  }
}

async function setAuthenticated(user: UserSummary, expiresAt: number | null, lease: DocumentLease, error: string | null = null): Promise<void> {
  if (automaticAuthenticationBlocked) throw new StaleDocumentError();
  if (browser) await coordination().verified(user.email, lease);
  if (automaticAuthenticationBlocked) throw new StaleDocumentError();
  coordinationUnavailable = false;
  recoveryGeneration++;
  stopOfflineRevalidation();
  rememberVerifiedUser(user);
  store.set({
    status: 'authenticated',
    initialized: true,
    expiresAt,
    user,
    error,
    offline: false
  });
}

async function setOfflineAuthenticated(user: UserSummary): Promise<void> {
  if (automaticAuthenticationBlocked) { await setAnonymous(null, 'logout', false); return; }
  if (!await coordination().resumeCachedAccount(user.email)) {
    store.set({ status: 'anonymous', initialized: true, user: null, expiresAt: null,
      error: 'Reconnect to verify this account. Unverified local data remains preserved.' });
    return;
  }
  if (automaticAuthenticationBlocked) { await setAnonymous(null, 'logout', false); return; }
  store.set({
    status: 'authenticated',
    initialized: true,
    expiresAt: null,
    user,
    error: null,
    offline: true
  });
  startOfflineRevalidation();
}

async function setAnonymous(error: string | null = null, reason: AnonymousReason = 'expired', invalidate = true, lease?: DocumentLease): Promise<void> {
  coordinationUnavailable = false;
  recoveryGeneration++;
  if (browser && invalidate) {
    lease ??= coordination().capture(false);
    localRevocation = true;
    try {
      // An unverified bootstrap's 401 is not authority to log out a sibling.
      if (lease.owner) await coordination().invalidate(reason === 'logout', lease);
      else await lease.validate();
    }
    finally { localRevocation = false; }
  }
  stopOfflineRevalidation();
  forgetVerifiedUser();
  // Expiry keeps the cosmetic startup appearance for the same device user.
  if (browser && reason === 'logout') forgetStartupAppearance();
  store.set({
    status: 'anonymous',
    initialized: true,
    expiresAt: null,
    user: null,
    error,
    offline: false
  });
  for (const listener of anonymousListeners) {
    try {
      listener(reason);
    } catch (listenerError) {
      reportError('Auth anonymous listener failed', listenerError);
    }
  }
}

function stopOfflineRevalidation(): void {
  if (offlineRevalidateTimer !== null) clearInterval(offlineRevalidateTimer);
  offlineRevalidateTimer = null;
}

function installRecoveryListeners(): void {
  if (!browser) return;
  if (!offlineListenersInstalled) {
    offlineListenersInstalled = true;
    const resume = () => {
      if (coordinationUnavailable) recoverCoordination();
      else void revalidateOfflineSession();
    };
    window.addEventListener('online', resume);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') resume();
    });
  }
}

function startOfflineRevalidation(): void {
  if (!browser) return;
  installRecoveryListeners();
  stopOfflineRevalidation();
  offlineRevalidateTimer = setInterval(() => {
    if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
    void revalidateOfflineSession();
  }, OFFLINE_REVALIDATE_INTERVAL_MS);
}

let revalidatePromise: Promise<void> | null = null;

/** Confirm an offline-restored session with the server once reachable. */
async function revalidateOfflineSession(): Promise<void> {
  if (!get(store).offline) return;
  if (revalidatePromise) return revalidatePromise;
  revalidatePromise = (async () => {
    const lease = coordination().capture(false);
    try {
      const user = await fetchMe(lease);
      await lease.validate();
      if (!get(store).offline) return;
      if (!user) {
        await setAnonymous('Your session has expired. Please log in again.', 'expired', true, lease);
        return;
      }
      await setAuthenticated(user, null, lease);
    } catch {
      // Still unreachable; keep the offline session and try again later.
    } finally {
      revalidatePromise = null;
    }
  })();
  return revalidatePromise;
}

async function readApiMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = (await response.json()) as ApiErrorResponse;
    if (payload?.error?.message) {
      return payload.error.message;
    }
    if (typeof payload?.detail === 'string') {
      return payload.detail;
    }
    if (payload?.detail && typeof payload.detail === 'object' && 'message' in payload.detail) {
      const detailMessage = payload.detail.message;
      if (typeof detailMessage === 'string' && detailMessage.trim()) {
        return detailMessage;
      }
    }
  } catch {
    // Keep fallback.
  }
  return fallback;
}

async function fetchMe(lease = coordination().capture(false)): Promise<UserSummary | null> {
  try {
    return await withRequestDeadline(
      signal => fetchMeWithinDeadline(lease, signal),
      { signal: lease.signal, timeoutMs: AUTH_REQUEST_TIMEOUT_MS },
    );
  } catch (error) {
    if (isFetchTimeoutError(error)) throw new AuthNetworkError(error.message);
    throw error;
  }
}

async function fetchMeWithinDeadline(lease: DocumentLease, signal: AbortSignal): Promise<UserSummary | null> {
  await lease.validate();
  signal.throwIfAborted();
  let response: Response;
  try {
    response = await fetchWithTimeout(apiUrl('/api/auth/me'), {
      credentials: 'include',
      signal
    }, { timeoutMs: AUTH_REQUEST_TIMEOUT_MS });
  } catch (error) {
    throw new AuthNetworkError(toErrorMessage(error, 'Unable to reach the server.'));
  }
  await lease.validate();
  signal.throwIfAborted();

  if (response.status === 401) {
    return null;
  }

  if (response.status === 502 || response.status === 503 || response.status === 504) {
    // Gateway/controller unavailable (deploy, outage): not a session verdict.
    throw new AuthNetworkError('The server is temporarily unavailable.');
  }

  if (!response.ok) {
    throw new Error(await readApiMessage(response, 'Failed to verify the current session.'));
  }

  const user = (await readJsonWithSignal(response, signal)) as UserSummary;
  await lease.validate();
  signal.throwIfAborted();
  return user;
}

async function parseAuthSessionResponse(response: Response): Promise<AuthSessionResponse> {
  return (await response.json()) as AuthSessionResponse;
}

async function refreshBrowserSession(lease: DocumentLease): Promise<boolean> {
  await lease.validate();
  let response: Response;
  let payload: AuthSessionResponse | null;
  try {
    const result = await withRequestDeadline(async (signal) => {
      const response = await fetchWithTimeout(apiUrl('/api/auth/refresh'), {
        method: 'POST', credentials: 'include', signal,
      }, { timeoutMs: 0 });
      if (!response.ok) {
        void response.body?.cancel().catch(() => {});
        return { response, payload: null };
      }
      const payload = await readJsonWithSignal(response, signal) as AuthSessionResponse;
      signal.throwIfAborted();
      return { response, payload };
    }, { signal: lease.signal, timeoutMs: AUTH_REQUEST_TIMEOUT_MS });
    response = result.response;
    payload = result.payload;
  } catch (error) {
    reportError('Session refresh failed', error);
    return false;
  }
  await lease.validate();

  if (response.status === 401) {
    await setAnonymous('Your session has expired. Please log in again.', 'expired', true, lease);
    return false;
  }

  if (!response.ok || !payload) {
    reportError('Session refresh failed', new Error(`HTTP ${response.status}`));
    return false;
  }

  await lease.validate();
  if (lease.owner && payload.user.email !== get(store).user?.email) {
    await setAnonymous('The signed-in account changed. Please verify your session.', 'expired', true, lease);
    return false;
  }
  await setAuthenticated(payload.user, Date.parse(payload.expires_at), lease);
  await coordination().refreshed(lease.owner ? lease : coordination().capture());
  return true;
}

export const auth = {
  subscribe: store.subscribe,

  async bootstrap(): Promise<void> {
    if (automaticAuthenticationBlocked) {
      await setAnonymous(get(store).error, 'logout', false);
      return;
    }
    if (!browser) {
      await setAnonymous(null, 'expired', false);
      return;
    }

    if (bootstrapPromise) {
      return bootstrapPromise;
    }

    bootstrapPromise = (async () => {
      const owner = coordination();
      try { await owner.initialize(); }
      catch (error) {
        if (error instanceof CoordinationUnavailableError) { markCoordinationUnavailable(); return; }
        await setAnonymous(toErrorMessage(error, 'Coordination unavailable.'), 'expired', false);
        return;
      }
      if (owner.blocked) { await setAnonymous(null, 'expired', false); return; }
      const lease = owner.capture(false);
      const current = get(store);
      if (!current.initialized) {
        store.set({ ...initialState });
      }

      try {
        await owner.run('session', lease, async () => {
          const user = await fetchMe(lease);
          await lease.validate();
          if (user) {
            await setAuthenticated(user, current.expiresAt, lease);
          } else if (!await refreshBrowserSession(lease) && get(store).status !== 'anonymous') {
            throw new AuthNetworkError('Unable to refresh your session.');
          }
        });
      } catch (error) {
        if (error instanceof StaleDocumentError || lease.signal.aborted) return;
        // Unreachable server: start with the last verified user so the
        // locally cached workspace stays usable offline.
        const lastUser = error instanceof AuthNetworkError ? readLastVerifiedUser() : null;
        if (lastUser) {
          await setOfflineAuthenticated(lastUser);
          return;
        }
        reportError('Auth bootstrap verification failed', error);
        await setAnonymous(toErrorMessage(error, 'Unable to verify your session.'), 'expired', false);
      }
    })();

    try {
      await bootstrapPromise;
    } finally {
      bootstrapPromise = null;
    }
  },

  async login(email: string, password: string): Promise<MfaChallengeResponse | null> {
    const owner = coordination();
    automaticAuthenticationBlocked = true;
    recoveryGeneration++;
    localRevocation = true;
    try { await owner.beginLogin(); }
    finally { localRevocation = false; }
    automaticAuthenticationBlocked = false;
    const lease = owner.capture(false);
    mfaIntent = null;
    store.update((state) => ({ ...state, status: 'loading', error: null }));

    try {
      return await owner.run('session', lease, async () => {
      const response = await fetchWithTimeout(apiUrl('/api/auth/login'), {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ email, password }),
        signal: lease.signal
      }, { timeoutMs: AUTH_REQUEST_TIMEOUT_MS });

      if (!response.ok) {
        throw new Error(await readApiMessage(response, 'Unable to log in.'));
      }

      const payload = (await response.json()) as AuthLoginResponse;
      await lease.validate();
      if (payload.status !== 'authenticated') {
        mfaIntent = { token: payload.challenge_token, lease };
        await setAnonymous(null, 'expired', false);
        return payload;
      }
      await setAuthenticated(payload.user, Date.parse(payload.expires_at), lease);
      return null;
      });
    } catch (error) {
      if (error instanceof StaleDocumentError || lease.signal.aborted) throw error;
      const message = toErrorMessage(error, 'Unable to log in.');
      await setAnonymous(message, 'expired', true, lease);
      throw new Error(message);
    }
  },

  async startMfaSetup(challengeToken: string): Promise<MfaSetupResponse> {
    const response = await fetchWithTimeout(apiUrl('/api/auth/mfa/setup/start'), {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ challenge_token: challengeToken })
    }, { timeoutMs: AUTH_REQUEST_TIMEOUT_MS });
    if (!response.ok) {
      throw new Error(await readApiMessage(response, 'Unable to start MFA setup.'));
    }
    return (await response.json()) as MfaSetupResponse;
  },

  async completeMfa(
    challengeToken: string,
    code: string,
    setup: boolean
  ): Promise<AuthSessionResponse> {
    const owner = coordination();
    if (!mfaIntent || mfaIntent.token !== challengeToken) throw new StaleDocumentError();
    const lease = mfaIntent.lease;
    return owner.run('session', lease, async () => {
    const response = await fetchWithTimeout(
      apiUrl(setup ? '/api/auth/mfa/setup/confirm' : '/api/auth/mfa/verify'),
      {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ challenge_token: challengeToken, code }),
        signal: lease.signal
      },
      { timeoutMs: AUTH_REQUEST_TIMEOUT_MS }
    );
    if (!response.ok) {
      throw new Error(await readApiMessage(response, 'Invalid authentication code.'));
    }
    const payload = await parseAuthSessionResponse(response);
    await lease.validate();
    await setAuthenticated(payload.user, Date.parse(payload.expires_at), lease);
    mfaIntent = null;
    return payload;
    });
  },

  async logout(): Promise<void> {
    const owner = coordination();
    automaticAuthenticationBlocked = true;
    // Revocation is immediate and never waits behind uploads/refresh/network.
    recoveryGeneration++;
    localRevocation = true;
    try { await owner.invalidate(); }
    catch (error) {
      await setAnonymous(toErrorMessage(error, 'Unable to complete logout safely.'), 'logout', false);
      throw error;
    }
    finally { localRevocation = false; }
    await setAnonymous(null, 'logout', false);
    const lease = owner.capture(false);
    try {
      await owner.run('session', lease, async () => {
      await clearWebPushSubscription(true);
      await lease.validate();
      await fetchWithTimeout(apiUrl('/api/auth/logout'), {
        method: 'POST',
        credentials: 'include',
        signal: lease.signal
      }, { timeoutMs: AUTH_REQUEST_TIMEOUT_MS });
      });
    } catch (error) {
      reportError('Logout request failed', error);
    } finally {
      // A later explicit login may already own a new epoch.
      try {
        await lease.validate();
        await setAnonymous(null, 'logout', false);
      } catch { /* A newer intent owns the session. */ }
    }
  },

  async refreshSession(): Promise<boolean> {
    if (refreshPromise) {
      return refreshPromise;
    }

    const owner = coordination();
    const lease = owner.capture();
    const version = owner.refreshVersion;
    refreshPromise = owner.run('session', lease, async () => {
      if (version !== owner.refreshVersion) {
        const user = await fetchMe(lease);
        await lease.validate();
        if (!user || user.email !== get(store).user?.email) {
          await setAnonymous('Your session has expired. Please log in again.', 'expired', true, lease);
          return false;
        }
        await setAuthenticated(user, get(store).expiresAt, lease);
        return true;
      }
      return refreshBrowserSession(lease);
    }).catch((error: unknown) => {
      if (!(error instanceof StaleDocumentError)) reportError('Session coordination failed', error);
      return false;
    });
    try {
      return await refreshPromise;
    } finally {
      refreshPromise = null;
    }
  },

  /**
   * Re-validate the browser session after an API 401. Concurrent callers
   * share one refresh request so a burst of 401s (common when a mobile app
   * resumes) cannot race each other into a logout.
   *
   * - `valid`: the session was refreshed; the caller may retry once.
   * - `expired`: the server rejected the session; auth is now anonymous.
   * - `unknown`: refresh could not reach the server; keep the session.
   */
  async recoverSession(): Promise<'valid' | 'expired' | 'unknown'> {
    if (!browser) return 'unknown';
    if (get(store).status === 'anonymous') return 'expired';
    const refreshed = await auth.refreshSession();
    if (refreshed) return 'valid';
    return get(store).status === 'anonymous' ? 'expired' : 'unknown';
  },

  /** Run `listener` whenever the session ends (logout, expiry, rejection). */
  onAnonymous(listener: (reason: AnonymousReason) => void): () => void {
    anonymousListeners.add(listener);
    return () => anonymousListeners.delete(listener);
  },

  /** Re-verify an offline-restored session now (e.g. on reconnect). */
  revalidate(): Promise<void> {
    return revalidateOfflineSession();
  },

  getSnapshot(): AuthState {
    return get(store);
  },

  updateUser(user: UserSummary): void {
    store.update((state) => ({
      ...state,
      user,
      status: user ? 'authenticated' : 'anonymous'
    }));
  },

  clear(error: string | null = null, expected?: DocumentLease): Promise<void> {
    if (!browser || !coordination().owner) return setAnonymous(error, 'expired', false);
    const lease = expected ?? coordination().capture();
    return setAnonymous(error, 'expired', true, lease).catch((failure) => {
      if (!(failure instanceof StaleDocumentError)) throw failure;
    });
  }
};
