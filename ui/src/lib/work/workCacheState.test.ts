import { describe, expect, it } from 'vitest';
import { presentActivityLifecycle } from './activityLifecycle';

describe('presentActivityLifecycle', () => {
  it('hides the lifecycle banner when live', () => {
    expect(presentActivityLifecycle({ state: 'live' })).toEqual({
      kind: 'none', message: null, busy: false,
    });
  });

  it.each([
    ['catching_up', 'status', 'Catching up activity…', true],
    ['partial', 'warning', 'Activity is partially available.', false],
    ['failed', 'error', 'Activity refresh failed.', false],
  ] as const)('presents %s consistently', (state, kind, message, busy) => {
    expect(presentActivityLifecycle({ state })).toEqual({ kind, message, busy });
  });
});
