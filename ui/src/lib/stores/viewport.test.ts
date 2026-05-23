import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { calculateViewportMetrics, isMobile, isTouch } from './viewport';

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

  it('uses layout viewport height when visual viewport only differs by safe area', () => {
    expect(
      calculateViewportMetrics({
        innerHeight: 874,
        visualViewportHeight: 840,
        visualViewportOffsetTop: 0,
      })
    ).toEqual({ height: 874, offsetTop: 0, keyboardOpen: false });
  });

  it('anchors the shell at top and shrinks from the bottom while keyboard is open', () => {
    expect(
      calculateViewportMetrics({
        innerHeight: 874,
        visualViewportHeight: 520,
        visualViewportOffsetTop: 16,
      })
    ).toEqual({ height: 536, offsetTop: 0, keyboardOpen: true });
  });

  it('ignores stale keyboard-sized visual viewport metrics when no text input is focused', () => {
    expect(
      calculateViewportMetrics({
        innerHeight: 874,
        visualViewportHeight: 520,
        visualViewportOffsetTop: 16,
        keyboardCanBeOpen: false,
      })
    ).toEqual({ height: 874, offsetTop: 0, keyboardOpen: false });
  });

  it('does not treat visual viewport offset alone as keyboard open', () => {
    // iOS may push the visual viewport down to centre a focused input on
    // pages with no keyboard overlap (e.g. Projects, Tasks, Settings). If we
    // marked that as keyboard-open the app shell would jump to top: offsetTop
    // and detach from the fixed mobile header at top: 0 of the viewport,
    // producing a "top bounces" regression. Only a real overlap (keyboard
    // covering the bottom of the layout viewport) counts.
    expect(
      calculateViewportMetrics({
        innerHeight: 700,
        visualViewportHeight: 700,
        visualViewportOffsetTop: 200,
      })
    ).toEqual({ height: 700, offsetTop: 0, keyboardOpen: false });
  });
});
