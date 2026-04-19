import { readable, writable } from 'svelte/store';

/**
 * PWA lifecycle stores + helpers.
 *
 * - `installPromptAvailable`: true when the browser has fired `beforeinstallprompt`
 *   and we've stashed the event. Components can call `promptInstall()` to trigger.
 * - `displayMode`: 'standalone' | 'browser' — detects whether the app is launched
 *   from the home screen / installed app vs a regular tab.
 * - `updateAvailable`: true when the service worker posted a `sw:update` message
 *   indicating a new version has been activated.
 * - `isIosSafari`: true on iOS Safari where Add-to-Home-Screen is manual.
 */

// --- Install prompt (Android/Chrome/Edge) ---------------------------------

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
}

let deferredPrompt: BeforeInstallPromptEvent | null = null;

const AUTO_APPLY_WAITING_PREFIX = 'cognis-sw-auto-apply:';
const AUTO_APPLY_TIMEOUT_MS = 5000;

export const installPromptAvailable = writable(false);
export const updateAvailable = writable(false);

function waitingWorkerAttemptKey(waiting: ServiceWorker): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return `${AUTO_APPLY_WAITING_PREFIX}${waiting.scriptURL}`;
  } catch {
    return null;
  }
}

function activateWaitingWorker(waiting: ServiceWorker, options: { auto?: boolean } = {}): void {
  updateAvailable.set(false);

  const attemptKey = options.auto ? waitingWorkerAttemptKey(waiting) : null;
  let timeoutId: number | null = null;

  const onControllerChange = () => {
    if (timeoutId !== null) {
      window.clearTimeout(timeoutId);
    }
    if (attemptKey) {
      window.sessionStorage.removeItem(attemptKey);
    }
    window.location.reload();
  };

  navigator.serviceWorker.addEventListener(
    'controllerchange',
    onControllerChange,
    { once: true }
  );

  if (attemptKey) {
    timeoutId = window.setTimeout(() => {
      navigator.serviceWorker.removeEventListener('controllerchange', onControllerChange);
      updateAvailable.set(true);
    }, AUTO_APPLY_TIMEOUT_MS);
  }

  waiting.postMessage({ type: 'SKIP_WAITING' });
}

function tryAutoApplyWaitingWorker(waiting: ServiceWorker): boolean {
  const attemptKey = waitingWorkerAttemptKey(waiting);
  if (!attemptKey) return false;
  if (window.sessionStorage.getItem(attemptKey) === '1') {
    return false;
  }
  window.sessionStorage.setItem(attemptKey, '1');
  activateWaitingWorker(waiting, { auto: true });
  return true;
}

if (typeof window !== 'undefined') {
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    deferredPrompt = event as BeforeInstallPromptEvent;
    installPromptAvailable.set(true);
  });

  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    installPromptAvailable.set(false);
  });
}

/**
 * Trigger the stashed install prompt. Returns the outcome or null if unavailable.
 */
export async function promptInstall(): Promise<'accepted' | 'dismissed' | null> {
  if (!deferredPrompt) return null;
  try {
    await deferredPrompt.prompt();
    const choice = await deferredPrompt.userChoice;
    deferredPrompt = null;
    installPromptAvailable.set(false);
    return choice.outcome;
  } catch {
    return null;
  }
}

// --- Display mode (standalone vs browser) ---------------------------------

function detectDisplayMode(): 'standalone' | 'browser' {
  if (typeof window === 'undefined') return 'browser';
  const mq = window.matchMedia?.('(display-mode: standalone)');
  // iOS Safari exposes navigator.standalone instead of display-mode media query.
  const iosStandalone = (window.navigator as Navigator & { standalone?: boolean }).standalone === true;
  if (mq?.matches || iosStandalone) return 'standalone';
  return 'browser';
}

export const displayMode = readable<'standalone' | 'browser'>(detectDisplayMode(), (set) => {
  if (typeof window === 'undefined') return;
  const mq = window.matchMedia?.('(display-mode: standalone)');
  const update = () => set(detectDisplayMode());
  mq?.addEventListener?.('change', update);
  return () => mq?.removeEventListener?.('change', update);
});

// --- iOS detection --------------------------------------------------------

export function isIosSafari(): boolean {
  if (typeof navigator === 'undefined') return false;
  const ua = navigator.userAgent || '';
  const isIos = /iPad|iPhone|iPod/.test(ua) && !('MSStream' in window);
  const isSafari = /Safari/i.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/i.test(ua);
  return isIos && isSafari;
}

// --- Service worker registration ------------------------------------------

/**
 * Service worker registration + update lifecycle.
 *
 * The SW installs new versions to the `waiting` state — we never auto-apply
 * them. The user sees an "Update available" banner and taps Reload, which
 * posts `SKIP_WAITING` to the waiting SW; it activates and `controllerchange`
 * fires on the page, triggering a reload so the new assets take effect.
 *
 * First installs (no existing controller) do NOT raise the update banner so
 * we don't nag fresh visitors.
 */
export async function registerServiceWorker(): Promise<void> {
  if (typeof window === 'undefined') return;
  if (!('serviceWorker' in navigator)) return;

  try {
    const registration = await navigator.serviceWorker.register('/service-worker.js', {
      type: 'module',
      scope: '/'
    });

    // If there's already a waiting SW at page load, the user has already
    // refreshed or reopened the app since the update banner first appeared.
    // Finish applying the update instead of showing the same banner again.
    if (registration.waiting && navigator.serviceWorker.controller) {
      if (!tryAutoApplyWaitingWorker(registration.waiting)) {
        updateAvailable.set(true);
      }
      return;
    }

    // When a new SW finishes installing AND an existing controller is in
    // charge, a fresh version is ready to activate. Offer the update banner.
    registration.addEventListener('updatefound', () => {
      const installing = registration.installing;
      if (!installing) return;
      installing.addEventListener('statechange', () => {
        if (installing.state === 'installed' && navigator.serviceWorker.controller) {
          updateAvailable.set(true);
        }
      });
    });
  } catch (error) {
    // Service worker registration failures are non-fatal.
    console.warn('[pwa] Service worker registration failed', error);
  }
}

export async function applyUpdate(): Promise<void> {
  if (typeof window === 'undefined' || !('serviceWorker' in navigator)) return;
  const registration = await navigator.serviceWorker.getRegistration();
  const waiting = registration?.waiting;
  if (!waiting) {
    // Nothing waiting — a reload is still the user's "apply" gesture.
    window.location.reload();
    return;
  }
  activateWaitingWorker(waiting);
}
