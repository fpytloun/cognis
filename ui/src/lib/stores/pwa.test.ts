import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { canAttemptPwaAuxiliaryWindow, installPromptAvailable, isIosStandalonePwa, registerServiceWorker, updateAvailable } from './pwa';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('pwa stores', () => {
  it('installPromptAvailable defaults to false', () => {
    expect(get(installPromptAvailable)).toBe(false);
  });

  it('updateAvailable defaults to false', () => {
    expect(get(updateAvailable)).toBe(false);
  });

  it('allows auxiliary windows outside iOS standalone PWAs', () => {
    Object.defineProperty(globalThis.navigator, 'userAgent', {
      configurable: true,
      value: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/120 Safari/537.36',
    });
    Object.defineProperty(globalThis.navigator, 'standalone', {
      configurable: true,
      value: true,
    });

    expect(isIosStandalonePwa()).toBe(false);
    expect(canAttemptPwaAuxiliaryWindow()).toBe(true);
  });

  it('blocks auxiliary windows in iOS standalone PWAs', () => {
    Object.defineProperty(globalThis.navigator, 'userAgent', {
      configurable: true,
      value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1',
    });
    Object.defineProperty(globalThis.navigator, 'standalone', {
      configurable: true,
      value: true,
    });

    expect(isIosStandalonePwa()).toBe(true);
    expect(canAttemptPwaAuxiliaryWindow()).toBe(false);
  });

  it('blocks auxiliary windows in iPadOS standalone PWAs with desktop user agents', () => {
    Object.defineProperty(globalThis.navigator, 'userAgent', {
      configurable: true,
      value: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15',
    });
    Object.defineProperty(globalThis.navigator, 'platform', {
      configurable: true,
      value: 'MacIntel',
    });
    Object.defineProperty(globalThis.navigator, 'maxTouchPoints', {
      configurable: true,
      value: 5,
    });
    Object.defineProperty(globalThis.navigator, 'standalone', {
      configurable: true,
      value: true,
    });

    expect(isIosStandalonePwa()).toBe(true);
    expect(canAttemptPwaAuxiliaryWindow()).toBe(false);
  });

  it('registerServiceWorker does not hard-reset when a waiting worker exists', async () => {
    const unregister = vi.fn().mockResolvedValue(true);
    const getRegistrations = vi.fn().mockResolvedValue([{ unregister }]);
    const register = vi.fn().mockResolvedValue({ waiting: {}, addEventListener: vi.fn() });

    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: {},
        getRegistrations,
        register,
      },
    });

    await registerServiceWorker();

    expect(register).toHaveBeenCalledWith('/service-worker.js', {
      type: 'module',
      scope: '/',
    });
    expect(getRegistrations).not.toHaveBeenCalled();
    expect(unregister).not.toHaveBeenCalled();
  });
});
