import { readable } from 'svelte/store';

/**
 * Viewport helpers.
 *
 * - `isMobile` store: true when viewport < 1024px (our mobile/desktop pivot).
 *   Replaces one-shot `window.innerWidth` checks; updates on resize.
 * - `isTouch()` helper: true when the primary input is coarse (phone/tablet).
 * - `viewportMetrics` store: visualViewport-backed CSS variables for mobile
 *   shells. The shell is sized to the visible viewport; composers should not
 *   add a separate keyboard offset on top.
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

interface ViewportMetrics {
  height: number;
  offsetTop: number;
  keyboardOpen: boolean;
}

export interface ViewportInput {
  innerHeight: number;
  visualViewportHeight?: number;
  visualViewportOffsetTop?: number;
}

export function calculateViewportMetrics(input: ViewportInput): ViewportMetrics {
  const innerHeight = Math.max(0, input.innerHeight);
  const visualHeight = input.visualViewportHeight ?? innerHeight;
  const visualOffsetTop = input.visualViewportOffsetTop ?? 0;
  const keyboardOverlap = innerHeight - (visualOffsetTop + visualHeight);
  // The on-screen keyboard manifests as overlay mode in iOS PWA standalone:
  // window.innerHeight stays at the full layout viewport while
  // visualViewport.height shrinks by roughly the keyboard height. We do NOT
  // treat a positive visualOffsetTop alone as keyboard-open: iOS uses the
  // same mechanism to centre a focused input even with no keyboard overlap,
  // and reacting to it would push the app shell down past the fixed mobile
  // header on non-chat routes (visible "top bounces" regression on Projects,
  // Tasks, Settings when an input gains focus).
  const keyboardOpen = keyboardOverlap > 80;
  const height = keyboardOpen ? visualHeight : innerHeight;
  const offsetTop = keyboardOpen ? visualOffsetTop : 0;
  return {
    height: Math.max(0, height),
    offsetTop: Math.max(0, offsetTop),
    keyboardOpen,
  };
}

function readViewportMetrics(): ViewportMetrics {
  if (typeof window === 'undefined') {
    return { height: 0, offsetTop: 0, keyboardOpen: false };
  }
  const vv = window.visualViewport;
  // When the keyboard is closed, the shell must paint the full layout
  // viewport, including iOS rounded-corner/home-indicator areas. Only switch
  // to visualViewport sizing while the keyboard is actually reducing the
  // usable area.
  return calculateViewportMetrics({
    innerHeight: window.innerHeight,
    visualViewportHeight: vv?.height,
    visualViewportOffsetTop: vv?.offsetTop,
  });
}

function syncViewportVariables(metrics = readViewportMetrics()): void {
  if (typeof document === 'undefined' || typeof window === 'undefined') return;
  const root = document.documentElement;
  if (metrics.keyboardOpen) {
    // Pin the shell to the visible viewport above the keyboard (and any iOS
    // form-accessory bar, which visualViewport already excludes from its
    // height).
    root.style.setProperty('--app-viewport-height', `${Math.round(metrics.height)}px`);
    root.style.setProperty('--app-viewport-offset-top', `${Math.round(metrics.offsetTop)}px`);
  } else {
    // Keyboard closed: clear the inline overrides so the shell falls back to
    // the `:root` rule's `100dvh`. On iOS PWA standalone with
    // `viewport-fit=cover`, `100dvh` covers the full physical viewport
    // including the home-indicator safe area, while `window.innerHeight` can
    // underreport by ~34pt and leave a visible strip below the bottom tab bar
    // and chat composer.
    root.style.removeProperty('--app-viewport-height');
    root.style.removeProperty('--app-viewport-offset-top');
  }
  root.style.setProperty('--app-bottom-inset', '0px');
  root.dataset.keyboard = metrics.keyboardOpen ? 'open' : 'closed';
}

export const viewportMetrics = readable<ViewportMetrics>(
  { height: 0, offsetTop: 0, keyboardOpen: false },
  (set) => {
  if (typeof window === 'undefined') return;
  const vv = window.visualViewport;
  const scheduledTimers = new Set<number>();
  const update = () => {
    const metrics = readViewportMetrics();
    syncViewportVariables(metrics);
    set(metrics);
  };
  const scheduleUpdate = () => {
    update();
    window.requestAnimationFrame(update);
    const timer = window.setTimeout(() => {
      scheduledTimers.delete(timer);
      update();
    }, 250);
    scheduledTimers.add(timer);
  };

  update();
  vv?.addEventListener('resize', update);
  vv?.addEventListener('scroll', update);
  window.addEventListener('resize', update, { passive: true });
  window.addEventListener('orientationchange', scheduleUpdate, { passive: true });
  window.addEventListener('pageshow', scheduleUpdate);
  document.addEventListener('visibilitychange', scheduleUpdate);
  window.addEventListener('focusin', scheduleUpdate, true);
  window.addEventListener('focusout', scheduleUpdate, true);
  const firstTimer = window.setTimeout(() => {
    scheduledTimers.delete(firstTimer);
    update();
  }, 250);
  const secondTimer = window.setTimeout(() => {
    scheduledTimers.delete(secondTimer);
    update();
  }, 1000);
  scheduledTimers.add(firstTimer);
  scheduledTimers.add(secondTimer);
  return () => {
    for (const timer of scheduledTimers) {
      window.clearTimeout(timer);
    }
    scheduledTimers.clear();
    vv?.removeEventListener('resize', update);
    vv?.removeEventListener('scroll', update);
    window.removeEventListener('resize', update);
    window.removeEventListener('orientationchange', scheduleUpdate);
    window.removeEventListener('pageshow', scheduleUpdate);
    document.removeEventListener('visibilitychange', scheduleUpdate);
    window.removeEventListener('focusin', scheduleUpdate, true);
    window.removeEventListener('focusout', scheduleUpdate, true);
    const root = document.documentElement;
    root.style.removeProperty('--app-viewport-height');
    root.style.removeProperty('--app-viewport-offset-top');
    root.style.removeProperty('--app-bottom-inset');
    delete root.dataset.keyboard;
  };
});
