import { writable } from 'svelte/store';
import type { UserPreferences } from '$lib/types/api';
import { normalizeUserPreferences } from '$lib/user-preferences';

export type ViewerTheme = 'light' | 'dark' | 'system' | 'app';
export type ViewerAppearance = Pick<UserPreferences['display'], 'theme' | 'theme_family'>;
export const viewerTheme = writable<ViewerTheme | null>(null);
const storageKey = 'cognis-rich-viewer-theme';

/** Read appearance from the browser's own session, never from a public capability. */
export async function fetchHostedAppearance(signal: AbortSignal): Promise<ViewerAppearance | null> {
  const response = await fetch('/api/v1/user-preferences', {
    credentials: 'same-origin', cache: 'no-store', signal,
  });
  if (!response.ok) return null;
  const { theme, theme_family } = normalizeUserPreferences(await response.json()).display;
  return { theme, theme_family };
}

export function resolveViewerTheme(
  preference: ViewerTheme | null,
  surface: 'embedded' | 'standalone',
  appTheme: string | null,
  systemDark: boolean,
  accountAppearance: ViewerAppearance | null = null,
): 'light' | 'dark' {
  if (surface === 'standalone' && preference && preference !== 'app') {
    return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference;
  }
  if (surface === 'standalone' && accountAppearance) {
    return accountAppearance.theme === 'system' ? (systemDark ? 'dark' : 'light') : accountAppearance.theme;
  }
  // In-app documents belong to account appearance, never the external reader preference.
  if (surface === 'embedded'
    && (appTheme === 'light' || appTheme === 'dark')) return appTheme;
  if (surface === 'standalone' && (preference === 'light' || preference === 'dark')) return preference;
  return systemDark ? 'dark' : 'light';
}

export function loadViewerTheme(): void {
  try {
    const value = localStorage.getItem(storageKey);
    viewerTheme.set(value === 'light' || value === 'dark' || value === 'system' || value === 'app' ? value : null);
  } catch {
    // Storage can be unavailable in private or sandboxed viewers.
  }
}

export function setViewerTheme(value: ViewerTheme): void {
  viewerTheme.set(value);
  try { localStorage.setItem(storageKey, value); } catch { /* Session preference still works. */ }
}

/** Watch only theme-bearing ancestors, including the host for embedded documents. */
export function observeViewerTheme(node: Element, changed: () => void): () => void {
  const observer = new MutationObserver(changed);
  for (let ancestor: Element | null = node; ancestor; ancestor = ancestor.parentElement) {
    observer.observe(ancestor, { attributes: true, attributeFilter: ['data-viewer-theme', 'data-resolved-theme', 'data-theme-family'] });
  }
  return () => observer.disconnect();
}
