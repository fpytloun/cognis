import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  applyUpdate,
  canAttemptPwaAuxiliaryWindow,
  dismissUpdateBanner,
  handleServiceWorkerClientMessage,
  installPromptAvailable,
  isIosStandalonePwa,
  registerServiceWorker,
  updateAvailable,
} from './pwa';

function workerWithVersion(version: string): ServiceWorker {
  return {
    postMessage: vi.fn((message: { type?: string }, transfer?: Transferable[]) => {
      if (message.type !== 'GET_VERSION') return;
      const port = transfer?.[0] as MessagePort | undefined;
      port?.start?.();
      port?.postMessage({ type: 'VERSION', version });
    }),
  } as unknown as ServiceWorker;
}

function installSynchronousMessageChannelMock(): void {
  class TestMessagePort {
    peer: TestMessagePort | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;

    postMessage(data: unknown): void {
      this.peer?.onmessage?.({ data } as MessageEvent);
    }

    start(): void {}

    close(): void {}
  }

  class TestMessageChannel {
    port1 = new TestMessagePort();
    port2 = new TestMessagePort();

    constructor() {
      this.port1.peer = this.port2;
      this.port2.peer = this.port1;
    }
  }

  vi.stubGlobal('MessageChannel', TestMessageChannel);
}

async function flushMicrotasks(count = 5): Promise<void> {
  for (let index = 0; index < count; index += 1) {
    await Promise.resolve();
  }
}


afterEach(() => {
  dismissUpdateBanner();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
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

  it('does not show an update banner for first-install waiting workers', async () => {
    const unregister = vi.fn().mockResolvedValue(true);
    const getRegistrations = vi.fn().mockResolvedValue([{ unregister }]);
    const register = vi.fn().mockResolvedValue({
      waiting: {},
      installing: null,
      addEventListener: vi.fn(),
      update: vi.fn().mockResolvedValue(undefined),
    });

    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: null,
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
    expect(get(updateAvailable)).toBe(false);
  });

  it('shows an update banner when a controlled page has a waiting worker', async () => {
    const register = vi.fn().mockResolvedValue({
      waiting: {},
      installing: null,
      addEventListener: vi.fn(),
      update: vi.fn().mockResolvedValue(undefined),
    });

    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: {},
        register,
      },
    });

    await registerServiceWorker();

    expect(get(updateAvailable)).toBe(true);
  });

  it('shows an update banner when an installing worker becomes installed on a controlled page', async () => {
    let stateChange: () => void = () => {
      throw new Error('statechange listener was not registered');
    };
    const installing = {
      state: 'installing',
      addEventListener: vi.fn((_event: string, handler: () => void) => {
        stateChange = handler;
      }),
    };
    const registration = {
      waiting: null as typeof installing | null,
      installing,
      addEventListener: vi.fn(),
      update: vi.fn().mockResolvedValue(undefined),
    };
    const register = vi.fn().mockResolvedValue(registration);

    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: {},
        register,
      },
    });

    await registerServiceWorker();
    expect(get(updateAvailable)).toBe(false);

    installing.state = 'installed';
    registration.waiting = installing;
    stateChange();

    expect(get(updateAvailable)).toBe(true);
  });

  it('does not show an update banner for an installed worker unless it is registration.waiting', async () => {
    let stateChange: () => void = () => {
      throw new Error('statechange listener was not registered');
    };
    const installing = {
      state: 'installing',
      addEventListener: vi.fn((_event: string, handler: () => void) => {
        stateChange = handler;
      }),
    };
    const registration = {
      waiting: null as typeof installing | null,
      installing,
      addEventListener: vi.fn(),
      update: vi.fn().mockResolvedValue(undefined),
    };
    const register = vi.fn().mockResolvedValue(registration);

    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: {},
        register,
      },
    });

    await registerServiceWorker();
    expect(get(updateAvailable)).toBe(false);

    installing.state = 'installed';
    stateChange();

    expect(get(updateAvailable)).toBe(false);
  });

  it('clears the update banner when the observed waiting worker is no longer waiting', async () => {
    let stateChange: () => void = () => {
      throw new Error('statechange listener was not registered');
    };
    const installing = {
      state: 'installing',
      addEventListener: vi.fn((_event: string, handler: () => void) => {
        stateChange = handler;
      }),
    };
    const registration = {
      waiting: null as typeof installing | null,
      installing,
      addEventListener: vi.fn(),
      update: vi.fn().mockResolvedValue(undefined),
    };
    const register = vi.fn().mockResolvedValue(registration);

    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: {},
        register,
      },
    });

    await registerServiceWorker();

    installing.state = 'installed';
    registration.waiting = installing;
    stateChange();
    expect(get(updateAvailable)).toBe(true);

    installing.state = 'activating';
    registration.waiting = null;
    stateChange();

    expect(get(updateAvailable)).toBe(false);
  });

  it('activates the current waiting worker before reloading for an update', async () => {
    installSynchronousMessageChannelMock();
    const controllerChangeHandlers: Array<() => void> = [];
    const waiting = workerWithVersion('v2');
    const active = workerWithVersion('v2');
    const registration = {
      waiting,
      installing: null,
      addEventListener: vi.fn(),
      update: vi.fn().mockResolvedValue(undefined),
      unregister: vi.fn().mockResolvedValue(true),
    };
    const register = vi.fn().mockResolvedValue(registration);
    const getRegistration = vi.fn().mockResolvedValue(registration);
    const addEventListener = vi.fn((event: string, handler: () => void) => {
      if (event === 'controllerchange') {
        controllerChangeHandlers.push(handler);
      }
    });

    const serviceWorker = {
      controller: workerWithVersion('v1'),
      register,
      getRegistration,
      addEventListener,
      removeEventListener: vi.fn(),
    };

    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: serviceWorker,
    });

    await registerServiceWorker();
    expect(get(updateAvailable)).toBe(true);

    const reload = vi.fn();
    const updatePromise = applyUpdate(reload);

    await flushMicrotasks();
    expect(getRegistration).toHaveBeenCalled();
    expect(waiting.postMessage).toHaveBeenCalledWith({ type: 'GET_VERSION' }, expect.any(Array));
    expect(waiting.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' });
    expect(reload).not.toHaveBeenCalled();

    serviceWorker.controller = active;
    for (const handler of controllerChangeHandlers) {
      handler();
    }
    await updatePromise;

    expect(reload).toHaveBeenCalledOnce();
    expect(registration.unregister).not.toHaveBeenCalled();
    expect(get(updateAvailable)).toBe(false);
  });

  it('resets a stuck waiting worker and cognis caches when activation times out', async () => {
    vi.useFakeTimers();
    installSynchronousMessageChannelMock();
    const waiting = workerWithVersion('v2');
    const registration = {
      waiting,
      installing: null,
      addEventListener: vi.fn(),
      update: vi.fn().mockResolvedValue(undefined),
      unregister: vi.fn().mockResolvedValue(true),
    };
    const register = vi.fn().mockResolvedValue(registration);
    const getRegistration = vi.fn().mockResolvedValue(registration);
    const cacheKeys = vi.fn().mockResolvedValue(['cognis-precache-old', 'other-cache', 'cognis-runtime-old']);
    const cacheDelete = vi.fn().mockResolvedValue(true);

    Object.defineProperty(window, 'caches', {
      configurable: true,
      value: {
        keys: cacheKeys,
        delete: cacheDelete,
      },
    });
    Object.defineProperty(globalThis.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        controller: workerWithVersion('v1'),
        register,
        getRegistration,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      },
    });

    await registerServiceWorker();
    expect(get(updateAvailable)).toBe(true);

    const reload = vi.fn();
    const updatePromise = applyUpdate(reload);

    await flushMicrotasks();
    expect(waiting.postMessage).toHaveBeenCalledWith({ type: 'SKIP_WAITING' });

    await vi.advanceTimersByTimeAsync(5000);
    await updatePromise;

    expect(registration.unregister).toHaveBeenCalledOnce();
    expect(cacheKeys).toHaveBeenCalledOnce();
    expect(cacheDelete).toHaveBeenCalledWith('cognis-precache-old');
    expect(cacheDelete).toHaveBeenCalledWith('cognis-runtime-old');
    expect(cacheDelete).not.toHaveBeenCalledWith('other-cache');
    expect(reload).toHaveBeenCalledOnce();
    expect(get(updateAvailable)).toBe(false);
  });

  it('reloads at most once when an activated service worker announces an update', () => {
    updateAvailable.set(true);
    const reload = vi.fn();

    expect(handleServiceWorkerClientMessage({ type: 'COGNIS_SW_UPDATED', version: 'v2' }, reload)).toBe(true);
    expect(handleServiceWorkerClientMessage({ type: 'COGNIS_SW_UPDATED', version: 'v2' }, reload)).toBe(true);
    expect(handleServiceWorkerClientMessage({ type: 'OTHER' }, reload)).toBe(false);

    expect(reload).toHaveBeenCalledOnce();
    expect(get(updateAvailable)).toBe(false);
  });
});
