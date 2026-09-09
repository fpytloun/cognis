import { describe, expect, it } from 'vitest';

import { computeVirtualWindow, scrollOffsetForIndex, shouldVirtualize } from './virtualList';

describe('shouldVirtualize', () => {
  it('does not virtualize small trees', () => {
    expect(shouldVirtualize(299)).toBe(false);
    expect(shouldVirtualize(300)).toBe(false);
  });

  it('virtualizes once the threshold is exceeded', () => {
    expect(shouldVirtualize(301)).toBe(true);
    expect(shouldVirtualize(50_000)).toBe(true);
  });
});

describe('computeVirtualWindow', () => {
  const ITEM_HEIGHT = 36;
  const ITEM_COUNT = 50_000;
  const VIEWPORT = 600;

  it('windows the start of a 50,000-row list', () => {
    const window = computeVirtualWindow({
      itemCount: ITEM_COUNT,
      itemHeight: ITEM_HEIGHT,
      scrollTop: 0,
      viewportHeight: VIEWPORT,
    });
    expect(window.startIndex).toBe(0);
    expect(window.paddingTop).toBe(0);
    expect(window.endIndex).toBeGreaterThan(0);
    expect(window.endIndex).toBeLessThan(ITEM_COUNT);
    expect(window.paddingBottom).toBeGreaterThan(0);
  });

  it('windows the middle of a 50,000-row list with overscan', () => {
    const scrollTop = 20_000 * ITEM_HEIGHT;
    const window = computeVirtualWindow({
      itemCount: ITEM_COUNT,
      itemHeight: ITEM_HEIGHT,
      scrollTop,
      viewportHeight: VIEWPORT,
      overscan: 8,
    });
    expect(window.startIndex).toBe(20_000 - 8);
    expect(window.paddingTop).toBe(window.startIndex * ITEM_HEIGHT);
    expect(window.endIndex).toBeGreaterThan(20_000);
    expect(window.paddingBottom).toBe((ITEM_COUNT - window.endIndex) * ITEM_HEIGHT);
  });

  it('windows the boundary at the end of the list without overrunning', () => {
    const scrollTop = (ITEM_COUNT - 5) * ITEM_HEIGHT;
    const window = computeVirtualWindow({
      itemCount: ITEM_COUNT,
      itemHeight: ITEM_HEIGHT,
      scrollTop,
      viewportHeight: VIEWPORT,
    });
    expect(window.endIndex).toBe(ITEM_COUNT);
    expect(window.paddingBottom).toBe(0);
  });

  it('handles an empty list', () => {
    expect(computeVirtualWindow({
      itemCount: 0,
      itemHeight: ITEM_HEIGHT,
      scrollTop: 0,
      viewportHeight: VIEWPORT,
    })).toEqual({ startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 });
  });

  it('clamps a negative scrollTop to zero', () => {
    const window = computeVirtualWindow({
      itemCount: ITEM_COUNT,
      itemHeight: ITEM_HEIGHT,
      scrollTop: -500,
      viewportHeight: VIEWPORT,
    });
    expect(window.startIndex).toBe(0);
  });
});

describe('scrollOffsetForIndex', () => {
  const ITEM_HEIGHT = 36;
  const VIEWPORT = 600;

  it('returns null when the index is already visible', () => {
    expect(scrollOffsetForIndex({
      index: 5,
      itemHeight: ITEM_HEIGHT,
      scrollTop: 0,
      viewportHeight: VIEWPORT,
    })).toBeNull();
  });

  it('scrolls down to reveal an index below the viewport', () => {
    const offset = scrollOffsetForIndex({
      index: 100,
      itemHeight: ITEM_HEIGHT,
      scrollTop: 0,
      viewportHeight: VIEWPORT,
    });
    expect(offset).toBe((100 + 1) * ITEM_HEIGHT - VIEWPORT);
  });

  it('scrolls up to reveal an index above the viewport', () => {
    const offset = scrollOffsetForIndex({
      index: 5,
      itemHeight: ITEM_HEIGHT,
      scrollTop: 1000,
      viewportHeight: VIEWPORT,
    });
    expect(offset).toBe(5 * ITEM_HEIGHT);
  });
});
