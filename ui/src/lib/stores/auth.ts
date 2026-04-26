import { browser } from '$app/environment';
import { get, writable } from 'svelte/store';

import { apiUrl } from '$lib/config';
import { reportError } from '$lib/errors';
import type { ApiErrorResponse, AuthSessionResponse, AuthStatus, UserSummary } from '$lib/types/api';
import { toErrorMessage } from '$lib/utils';

export interface AuthState {
  status: AuthStatus;
  initialized: boolean;
  expiresAt: number | null;
  user: UserSummary | null;
  error: string | null;
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
const WEB_PUSH_ENABLED_KEY = 'cognis_web_push_enabled';

async function clearWebPushSubscription(notifyServer: boolean): Promise<void> {
  if (!browser || !('serviceWorker' in navigator) || !('PushManager' in window)) {
    return;
  }
  try {
    const registration = await navigator.serviceWorker.getRegistration();
    if (!registration) return;
    const subscription = await registration.pushManager.getSubscription();
    if (subscription?.endpoint && notifyServer) {
      await fetch(apiUrl('/api/v1/push/subscriptions/unsubscribe'), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: subscription.endpoint })
      }).catch((error: unknown) => {
        reportError('Push unsubscribe request failed', error);
      });
    }
    await subscription?.unsubscribe();
  } catch (error) {
    reportError('Push subscription cleanup failed', error);
  } finally {
    window.localStorage.removeItem(WEB_PUSH_ENABLED_KEY);
  }
}

function setAuthenticated(user: UserSummary, expiresAt: number | null, error: string | null = null): void {
  store.set({
    status: 'authenticated',
    initialized: true,
    expiresAt,
    user,
    error
  });
}

function setAnonymous(error: string | null = null): void {
  store.set({
    status: 'anonymous',
    initialized: true,
    expiresAt: null,
    user: null,
    error
  });
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

async function fetchMe(): Promise<UserSummary | null> {
  let response: Response;
  try {
    response = await fetch(apiUrl('/api/auth/me'), {
      credentials: 'include'
    });
  } catch (error) {
    throw new Error(toErrorMessage(error, 'Unable to reach the server.'));
  }

  if (response.status === 401) {
    return null;
  }

  if (!response.ok) {
    throw new Error(await readApiMessage(response, 'Failed to verify the current session.'));
  }

  return (await response.json()) as UserSummary;
}

async function parseAuthSessionResponse(response: Response): Promise<AuthSessionResponse> {
  return (await response.json()) as AuthSessionResponse;
}

async function refreshBrowserSession(): Promise<boolean> {
  let response: Response;
  try {
    response = await fetch(apiUrl('/api/auth/refresh'), {
      method: 'POST',
      credentials: 'include'
    });
  } catch (error) {
    reportError('Session refresh failed', error);
    return false;
  }

  if (response.status === 401) {
    void clearWebPushSubscription(false);
    setAnonymous('Your session has expired. Please log in again.');
    return false;
  }

  if (!response.ok) {
    reportError('Session refresh failed', new Error(`HTTP ${response.status}`));
    return false;
  }

  const payload = await parseAuthSessionResponse(response);
  setAuthenticated(payload.user, Date.parse(payload.expires_at));
  return true;
}

export const auth = {
  subscribe: store.subscribe,

  async bootstrap(): Promise<void> {
    if (!browser) {
      setAnonymous(null);
      return;
    }

    if (bootstrapPromise) {
      return bootstrapPromise;
    }

    bootstrapPromise = (async () => {
      const current = get(store);
      if (!current.initialized) {
        store.set({ ...initialState });
      }

      try {
        const user = await fetchMe();
        if (!user) {
          setAnonymous(null);
          return;
        }
        setAuthenticated(user, current.expiresAt, null);
      } catch (error) {
        reportError('Auth bootstrap verification failed', error);
        setAnonymous(toErrorMessage(error, 'Unable to verify your session.'));
      }
    })();

    try {
      await bootstrapPromise;
    } finally {
      bootstrapPromise = null;
    }
  },

  async login(email: string, password: string): Promise<void> {
    store.update((state) => ({ ...state, status: 'loading', error: null }));

    try {
      const response = await fetch(apiUrl('/api/auth/login'), {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ email, password })
      });

      if (!response.ok) {
        throw new Error(await readApiMessage(response, 'Unable to log in.'));
      }

      const payload = await parseAuthSessionResponse(response);
      setAuthenticated(payload.user, Date.parse(payload.expires_at));
    } catch (error) {
      const message = toErrorMessage(error, 'Unable to log in.');
      setAnonymous(message);
      throw new Error(message);
    }
  },

  async logout(): Promise<void> {
    try {
      await clearWebPushSubscription(true);
      await fetch(apiUrl('/api/auth/logout'), {
        method: 'POST',
        credentials: 'include'
      });
    } catch (error) {
      reportError('Logout request failed', error);
    } finally {
      setAnonymous(null);
    }
  },

  async refreshSession(): Promise<boolean> {
    if (refreshPromise) {
      return refreshPromise;
    }

    refreshPromise = refreshBrowserSession();
    try {
      return await refreshPromise;
    } finally {
      refreshPromise = null;
    }
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

  clear(error: string | null = null): void {
    void clearWebPushSubscription(false);
    setAnonymous(error);
  }
};
