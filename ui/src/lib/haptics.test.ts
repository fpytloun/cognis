import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { haptic } from './haptics';

describe('haptic', () => {
  const original = navigator.vibrate;

  beforeEach(() => {
    // jsdom doesn't implement vibrate; provide a mock we can inspect.
    Object.defineProperty(navigator, 'vibrate', {
      value: vi.fn(() => true),
      configurable: true,
      writable: true
    });
  });

  afterEach(() => {
    Object.defineProperty(navigator, 'vibrate', {
      value: original,
      configurable: true,
      writable: true
    });
  });

  it('light() invokes vibrate with a short single pulse', () => {
    haptic.light();
    expect(navigator.vibrate).toHaveBeenCalledWith(8);
  });

  it('medium() invokes a longer pulse than light', () => {
    haptic.medium();
    expect(navigator.vibrate).toHaveBeenCalledWith(14);
  });

  it('heavy() invokes the longest single pulse', () => {
    haptic.heavy();
    expect(navigator.vibrate).toHaveBeenCalledWith(22);
  });

  it('success() fires a multi-pulse pattern', () => {
    haptic.success();
    const arg = (navigator.vibrate as unknown as { mock: { calls: unknown[][] } }).mock.calls[0][0];
    expect(Array.isArray(arg)).toBe(true);
    expect((arg as number[]).length).toBe(3);
  });

  it('is a no-op when vibrate is not available', () => {
    Object.defineProperty(navigator, 'vibrate', {
      value: undefined,
      configurable: true,
      writable: true
    });
    expect(() => haptic.light()).not.toThrow();
  });

  it('is a no-op when vibrate throws', () => {
    Object.defineProperty(navigator, 'vibrate', {
      value: () => {
        throw new Error('blocked');
      },
      configurable: true,
      writable: true
    });
    expect(() => haptic.error()).not.toThrow();
  });
});
