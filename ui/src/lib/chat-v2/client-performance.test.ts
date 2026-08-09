import { describe, expect, it, vi } from 'vitest';

import { createClientPerformanceTiming } from './client-performance';

describe('client performance timing', () => {
  it('uses one monotonic start for cached restore and authoritative freshness', () => {
    const emit = vi.fn();
    const values = [125, 180];
    const timing = createClientPerformanceTiming(100, () => values.shift() ?? 180, emit);

    timing.cachedRestore();
    timing.timelineFresh(true);

    expect(emit).toHaveBeenNthCalledWith(1, 'cached_restore_ms', 25);
    expect(emit).toHaveBeenNthCalledWith(2, 'timeline_fresh_ms', 80);
  });

  it('does not emit freshness for a stale load generation', () => {
    const emit = vi.fn();
    const timing = createClientPerformanceTiming(100, () => 200, emit);

    timing.timelineFresh(false);

    expect(emit).not.toHaveBeenCalled();
  });
});
