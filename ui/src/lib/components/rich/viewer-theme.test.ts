import { describe, expect, it } from 'vitest';
import { resolveViewerTheme } from './viewer-theme';

describe('resolveViewerTheme', () => {
  it('defaults embedded documents to the app and standalone documents to the OS', () => {
    expect(resolveViewerTheme(null, 'embedded', 'light', true)).toBe('light');
    expect(resolveViewerTheme(null, 'embedded', 'dark', false)).toBe('dark');
    expect(resolveViewerTheme(null, 'standalone', 'light', true)).toBe('dark');
    expect(resolveViewerTheme(null, 'standalone', 'dark', false)).toBe('light');
  });

  it('ignores every saved external reader preference inside the app', () => {
    for (const preference of [null, 'system', 'app', 'light', 'dark'] as const) {
      expect(resolveViewerTheme(preference, 'embedded', 'light', true)).toBe('light');
      expect(resolveViewerTheme(preference, 'embedded', 'dark', false)).toBe('dark');
      expect(resolveViewerTheme(preference, 'embedded', null, false)).toBe('light');
    }
  });

  it('keeps standalone reader preferences independent of app theme', () => {
    expect(resolveViewerTheme('system', 'standalone', 'light', true)).toBe('dark');
    expect(resolveViewerTheme('system', 'standalone', 'dark', false)).toBe('light');
    expect(resolveViewerTheme('light', 'standalone', 'dark', true)).toBe('light');
    expect(resolveViewerTheme('dark', 'standalone', 'light', false)).toBe('dark');
  });

  it('supports app mode without invalid standalone or missing-root states', () => {
    expect(resolveViewerTheme('app', 'embedded', 'light', true)).toBe('light');
    expect(resolveViewerTheme('app', 'standalone', 'light', true)).toBe('dark');
    expect(resolveViewerTheme('app', 'embedded', null, false)).toBe('light');
  });

  it('lets reader controls override the account default without changing the account', () => {
    expect(resolveViewerTheme(null, 'standalone', null, true, { theme: 'light', theme_family: 'paper' })).toBe('light');
    expect(resolveViewerTheme('dark', 'standalone', null, true, { theme: 'light', theme_family: 'paper' })).toBe('dark');
    expect(resolveViewerTheme('light', 'standalone', null, false, { theme: 'dark', theme_family: 'gruvbox' })).toBe('light');
    expect(resolveViewerTheme('system', 'standalone', null, true, { theme: 'light', theme_family: 'classic' })).toBe('dark');
    expect(resolveViewerTheme('system', 'standalone', null, false, { theme: 'dark', theme_family: 'classic' })).toBe('light');
  });
});
