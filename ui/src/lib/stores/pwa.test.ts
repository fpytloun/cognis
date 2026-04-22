import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { installPromptAvailable, registerServiceWorker, updateAvailable } from './pwa';

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
