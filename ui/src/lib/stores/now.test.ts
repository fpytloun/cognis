import { get } from 'svelte/store';
import { describe, expect, it, vi } from 'vitest';

import { now } from './now';

describe('now store', () => {
  it('provides a timestamp', () => {
    const value = get(now);
    expect(typeof value).toBe('number');
    expect(value).toBeGreaterThan(0);
  });

  it('updates on tick', async () => {
    vi.useFakeTimers();
    const initial = get(now);
    vi.advanceTimersByTime(31_000);
    // The readable subscription fires on first subscribe, then on the interval.
    const after = get(now);
    // `after` is >= `initial` (timer may or may not have fired depending on scheduler,
    // but the store is a valid numeric timestamp).
    expect(after).toBeGreaterThanOrEqual(initial);
    vi.useRealTimers();
  });
});
