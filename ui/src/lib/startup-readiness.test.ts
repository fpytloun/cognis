import { cleanup, render, waitFor } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { createRawSnippet } from 'svelte';
import Layout from '../routes/+layout.svelte';
import { auth } from './stores/auth';
import * as runtime from './native/runtime';
import * as preferences from './stores/userPreferences';
import * as appearance from './theme/appearance';
import * as pwa from './stores/pwa';

const host = vi.hoisted(() => ({ native: false }));
vi.mock('$app/stores', () => ({
  page: { subscribe(run: (value: { url: URL }) => void) {
    run({ url: new URL('https://public-feedback.test/feedback') }); return () => {};
  } },
}));
vi.mock('$app/navigation', () => ({ goto: vi.fn(), afterNavigate: vi.fn(), beforeNavigate: vi.fn() }));
vi.mock('$lib/native/host', async importOriginal => ({
  ...await importOriginal<typeof import('./native/host')>(),
  isNativeHost: () => host.native,
}));
afterEach(() => {
  cleanup(); vi.restoreAllMocks();
  delete (window as Window & { __cognisStartupRecoverySuccess?: () => void }).__cognisStartupRecoverySuccess;
});

it.each([false, true])('signals hydrated public startup without starting private runtime (native=%s)', async native => {
  host.native = native;
  const ready = vi.fn();
  (window as Window & { __cognisStartupRecoverySuccess?: () => void }).__cognisStartupRecoverySuccess = ready;
  const bootstrap = vi.spyOn(auth, 'bootstrap');
  const nativeRuntime = vi.spyOn(runtime, 'installNativeRuntime');
  const userPreferences = vi.spyOn(preferences, 'startUserPreferences');
  const theme = vi.spyOn(appearance, 'startAppearance');
  const serviceWorker = vi.spyOn(pwa, 'registerServiceWorker');
  render(Layout, { children: createRawSnippet(() => ({ render: () => '<main>Public surface</main>' })) });
  await waitFor(() => expect(ready).toHaveBeenCalledOnce());
  expect(bootstrap).not.toHaveBeenCalled();
  expect(nativeRuntime).not.toHaveBeenCalled();
  expect(userPreferences).not.toHaveBeenCalled();
  expect(theme).not.toHaveBeenCalled();
  expect(serviceWorker).not.toHaveBeenCalled();
});
