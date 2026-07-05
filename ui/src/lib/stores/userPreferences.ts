import { browser } from '$app/environment';
import { writable } from 'svelte/store';

import { api } from '$lib/api/client';
import { reportError } from '$lib/errors';
import type { UserPreferences } from '$lib/types/api';
import { DEFAULT_USER_PREFERENCES, normalizeUserPreferences } from '$lib/user-preferences';

const STORAGE_KEY_PREFIX = 'cognis_user_preferences';

function storageKey(userEmail: string | null | undefined): string | null {
  const normalized = userEmail?.trim().toLowerCase();
  return normalized ? `${STORAGE_KEY_PREFIX}:${normalized}` : null;
}

function readCachedPreferences(userEmail?: string | null): UserPreferences {
  if (!browser) {
    return structuredClone(DEFAULT_USER_PREFERENCES);
  }
  const key = storageKey(userEmail);
  if (!key) {
    return structuredClone(DEFAULT_USER_PREFERENCES);
  }
  try {
    return normalizeUserPreferences(JSON.parse(window.localStorage.getItem(key) || 'null'));
  } catch {
    return structuredClone(DEFAULT_USER_PREFERENCES);
  }
}

function cachePreferences(value: UserPreferences, userEmail?: string | null): void {
  if (!browser) return;
  const key = storageKey(userEmail);
  if (!key) return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch (error) {
    reportError('Unable to cache user preferences', error);
  }
}

const store = writable<UserPreferences>(structuredClone(DEFAULT_USER_PREFERENCES));
let loadPromise: Promise<UserPreferences> | null = null;
let loadedUserEmail: string | null = null;

export const userPreferences = {
  subscribe: store.subscribe
};

export async function loadUserPreferences(userEmail?: string | null): Promise<UserPreferences> {
  const normalizedEmail = userEmail?.trim().toLowerCase() || null;
  if (normalizedEmail !== loadedUserEmail) {
    loadedUserEmail = normalizedEmail;
    store.set(readCachedPreferences(normalizedEmail));
    loadPromise = null;
  }
  if (loadPromise) {
    return loadPromise;
  }
  loadPromise = api.userPreferences.get()
    .then((value) => {
      const normalized = normalizeUserPreferences(value);
      store.set(normalized);
      cachePreferences(normalized, normalizedEmail);
      return normalized;
    })
    .catch((error: unknown) => {
      reportError('Unable to load user preferences', error);
      return readCachedPreferences(normalizedEmail);
    })
    .finally(() => {
      loadPromise = null;
    });
  return loadPromise;
}

export async function saveUserPreferences(next: UserPreferences): Promise<UserPreferences> {
  const normalized = normalizeUserPreferences(next);
  store.set(normalized);
  cachePreferences(normalized, loadedUserEmail);
  const saved = normalizeUserPreferences(await api.userPreferences.update(normalized));
  store.set(saved);
  cachePreferences(saved, loadedUserEmail);
  return saved;
}
