import { readable } from 'svelte/store';

/**
 * Viewport helpers.
 *
 * - `isMobile` store: true when viewport < 1024px (our mobile/desktop pivot).
 *   Replaces one-shot `window.innerWidth` checks; updates on resize.
 * - `isTouch()` helper: true when the primary input is coarse (phone/tablet).
 * - `keyboardOffset` store: visualViewport-based offset in pixels for iOS keyboard.
 */

const MOBILE_BREAKPOINT = 1024;

function readIsMobile(): boolean {
  if (typeof window === 'undefined') return false;
  return window.innerWidth < MOBILE_BREAKPOINT;
}

export const isMobile = readable(readIsMobile(), (set) => {
  if (typeof window === 'undefined') return;
  const update = () => set(readIsMobile());
  window.addEventListener('resize', update, { passive: true });
  return () => window.removeEventListener('resize', update);
});

export function isTouch(): boolean {
  if (typeof window === 'undefined') return false;
  return window.matchMedia?.('(hover: none) and (pointer: coarse)').matches ?? false;
}

function readKeyboardOffset(): number {
  if (typeof window === 'undefined' || !window.visualViewport) return 0;
  const vv = window.visualViewport;
  // When the soft keyboard is up, visualViewport.height shrinks.
  return Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
}

export const keyboardOffset = readable(0, (set) => {
  if (typeof window === 'undefined' || !window.visualViewport) return;
  const vv = window.visualViewport;
  const update = () => {
    const offset = readKeyboardOffset();
    set(offset);
    // Expose as CSS variable so fixed-position elements (composer, bottom tabs)
    // can offset themselves without JS.
    document.documentElement.style.setProperty('--kb-offset', `${offset}px`);
  };
  update();
  vv.addEventListener('resize', update);
  vv.addEventListener('scroll', update);
  return () => {
    vv.removeEventListener('resize', update);
    vv.removeEventListener('scroll', update);
    document.documentElement.style.setProperty('--kb-offset', '0px');
  };
});
