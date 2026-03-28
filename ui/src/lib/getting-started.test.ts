import { beforeEach, describe, expect, it } from 'vitest';

import { deriveGettingStartedSteps, isGettingStartedDismissed, setGettingStartedDismissed } from '$lib/getting-started';
import type { SystemDiagnostics } from '$lib/types/api';

describe('getting started helpers', () => {
  const storage = new Map<string, string>();
  const diagnostics: SystemDiagnostics = {
    readiness: {
      mnemory_reachable: true,
      intaris_reachable: true,
      llm_provider_configured: false,
      agent_created: false,
      chat_ready: false
    },
    ui: {},
    database: {},
    config: {},
    providers: [],
    agents: {},
    key_fingerprint: null
  };

  beforeEach(() => {
    storage.clear();
    Object.defineProperty(window, 'localStorage', {
      value: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key)
      },
      configurable: true
    });
  });

  it('derives pending steps from diagnostics readiness', () => {
    const steps = deriveGettingStartedSteps(diagnostics);
    expect(steps[0]?.done).toBe(true);
    expect(steps[1]?.done).toBe(false);
  });

  it('stores dismissed state in localStorage', () => {
    expect(isGettingStartedDismissed()).toBe(false);
    setGettingStartedDismissed(true);
    expect(isGettingStartedDismissed()).toBe(true);
    setGettingStartedDismissed(false);
    expect(isGettingStartedDismissed()).toBe(false);
  });
});
