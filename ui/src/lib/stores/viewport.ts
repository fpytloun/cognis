import { readable } from 'svelte/store';
import { isIosStandalonePwa } from '$lib/stores/pwa';

/**
 * Viewport helpers.
 *
 * - `isMobile` store: true when viewport < 1024px (our mobile/desktop pivot).
 *   Replaces one-shot `window.innerWidth` checks; updates on resize.
 * - `isTouch()` helper: true when any input is coarse (phone/tablet or a
 *   tablet with an attached trackpad).
 * - `viewportMetrics` store: visualViewport-backed CSS variables for mobile
 *   chat bodies. The app shell remains fixed; only keyboard-avoiding chat
 *   surfaces reserve the occluded bottom area.
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
  return window.matchMedia?.('(any-pointer: coarse)').matches ?? false;
}

export function hasConservativeTouchKeyboardFallback(): boolean {
  return typeof window !== 'undefined'
    && !window.visualViewport
    && isTouch()
    && Math.min(window.screen.width, window.screen.height) < 600;
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
  layoutViewportBaseline?: number;
  keyboardCanBeOpen?: boolean;
}

export interface ViewportCssPolicy {
  height: string | null;
  bottomControlInset: string | null;
}

export function calculateViewportMetrics(input: ViewportInput): ViewportMetrics {
  const innerHeight = Math.max(0, input.innerHeight);
  const visualHeight = input.visualViewportHeight ?? innerHeight;
  const visualOffsetTop = input.visualViewportOffsetTop ?? 0;
  const keyboardOverlap = innerHeight - (visualOffsetTop + visualHeight);
  const layoutViewportShrink = Math.max(0, (input.layoutViewportBaseline ?? innerHeight) - innerHeight);
  // The on-screen keyboard manifests as overlay mode in iOS PWA standalone:
  // window.innerHeight stays at the full layout viewport while
  // visualViewport.height shrinks by roughly the keyboard height. We do NOT
  // treat a positive visualOffsetTop alone as keyboard-open: iOS uses the
  // same mechanism to centre a focused input even with no keyboard overlap,
  // and reacting to it would push the app shell down past the fixed mobile
  // header on non-chat routes (visible "top bounces" regression on Projects,
  // Tasks, Settings when an input gains focus).
  const keyboardOpen = (keyboardOverlap > 80 || layoutViewportShrink > 80) && input.keyboardCanBeOpen !== false;
  // A positive visualViewport.offsetTop means iOS panned the visible viewport
  // over the layout viewport to reveal the focused input. Compensate only when
  // a real keyboard overlap exists; applying offset-only focus movement caused
  // the historical non-chat header bounce.
  const height = keyboardOpen ? visualOffsetTop + visualHeight : innerHeight;
  const offsetTop = keyboardOpen ? visualOffsetTop : 0;
  return {
    height: Math.max(0, height),
    offsetTop: Math.max(0, offsetTop),
    keyboardOpen,
  };
}

export function viewportCssPolicy(
  metrics: ViewportMetrics,
  iosStandalonePwa: boolean,
): ViewportCssPolicy {
  if (metrics.keyboardOpen) {
    return {
      height: `${Math.round(metrics.height)}px`,
      bottomControlInset: '0px',
    };
  }
  if (iosStandalonePwa) {
    return {
      // Do not size the closed shell with viewport units. The fixed shell uses
      // top/bottom edges so WebKit keeps controls inside the visible viewport.
      height: null,
      bottomControlInset: null,
    };
  }
  return {
    height: null,
    bottomControlInset: null,
  };
}

function hasFocusedTextInput(): boolean {
  if (typeof document === 'undefined') {
    return false;
  }
  const activeElement = document.activeElement;
  if (!(activeElement instanceof HTMLElement)) {
    return false;
  }
  const tagName = activeElement.tagName.toLowerCase();
  return tagName === 'input' || tagName === 'textarea' || activeElement.isContentEditable;
}

function readViewportMetrics(layoutViewportBaseline?: number): ViewportMetrics {
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
    layoutViewportBaseline,
    keyboardCanBeOpen: hasFocusedTextInput(),
  });
}

function syncViewportVariables(metrics = readViewportMetrics()): void {
  if (typeof document === 'undefined' || typeof window === 'undefined') return;
  const root = document.documentElement;
  const standalonePwa = isIosStandalonePwa();
  const policy = viewportCssPolicy(metrics, standalonePwa);
  root.dataset.standalonePwa = standalonePwa ? 'true' : 'false';
  if (policy.height === null) {
    root.style.removeProperty('--app-viewport-height');
  } else {
    root.style.setProperty('--app-viewport-height', policy.height);
  }
  if (policy.bottomControlInset === null) {
    root.style.removeProperty('--app-bottom-control-inset');
  } else {
    root.style.setProperty('--app-bottom-control-inset', policy.bottomControlInset);
  }
  // Never move the fixed application frame during the keyboard animation.
  // Only compositor-stable headers compensate the visual viewport pan.
  root.style.setProperty('--app-viewport-offset-top', '0px');
  root.style.setProperty('--app-visual-viewport-offset-top', `${Math.round(metrics.offsetTop)}px`);
  root.style.setProperty('--app-bottom-inset', '0px');
  root.dataset.keyboard = metrics.keyboardOpen ? 'open' : 'closed';
}

export const viewportMetrics = readable<ViewportMetrics>(
  { height: 0, offsetTop: 0, keyboardOpen: false },
  (set) => {
  if (typeof window === 'undefined') return;
  const vv = window.visualViewport;
  const scheduledTimers = new Set<number>();
  let layoutViewportBaseline = window.innerHeight;
  const update = () => {
    const focusedTextInput = hasFocusedTextInput();
    if (!focusedTextInput) {
      layoutViewportBaseline = window.innerHeight;
    }
    const metrics = readViewportMetrics(isTouch() ? layoutViewportBaseline : undefined);
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
  const scheduleVisibleUpdate = () => {
    if (document.visibilityState === 'hidden') {
      return;
    }
    scheduleUpdate();
  };

  update();
  vv?.addEventListener('resize', update);
  vv?.addEventListener('scroll', update);
  window.addEventListener('resize', update, { passive: true });
  window.addEventListener('orientationchange', scheduleUpdate, { passive: true });
  window.addEventListener('pageshow', scheduleVisibleUpdate);
  window.addEventListener('focus', scheduleVisibleUpdate);
  document.addEventListener('visibilitychange', scheduleVisibleUpdate);
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
    window.removeEventListener('pageshow', scheduleVisibleUpdate);
    window.removeEventListener('focus', scheduleVisibleUpdate);
    document.removeEventListener('visibilitychange', scheduleVisibleUpdate);
    window.removeEventListener('focusin', scheduleUpdate, true);
    window.removeEventListener('focusout', scheduleUpdate, true);
    const root = document.documentElement;
    root.style.removeProperty('--app-viewport-height');
    root.style.removeProperty('--app-viewport-offset-top');
    root.style.removeProperty('--app-visual-viewport-offset-top');
    root.style.removeProperty('--app-bottom-inset');
    root.style.removeProperty('--app-bottom-control-inset');
    delete root.dataset.keyboard;
    delete root.dataset.standalonePwa;
  };
});
