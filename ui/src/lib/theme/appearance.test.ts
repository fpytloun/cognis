import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
import type { UserPreferences } from '$lib/types/api';
import { THEME_FAMILIES, type ThemeFamily } from './catalog';

const environment = vi.hoisted(() => ({
  owner: 'https://controller.test\nfirst@cognis.test' as string | null,
  epoch: 'first-epoch',
  listeners: new Set<() => void>()
}));
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api/client', () => ({ api: { userPreferences: { get: vi.fn(), update: vi.fn() } } }));
vi.mock('$lib/errors', () => ({ reportError: vi.fn() }));
vi.mock('$lib/stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { auth: writable({ status: 'anonymous', user: null }) };
});
vi.mock('$lib/document-coordination', () => ({
  documentCoordination: () => ({
    capture: () => {
      const owner = environment.owner, epoch = environment.epoch;
      const assertCurrent = () => {
        if (!owner || owner !== environment.owner || epoch !== environment.epoch) throw new Error('Stale document');
      };
      assertCurrent();
      return { owner, epoch, assertCurrent, validate: async () => assertCurrent() };
    },
    storageName: (name: string) => `${name}:account-v2:${encodeURIComponent(environment.owner!)}`,
    onInvalidated: (listener: () => void) => {
      environment.listeners.add(listener);
      return () => environment.listeners.delete(listener);
    }
  })
}));

// Test projection against a controlled palette; palette values have separate tests.
const tokens = `
  :root { --app-chrome-bg: #edf2f5; }
  :root[data-resolved-theme="dark"] { --app-chrome-bg: #07111a; }
  :root[data-theme-family="paper"][data-resolved-theme="light"] { --app-chrome-bg: #eeeadd; }
  :root[data-theme-family="paper"][data-resolved-theme="dark"] { --app-chrome-bg: #272620; }
`;
const html = readFileSync('src/app.html', 'utf8');
let system: EventTarget & { matches: boolean };
let stops: Array<() => void>;
function prefs(theme: UserPreferences['display']['theme'], family: ThemeFamily = 'classic'): UserPreferences {
  const value = structuredClone(DEFAULT_USER_PREFERENCES);
  value.display.theme = theme;
  value.display.theme_family = family;
  return value;
}
function resolved() { return document.documentElement.dataset.resolvedTheme; }
function changeSystem(dark: boolean) {
  system.matches = dark;
  system.dispatchEvent(new Event('change'));
}
beforeEach(async () => {
  vi.resetModules();
  vi.clearAllMocks();
  localStorage.clear();
  environment.owner = 'https://controller.test\nfirst@cognis.test';
  environment.epoch = 'first-epoch';
  environment.listeners.clear();
  const { auth } = await import('$lib/stores/auth');
  (auth as unknown as { set(value: unknown): void }).set({ status: 'anonymous', user: null });
  document.documentElement.removeAttribute('style');
  document.documentElement.removeAttribute('class');
  delete document.documentElement.dataset.resolvedTheme;
  delete document.documentElement.dataset.themeFamily;
  document.head.innerHTML = `<style>${tokens}</style>`;
  system = Object.assign(new EventTarget(), { matches: false });
  vi.stubGlobal('matchMedia', vi.fn(() => system));
  stops = [];
});
afterEach(() => { stops.reverse().forEach((stop) => stop()); vi.unstubAllGlobals(); });

describe('application appearance through the preference owner', () => {
  it('projects root and metadata together without replacing content, preferences or unrelated root state', async () => {
    const { projectAppearance } = await import('./appearance');
    const { userPreferences } = await import('$lib/stores/userPreferences');
    const { get } = await import('svelte/store');
    const before = structuredClone(get(userPreferences));
    const root = document.documentElement;
    root.classList.add('workspace-shell');
    root.dataset.nativeSurface = 'rail';
    const draft = document.createElement('textarea');
    draft.value = 'Unsent draft';
    document.body.appendChild(draft);
    for (const [family, mode, chrome] of [
      ['paper', 'dark', '#272620'],
      ['paper', 'light', '#eeeadd'],
      ['classic', 'light', '#edf2f5']
    ] as const) {
      projectAppearance(family, mode);
      expect(root.dataset.themeFamily).toBe(family);
      expect(root.dataset.resolvedTheme).toBe(mode);
      expect(root.classList.contains('dark')).toBe(mode === 'dark');
      expect(root.style.colorScheme).toBe(mode);
      expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe(chrome);
      expect(document.querySelector('meta[name="color-scheme"]')?.getAttribute('content')).toBe(mode);
      expect(document.querySelectorAll('meta[name="theme-color"]')).toHaveLength(1);
      expect(document.querySelectorAll('meta[name="color-scheme"]')).toHaveLength(1);
    }
    expect(root.classList.contains('workspace-shell')).toBe(true);
    expect(root.dataset.nativeSurface).toBe('rail');
    expect(draft.isConnected).toBe(true);
    expect(draft.value).toBe('Unsent draft');
    expect(get(userPreferences)).toEqual(before);
    expect(localStorage.length).toBe(0);
    draft.remove();
    delete root.dataset.nativeSurface;
  });

  async function start() {
    const { startAppearance } = await import('./appearance');
    const preferences = await import('$lib/stores/userPreferences');
    const { api } = await import('$lib/api/client');
    const { auth } = await import('$lib/stores/auth');
    vi.mocked(api.userPreferences.get).mockResolvedValue(prefs('dark'));
    expect(getComputedStyle(document.documentElement).getPropertyValue('--app-chrome-bg')).toBe('#edf2f5');
    stops.push(startAppearance());
    stops.push(preferences.startUserPreferences());
    return { ...preferences, api, login: (email: string) =>
      (auth as unknown as { set(value: unknown): void }).set({ status: 'authenticated', user: { email } }) };
  }

  it('starts with the OS, applies verified account cache and follows explicit/system changes without replacing content', async () => {
    const marker = document.createElement('input');
    document.body.appendChild(marker);
    const store = await start();
    expect(resolved()).toBe('light');
    localStorage.setItem(`cognis_user_preferences:account-v2:${encodeURIComponent(environment.owner!)}`,
      JSON.stringify(prefs('dark')));
    store.login('first@cognis.test');
    expect(resolved()).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#07111a');
    await vi.waitFor(() => expect(document.querySelector('meta[name="color-scheme"]')?.getAttribute('content')).toBe('dark'));
    changeSystem(false);
    expect(resolved()).toBe('dark');
    vi.mocked(store.api.userPreferences.update).mockResolvedValue(prefs('system'));
    await store.saveUserPreferences(prefs('system'));
    expect(resolved()).toBe('light');
    changeSystem(true);
    expect(resolved()).toBe('dark');
    expect(marker.isConnected).toBe(true);
    marker.remove();
  });

  it('resets on revocation and binds a different account without replaying the prior preference', async () => {
    const store = await start();
    store.login('first@cognis.test');
    await vi.waitFor(() => expect(resolved()).toBe('dark'));
    environment.owner = null;
    environment.epoch = 'revoked-epoch';
    environment.listeners.forEach((listener) => listener());
    expect(resolved()).toBe('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#edf2f5');
    environment.owner = 'https://controller.test\nsecond@cognis.test';
    environment.epoch = 'second-epoch';
    vi.mocked(store.api.userPreferences.get).mockResolvedValue(prefs('system'));
    store.login('second@cognis.test');
    expect(resolved()).toBe('light');
    changeSystem(true);
    expect(resolved()).toBe('dark');
  });

  it('projects optimistic saves and their rollback, then removes system/store listeners', async () => {
    const store = await start();
    store.login('first@cognis.test');
    await vi.waitFor(() => expect(resolved()).toBe('dark'));
    let reject!: (error: Error) => void;
    vi.mocked(store.api.userPreferences.update).mockImplementation(() => new Promise((_resolve, fail) => { reject = fail; }));
    const save = store.saveUserPreferences(prefs('light', 'paper'));
    expect(resolved()).toBe('light');
    expect(document.documentElement.dataset.themeFamily).toBe('paper');
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#eeeadd');
    await vi.waitFor(() => expect(reject).toBeDefined());
    reject(new Error('Offline'));
    await expect(save).rejects.toThrow('Offline');
    expect(resolved()).toBe('dark');
    expect(document.documentElement.dataset.themeFamily).toBe('classic');
    stops.reverse().forEach((stop) => stop());
    stops = [];
    const before = resolved();
    changeSystem(true);
    expect(resolved()).toBe(before);
  });

  it('projects families independently of System mode and resets on account revocation', async () => {
    const store = await start();
    vi.mocked(store.api.userPreferences.get).mockResolvedValue(prefs('system', 'paper'));
    store.login('first@cognis.test');
    await vi.waitFor(() => expect(document.documentElement.dataset.themeFamily).toBe('paper'));
    for (const { id } of THEME_FAMILIES) {
      const next = prefs('system', id);
      vi.mocked(store.api.userPreferences.update).mockResolvedValueOnce(next);
      await store.saveUserPreferences(next);
      expect(document.documentElement.dataset.themeFamily).toBe(id);
      changeSystem(true);
      expect(resolved()).toBe('dark');
      expect(document.documentElement.dataset.themeFamily).toBe(id);
      expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content'))
        .toBe(id === 'paper' ? '#272620' : '#07111a');
      changeSystem(false);
      expect(resolved()).toBe('light');
    }
    environment.owner = null; environment.epoch = 'revoked';
    environment.listeners.forEach((listener) => listener());
    expect(document.documentElement.dataset.themeFamily).toBe('classic');
  });

  it.each([false, true])('resolves startup from the OS (dark=%s), ignoring any global preference cache', (dark) => {
    system.matches = dark;
    document.documentElement.dataset.themeFamily = 'paper';
    localStorage.setItem('cognis_theme', dark ? 'light' : 'dark');
    const script = html.match(/<script>([\s\S]*?)<\/script>/)![1];
    document.head.insertAdjacentHTML('beforeend', '<meta name="theme-color" content="#EDF2F5"><meta name="color-scheme" content="light dark">');
    new Function(script)();
    expect(resolved()).toBe(dark ? 'dark' : 'light');
    expect(document.documentElement.dataset.themeFamily).toBe('classic');
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content'))
      .toBe(dark ? '#07111A' : '#E9EEF4');
  });
});

describe('device startup appearance hint', () => {
  const palette = (family: string, chrome: string, bg: string) =>
    ({ family, chrome, bg, surface: '48 48 46', text: '250 249 245', border: '119 117 107', accent: '235 170 144', onAccent: '41 33 29' });
  const hint = (theme: string, light = palette('paper', '#d8d6d0', '216 214 208'), dark = palette('paper', '#1f1e1d', '31 30 29')) =>
    ({ v: 1, theme, light, dark });
  function boot() {
    const script = html.match(/<script>([\s\S]*?)<\/script>/)![1];
    document.head.insertAdjacentHTML('beforeend', '<meta name="theme-color" content="#EDF2F5"><meta name="color-scheme" content="light dark">');
    new Function(script)();
  }

  it.each([
    ['dark', false, 'dark', '#1f1e1d', '31 30 29'],
    ['light', true, 'light', '#d8d6d0', '216 214 208'],
    ['system', true, 'dark', '#1f1e1d', '31 30 29'],
    ['system', false, 'light', '#d8d6d0', '216 214 208']
  ])('boot paints the stored %s appearance (OS dark=%s)', (theme, osDark, mode, chrome, bg) => {
    system.matches = osDark;
    localStorage.setItem('cognis-startup-appearance', JSON.stringify(hint(theme)));
    boot();
    expect(resolved()).toBe(mode);
    expect(document.documentElement.dataset.themeFamily).toBe('paper');
    expect(document.documentElement.classList.contains('dark')).toBe(mode === 'dark');
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe(chrome);
    expect(document.getElementById('cognis-startup-palette')?.textContent).toContain(`--startup-bg:rgb(${bg})`);
  });

  it.each([
    ['unknown family', hint('dark', palette('paper', '#d8d6d0', '1 2 3'), palette('neon', '#1f1e1d', '1 2 3'))],
    ['CSS injection', hint('dark', palette('paper', '#d8d6d0', '1 2 3'), palette('paper', '#1f1e1d', '1 2 3);}body{color:red'))],
    ['bad chrome', hint('dark', palette('paper', '#d8d6d0', '1 2 3'), palette('paper', 'red', '1 2 3'))],
    ['wrong version', { ...hint('dark'), v: 2 }],
    ['malformed JSON', '{']
  ])('boot ignores an invalid hint (%s) and keeps Classic with the OS', (_name, value) => {
    system.matches = true;
    localStorage.setItem('cognis-startup-appearance', typeof value === 'string' ? value : JSON.stringify(value));
    boot();
    expect(resolved()).toBe('dark');
    expect(document.documentElement.dataset.themeFamily).toBe('classic');
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe('#07111A');
    expect(document.getElementById('cognis-startup-palette')).toBeNull();
  });

  it('keeps only the cosmetic hint during expiry and forgets it on explicit logout', async () => {
    document.head.insertAdjacentHTML('beforeend', `<style>
      [data-theme-family="gruvbox"][data-resolved-theme] { --color-chrome: 27 27 27; --color-surface: 50 48 47; --color-foreground: 212 190 152;
        --color-control-border: 168 153 132; --color-accent: 169 182 101; --color-on-accent: 40 40 40; }
      [data-theme-family="gruvbox"][data-resolved-theme="light"] { --app-chrome-bg: #f2e5bc; }
      [data-theme-family="gruvbox"][data-resolved-theme="dark"] { --app-chrome-bg: #1b1b1b; }
    </style>`);
    localStorage.setItem('cognis-startup-appearance', JSON.stringify(hint('dark')));
    const { startAppearance } = await import('./appearance');
    const preferences = await import('$lib/stores/userPreferences');
    const { api } = await import('$lib/api/client');
    const { auth } = await import('$lib/stores/auth');
    vi.mocked(api.userPreferences.get).mockResolvedValue(prefs('light', 'gruvbox'));
    stops.push(startAppearance());
    stops.push(preferences.startUserPreferences());
    // No account yet: the hint stays painted instead of the Classic default.
    expect(document.documentElement.dataset.themeFamily).toBe('paper');
    expect(resolved()).toBe('dark');
    (auth as unknown as { set(value: unknown): void }).set({ status: 'authenticated', user: { email: 'first@cognis.test' } });
    await vi.waitFor(() => expect(document.documentElement.dataset.themeFamily).toBe('gruvbox'));
    expect(resolved()).toBe('light');
    const { readStartupAppearance } = await import('./startup-appearance');
    expect(readStartupAppearance()).toMatchObject({ theme: 'light', light: { family: 'gruvbox', chrome: '#f2e5bc', bg: '27 27 27' } });
    environment.owner = null;
    environment.epoch = 'revoked';
    environment.listeners.forEach((listener) => listener());
    expect(document.documentElement.dataset.themeFamily).toBe('gruvbox');
    expect(resolved()).toBe('light');
    const { get } = await import('svelte/store');
    expect(get(preferences.userPreferences)).toEqual(DEFAULT_USER_PREFERENCES);
    const { forgetStartupAppearance } = await import('./startup-appearance');
    forgetStartupAppearance();
    window.dispatchEvent(new StorageEvent('storage', { key: 'cognis-startup-appearance', storageArea: localStorage }));
    expect(document.documentElement.dataset.themeFamily).toBe('classic');
  });

  it('does not replace a saved hint with defaults while verified account preferences are loading', async () => {
    localStorage.setItem('cognis-startup-appearance', JSON.stringify(hint('dark')));
    const { startAppearance } = await import('./appearance');
    const preferences = await import('$lib/stores/userPreferences');
    const { api } = await import('$lib/api/client');
    const { auth } = await import('$lib/stores/auth');
    let finish!: (value: UserPreferences) => void;
    vi.mocked(api.userPreferences.get).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    stops.push(startAppearance(), preferences.startUserPreferences());
    (auth as unknown as { set(value: unknown): void }).set({
      status: 'authenticated', user: { email: 'first@cognis.test' },
    });
    await vi.waitFor(() => expect(finish).toBeDefined());
    expect(document.documentElement.dataset.themeFamily).toBe('paper');
    expect(resolved()).toBe('dark');
    expect(JSON.parse(localStorage.getItem('cognis-startup-appearance')!)).toEqual(hint('dark'));
    finish(prefs('light', 'classic'));
    await vi.waitFor(() => expect(document.documentElement.dataset.themeFamily).toBe('classic'));
    expect(resolved()).toBe('light');
  });
});
import { readFileSync } from 'node:fs';
