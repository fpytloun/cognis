import { userPreferences, userPreferencesOwned } from '$lib/stores/userPreferences';
import { readStartupAppearance, rememberStartupAppearance, STARTUP_APPEARANCE_KEY } from './startup-appearance';
import type { UserPreferences } from '$lib/types/api';
import { DEFAULT_THEME_FAMILY, type ThemeFamily } from './catalog';

export type AppearancePreference = UserPreferences['display']['theme'];
export type ResolvedTheme = 'light' | 'dark';

export function resolveTheme(preference: AppearancePreference, systemDark: boolean): ResolvedTheme {
  return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference;
}

/** Project a resolved appearance without changing account preferences. */
export function projectAppearance(family: ThemeFamily, theme: ResolvedTheme): void {
  const root = document.documentElement;
  root.dataset.themeFamily = family;
  root.dataset.resolvedTheme = theme;
  root.classList.toggle('dark', theme === 'dark');
  root.style.colorScheme = theme;
  const chrome = getComputedStyle(root).getPropertyValue('--app-chrome-bg').trim();
  for (const [name, content] of [['color-scheme', theme], ['theme-color', chrome]]) {
    let meta = document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`);
    if (!meta) {
      meta = document.createElement('meta');
      meta.name = name;
      document.head.appendChild(meta);
    }
    meta.content = content;
  }
}

/**
 * Projection only: the preference store owns account binding, persistence and
 * rollback. Unowned preferences never project account data; retain only the
 * cosmetic startup hint until verified preferences bind. Explicit logout
 * removes that hint, so the defaults apply immediately.
 */
export function startAppearance(): () => void {
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let preference: AppearancePreference = 'system';
  let family: ThemeFamily = DEFAULT_THEME_FAMILY;
  let owned = false;
  const apply = () => {
    if (owned) {
      rememberStartupAppearance(family, preference);
    } else {
      const hint = readStartupAppearance();
      if (hint) {
        const mode = resolveTheme(hint.theme, system.matches);
        projectAppearance(hint[mode].family, mode);
        return;
      }
    }
    projectAppearance(family, resolveTheme(preference, system.matches));
  };
  system.addEventListener('change', apply);
  const storage = (event: StorageEvent) => {
    if (!owned && event.storageArea === localStorage && (event.key === STARTUP_APPEARANCE_KEY || event.key === null)) apply();
  };
  window.addEventListener('storage', storage);
  const stopPreferences = userPreferences.subscribe((value) => {
    preference = value.display.theme;
    family = value.display.theme_family;
    apply();
  });
  const stopOwned = userPreferencesOwned.subscribe((value) => {
    owned = value;
    apply();
  });
  return () => {
    stopPreferences();
    stopOwned();
    system.removeEventListener('change', apply);
    window.removeEventListener('storage', storage);
  };
}
