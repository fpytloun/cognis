import { browser } from '$app/environment';
import { get, writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
import { reportError } from '$lib/errors';
import { auth } from '$lib/stores/auth';
import type { UserPreferences } from '$lib/types/api';
import { DEFAULT_USER_PREFERENCES, normalizeUserPreferences } from '$lib/user-preferences';

const STORAGE_KEY_PREFIX = 'cognis_user_preferences';
const defaults = () => structuredClone(DEFAULT_USER_PREFERENCES);
const store = writable<UserPreferences>(defaults());
const readyStore = writable(false);
/** True while the store holds a verified account's (cached or loaded) value. */
const ownedStore = writable(false);

interface PreferenceOwner {
  lease: DocumentLease;
  key: string;
  mutation: number;
  saves: number;
  confirmed: UserPreferences;
  load: Promise<UserPreferences> | null;
  saveTail: Promise<unknown>;
  refreshRequested: boolean;
}
let owner: PreferenceOwner | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | undefined;

export const userPreferences = { subscribe: store.subscribe };
export const userPreferencesReady = { subscribe: readyStore.subscribe };
export const userPreferencesOwned = { subscribe: ownedStore.subscribe };

function reset(): void {
  owner = null;
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  ownedStore.set(false);
  store.set(defaults());
  readyStore.set(false);
}

function current(scope: PreferenceOwner): boolean {
  if (owner !== scope) return false;
  try { scope.lease.assertCurrent(); return true; } catch { reset(); return false; }
}

function readCache(key: string): UserPreferences | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) || 'null');
    return value && typeof value === 'object' && !Array.isArray(value)
      ? normalizeUserPreferences(value) : null;
  } catch { return null; }
}

function cache(value: UserPreferences, scope: PreferenceOwner): void {
  if (!current(scope)) return;
  try {
    const serialized = JSON.stringify(value);
    // An unchanged refresh must not trigger another document's refresh.
    if (localStorage.getItem(scope.key) !== serialized) localStorage.setItem(scope.key, serialized);
  } catch (error) { reportError('Unable to cache user preferences', error); }
}

function bind(userEmail?: string | null): PreferenceOwner | null {
  if (!browser) return null;
  try {
    const coordination = documentCoordination();
    const lease = coordination.capture();
    if (!lease.owner) throw new Error('Preferences require a verified account');
    // Late route callbacks must not switch the authenticated document's owner.
    if (userEmail && !lease.owner.endsWith(`\n${userEmail.trim().toLowerCase()}`)) return null;
    if (owner?.lease.owner === lease.owner && owner.lease.epoch === lease.epoch && current(owner)) return owner;
    reset();
    const key = coordination.storageName(STORAGE_KEY_PREFIX, lease);
    const cached = readCache(key);
    owner = { lease, key, mutation: 0, saves: 0, confirmed: cached ?? defaults(), load: null,
      saveTail: Promise.resolve(), refreshRequested: false };
    store.set(cached ?? defaults());
    // After the value, so owned subscribers never observe the reset defaults.
    ownedStore.set(cached !== null);
    return owner;
  } catch { reset(); return null; }
}

function requestRefresh(): void {
  const scope = owner;
  if (!scope || !current(scope)) return;
  scope.refreshRequested = true;
  if (scope.saves || scope.load || refreshTimer !== undefined) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = undefined;
    if (current(scope)) void loadUserPreferences();
  }, 0);
}

export async function loadUserPreferences(userEmail?: string | null): Promise<UserPreferences> {
  const scope = bind(userEmail);
  if (!scope) return get(store);
  if (scope.saves) { scope.refreshRequested = true; return get(store); }
  if (scope.load) return scope.load;
  clearTimeout(refreshTimer);
  refreshTimer = undefined;
  scope.refreshRequested = false;
  const mutation = scope.mutation;
  const pending = (async () => {
    try {
      await scope.lease.validate();
      if (!current(scope)) return get(store);
      const value = normalizeUserPreferences(await api.userPreferences.get());
      await scope.lease.validate();
      if (current(scope) && mutation === scope.mutation) {
        scope.confirmed = value;
        store.set(value);
        ownedStore.set(true);
        cache(value, scope);
        readyStore.set(true);
      }
    } catch (error) {
      if (current(scope) && mutation === scope.mutation) {
        reportError('Unable to load user preferences', error);
        readyStore.set(true);
      }
    } finally {
      scope.load = null;
      if (current(scope) && scope.refreshRequested) requestRefresh();
    }
    return get(store);
  })();
  scope.load = pending;
  return pending;
}

export async function saveUserPreferences(next: UserPreferences): Promise<UserPreferences> {
  const scope = bind();
  if (!scope) throw new Error('Preferences require a verified account');
  const mutation = ++scope.mutation;
  const normalized = normalizeUserPreferences(next);
  scope.saves++;
  store.set(normalized);
  // Serialize writes so an older request cannot win at the server. Only the
  // newest optimistic value (or its confirmed rollback) is rendered locally.
  const pending = scope.saveTail.catch(() => undefined).then(async () => {
    try {
      await scope.lease.validate();
      if (!current(scope)) throw new Error('Preference ownership changed');
      const saved = normalizeUserPreferences(await api.userPreferences.update(normalized));
      await scope.lease.validate();
      if (current(scope)) {
        scope.confirmed = saved;
        cache(saved, scope);
        if (mutation === scope.mutation) {
          store.set(saved);
          ownedStore.set(true);
          readyStore.set(true);
        }
      }
      return saved;
    } catch (error) {
      if (current(scope) && mutation === scope.mutation) store.set(scope.confirmed);
      throw error;
    } finally {
      scope.saves--;
      if (current(scope) && scope.refreshRequested) requestRefresh();
    }
  });
  scope.saveTail = pending;
  return pending;
}

/** One root document lifecycle, including native secondary documents. */
export function startUserPreferences(): () => void {
  if (!browser) return () => {};
  const refresh = () => {
    const state = get(auth);
    if (state.status !== 'authenticated' || !state.user) { reset(); return; }
    void loadUserPreferences(state.user.email);
  };
  const stopInvalidation = documentCoordination().onInvalidated(reset);
  const stopAuth = auth.subscribe(refresh);
  const storage = (event: StorageEvent) => {
    if (event.storageArea === localStorage && owner && (event.key === owner.key || event.key === null)) {
      // Treat cross-document data only as a hint: revalidate the captured
      // profile/account/epoch and read the canonical API, never event.newValue.
      requestRefresh();
    }
  };
  const foreground = () => {
    if (document.visibilityState === 'visible' && bind()) requestRefresh();
  };
  window.addEventListener('storage', storage);
  window.addEventListener('focus', foreground);
  document.addEventListener('visibilitychange', foreground);
  return () => {
    stopAuth(); stopInvalidation();
    window.removeEventListener('storage', storage);
    window.removeEventListener('focus', foreground);
    document.removeEventListener('visibilitychange', foreground);
    reset();
  };
}
