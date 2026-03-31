import { browser } from '$app/environment';
import { writable, get } from 'svelte/store';

import { apiUrl } from '$lib/config';
import { reportError } from '$lib/errors';
import type { AuthStatus, TokenResponse, UserSummary } from '$lib/types/api';
import { toErrorMessage } from '$lib/utils';

const STORAGE_KEY = 'cognis-ui-auth';

export interface AuthState {
  status: AuthStatus;
  initialized: boolean;
  accessToken: string | null;
  refreshToken: string | null;
  expiresAt: number | null;
  user: UserSummary | null;
  error: string | null;
}

interface PersistedAuthState {
  accessToken: string;
  refreshToken: string | null;
  expiresAt: number;
  user: UserSummary;
}

const initialState: AuthState = {
  status: 'loading',
  initialized: false,
  accessToken: null,
  refreshToken: null,
  expiresAt: null,
  user: null,
  error: null
};

const store = writable<AuthState>(initialState);

let bootstrapPromise: Promise<void> | null = null;
let refreshPromise: Promise<string | null> | null = null;

function persistState(state: AuthState): void {
  if (!browser) {
    return;
  }

  if (!state.accessToken || !state.user || state.expiresAt === null) {
    localStorage.removeItem(STORAGE_KEY);
    return;
  }

  const payload: PersistedAuthState = {
    accessToken: state.accessToken,
    refreshToken: state.refreshToken,
    expiresAt: state.expiresAt,
    user: state.user
  };
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

function hydratePersistedState(): void {
  if (!browser) {
    store.set({ ...initialState, status: 'anonymous', initialized: true });
    return;
  }

  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) {
    store.set({ ...initialState, status: 'anonymous', initialized: true });
    return;
  }

  try {
    const persisted = JSON.parse(raw) as PersistedAuthState;
    store.set({
      status: 'authenticated',
      initialized: true,
      accessToken: persisted.accessToken,
      refreshToken: persisted.refreshToken,
      expiresAt: persisted.expiresAt,
      user: persisted.user,
      error: null
    });
  } catch (error) {
    reportError('Failed to parse persisted auth state', error);
    localStorage.removeItem(STORAGE_KEY);
    store.set({ ...initialState, status: 'anonymous', initialized: true });
  }
}

function setAuthenticated(response: TokenResponse): void {
  const expiresAt = Date.now() + response.expires_in * 1000;
  const nextState: AuthState = {
    status: 'authenticated',
    initialized: true,
    accessToken: response.token,
    refreshToken: response.refresh_token,
    expiresAt,
    user: response.user,
    error: null
  };

  persistState(nextState);
  store.set(nextState);
}

function setAnonymous(error: string | null = null): void {
  const nextState: AuthState = {
    status: 'anonymous',
    initialized: true,
    accessToken: null,
    refreshToken: null,
    expiresAt: null,
    user: null,
    error
  };

  persistState(nextState);
  store.set(nextState);
}

function isExpired(state: AuthState): boolean {
  if (state.expiresAt === null) {
    return true;
  }

  return Date.now() >= state.expiresAt - 30_000;
}

async function parseTokenResponse(response: Response): Promise<TokenResponse> {
  return (await response.json()) as TokenResponse;
}

class AuthError extends Error {
  override readonly name = 'AuthError';

  constructor(
    message: string,
    readonly transient: boolean
  ) {
    super(message);
  }
}

async function fetchMe(accessToken: string): Promise<UserSummary> {
  let response: Response;
  try {
    response = await fetch(apiUrl('/api/auth/me'), {
      headers: {
        Authorization: `Bearer ${accessToken}`
      }
    });
  } catch {
    throw new AuthError('Unable to reach the server', true);
  }

  if (response.status === 401) {
    throw new AuthError('Session is no longer valid', false);
  }

  if (!response.ok) {
    throw new AuthError('Failed to verify the current session', true);
  }

  return (await response.json()) as UserSummary;
}

async function refreshAccessToken(): Promise<string | null> {
  const state = get(store);
  if (!state.refreshToken) {
    setAnonymous(null);
    return null;
  }

  let response: Response;
  try {
    response = await fetch(apiUrl('/api/auth/refresh'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ refresh_token: state.refreshToken })
    });
  } catch (error) {
    // Network error (server unreachable) — keep current tokens, don't clear session
    reportError('Token refresh failed (network)', error);
    return null;
  }

  if (response.status === 401) {
    // Definitive auth failure — token is invalid or expired
    setAnonymous('Your session has expired. Please log in again.');
    return null;
  }

  if (!response.ok) {
    // Non-401 HTTP error (4xx other than 401, 5xx) — treat as transient, keep tokens
    reportError('Token refresh failed (server)', new Error(`HTTP ${response.status}`));
    return null;
  }

  const payload = await parseTokenResponse(response);
  setAuthenticated(payload);
  return payload.token;
}

export const auth = {
  subscribe: store.subscribe,

  async bootstrap(): Promise<void> {
    if (!browser) {
      store.set({ ...initialState, status: 'anonymous', initialized: true });
      return;
    }

    if (bootstrapPromise) {
      return bootstrapPromise;
    }

    bootstrapPromise = (async () => {
      hydratePersistedState();
      const state = get(store);
      if (!state.accessToken) {
        return;
      }

      if (isExpired(state)) {
        await this.refreshSession();
        return;
      }

      try {
        const user = await fetchMe(state.accessToken);
        const nextState: AuthState = {
          ...state,
          user,
          status: 'authenticated',
          initialized: true,
          error: null
        };
        persistState(nextState);
        store.set(nextState);
      } catch (error) {
        reportError('Auth bootstrap verification failed', error);
        if (error instanceof AuthError && error.transient) {
          // Server unreachable — keep hydrated session, don't clear tokens.
          // Subsequent bootstrap() calls (e.g. on re-mount) will re-attempt
          // verification. Individual API calls handle 401 independently via
          // the retry logic in client.ts.
          return;
        }
        await this.refreshSession();
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
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ email, password })
      });

      if (!response.ok) {
        throw new Error('Invalid email or password.');
      }

      const payload = await parseTokenResponse(response);
      setAuthenticated(payload);
    } catch (error) {
      const message = toErrorMessage(error, 'Unable to log in.');
      setAnonymous(message);
      throw new Error(message);
    }
  },

  async logout(): Promise<void> {
    const state = get(store);
    try {
      if (state.accessToken) {
        await fetch(apiUrl('/api/auth/logout'), {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${state.accessToken}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ refresh_token: state.refreshToken })
        });
      }
    } catch (error) {
      reportError('Logout request failed', error);
    } finally {
      setAnonymous(null);
    }
  },

  async refreshSession(): Promise<string | null> {
    if (refreshPromise) {
      return refreshPromise;
    }

    refreshPromise = refreshAccessToken();
    try {
      return await refreshPromise;
    } finally {
      refreshPromise = null;
    }
  },

  getSnapshot(): AuthState {
    return get(store);
  },

  getAccessToken(): string | null {
    return get(store).accessToken;
  },

  clear(error: string | null = null): void {
    setAnonymous(error);
  }
};
