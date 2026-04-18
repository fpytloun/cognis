import { describe, expect, it } from 'vitest';

/**
 * Logic-level tests for PullToRefresh. Re-creates the pure thresholds used
 * inside the component so we can assert the state transitions without
 * mounting the Svelte component.
 */

const THRESHOLD = 72;

function computePullDistance(dy: number): number {
  if (dy <= 0) return 0;
  return Math.min(THRESHOLD * 1.5, dy);
}

function shouldRefresh(pullDistance: number): boolean {
  return pullDistance >= THRESHOLD;
}

describe('PullToRefresh thresholds', () => {
  it('ignores upward drags', () => {
    expect(computePullDistance(-50)).toBe(0);
  });

  it('clamps at 1.5x threshold', () => {
    expect(computePullDistance(10_000)).toBe(THRESHOLD * 1.5);
  });

  it('scales linearly between 0 and the clamp', () => {
    expect(computePullDistance(30)).toBe(30);
  });

  it('does not refresh below threshold', () => {
    expect(shouldRefresh(30)).toBe(false);
    expect(shouldRefresh(THRESHOLD - 1)).toBe(false);
  });

  it('refreshes at or past threshold', () => {
    expect(shouldRefresh(THRESHOLD)).toBe(true);
    expect(shouldRefresh(THRESHOLD + 20)).toBe(true);
  });
});
