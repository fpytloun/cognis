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

function readViewportMetrics(): ViewportMetrics {
  if (typeof window === 'undefined') {
    return { height: 0, offsetTop: 0, keyboardOpen: false };
  }
  const vv = window.visualViewport;
  // Use the visible *height* of the viewport (not offsetTop + height) so the
  // shell is sized to the area above the keyboard. `offsetTop` is published
  // separately so the shell can shift down when iOS moves the visual
  // viewport to keep a focused input visible.
  const height = vv ? vv.height : window.innerHeight;
  const offsetTop = vv ? vv.offsetTop : 0;
  const keyboardOverlap = vv ? window.innerHeight - (offsetTop + height) : 0;
  return {
    height: Math.max(0, height),
    offsetTop: Math.max(0, offsetTop),
    keyboardOpen: keyboardOverlap > 80,
  };
}

function syncViewportVariables(metrics = readViewportMetrics()): void {
  if (typeof document === 'undefined' || typeof window === 'undefined') return;
  const height = metrics.height || window.innerHeight;
  const root = document.documentElement;
  root.style.setProperty('--app-viewport-height', `${Math.round(height)}px`);
  root.style.setProperty('--app-viewport-offset-top', `${Math.round(metrics.offsetTop)}px`);
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
    root.style.setProperty('--app-viewport-height', '100dvh');
    root.style.setProperty('--app-viewport-offset-top', '0px');
    root.style.setProperty('--app-bottom-inset', '0px');
    delete root.dataset.keyboard;
  };
});
