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

const INSTALL_DISMISS_KEY = 'cognis-pwa-install-dismissed-until';
const INSTALL_DISMISS_TTL_MS = 30 * 24 * 60 * 60 * 1000;

export const installPromptAvailable = writable(false);

/**
 * Kept for API compatibility with modules that still read this store, but
 * the update-available banner is disabled: we could not reliably
 * distinguish between a genuine new waiting SW and a freshly-registered
 * SW after a hard reset, so the banner kept re-appearing on boot.
 * Service workers still update silently in the background; users see the
 * new UI on their next navigation.
 */
export const updateAvailable = writable(false);

function installPromptEligiblePath(pathname: string): boolean {
  return pathname === '/getting-started' || pathname === '/login' || pathname === '/setup';
}

export function isInstallPromptDismissed(): boolean {
  if (typeof window === 'undefined') return false;
  const raw = window.localStorage.getItem(INSTALL_DISMISS_KEY);
  if (!raw) return false;
  const until = Number(raw);
  if (!Number.isFinite(until)) {
    window.localStorage.removeItem(INSTALL_DISMISS_KEY);
    return false;
  }
  if (until <= Date.now()) {
    window.localStorage.removeItem(INSTALL_DISMISS_KEY);
    return false;
  }
  return true;
}

export function dismissInstallPromptForNow(): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(INSTALL_DISMISS_KEY, String(Date.now() + INSTALL_DISMISS_TTL_MS));
  installPromptAvailable.set(false);
}

// --- update banner ----------------------------

/**
 * No-op kept to preserve the exported API for existing imports.
 */
export function dismissUpdateBanner(): void {
  updateAvailable.set(false);
}

if (typeof window !== 'undefined') {
  window.addEventListener('beforeinstallprompt', (event) => {
    if (!installPromptEligiblePath(window.location.pathname) || isInstallPromptDismissed()) {
      deferredPrompt = null;
      installPromptAvailable.set(false);
      return;
    }
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
    if (choice.outcome === 'dismissed') {
      dismissInstallPromptForNow();
    }
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
 * Service worker registration.
 *
 * A waiting worker is left to activate on its own after all tabs close
 * (the browser default). We no longer raise an "Update available" banner
 * because it could not be distinguished reliably from the normal fresh
 * registration after a hard reset and ended up nagging on every load.
 */
export async function registerServiceWorker(): Promise<void> {
  if (typeof window === 'undefined') return;
  if (!('serviceWorker' in navigator)) return;

  try {
    await navigator.serviceWorker.register('/service-worker.js', {
      type: 'module',
      scope: '/'
    });
  } catch (error) {
    // Service worker registration failures are non-fatal.
    console.warn('[pwa] Service worker registration failed', error);
  }
}

export async function applyUpdate(): Promise<void> {
  if (typeof window === 'undefined') return;

  updateAvailable.set(false);

  // Hard reset: unregister every service worker and delete every Cognis
  // cache, then force a reload. Previous approaches used SKIP_WAITING +
  // controllerchange, which could deadlock on some browsers and cause
  // the banner to reappear indefinitely. The hard reset guarantees the
  // next load has no waiting worker and no stale cached shell, so the
  // new version installs cleanly without a banner loop.
  if ('serviceWorker' in navigator) {
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map((r) => r.unregister().catch(() => false)));
    } catch {
      // ignore
    }
  }

  if (typeof caches !== 'undefined') {
    try {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((n) => n.startsWith('cognis-'))
          .map((n) => caches.delete(n).catch(() => false))
      );
    } catch {
      // ignore
    }
  }

  window.location.reload();
}
