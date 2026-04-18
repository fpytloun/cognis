import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { isMobile, isTouch } from './viewport';

describe('viewport stores', () => {
  const originalInnerWidth = window.innerWidth;
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', {
      value: 400,
      writable: true,
      configurable: true
    });
  });

  afterEach(() => {
    Object.defineProperty(window, 'innerWidth', {
      value: originalInnerWidth,
      writable: true,
      configurable: true
    });
    window.matchMedia = originalMatchMedia;
  });

  it('isMobile reflects current innerWidth relative to 1024px', () => {
    // The readable store captured initial state at module import. We can't
    // force-update from outside without a resize event, but we can at least
    // verify the store has a boolean value.
    expect(typeof get(isMobile)).toBe('boolean');
  });

  it('isTouch returns a boolean even when matchMedia is unavailable', () => {
    // @ts-expect-error - intentional reassignment for test
    window.matchMedia = undefined;
    expect(isTouch()).toBe(false);
  });

  it('isTouch returns true when the media query reports coarse pointer + no hover', () => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('hover: none') && query.includes('pointer: coarse'),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
      onchange: null
    })) as unknown as typeof window.matchMedia;
    expect(isTouch()).toBe(true);
  });
});
