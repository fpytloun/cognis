import { readable, writable } from 'svelte/store';

/**
 * PWA lifecycle stores + helpers.
 *
 * - `installPromptAvailable`: true when the browser has fired `beforeinstallprompt`
 *   and we've stashed the event. Components can call `promptInstall()` to trigger.
 * - `displayMode`: 'standalone' | 'browser' — detects whether the app is launched
 *   from the home screen / installed app vs a regular tab.
 * - `updateAvailable`: fallback for legacy/stuck registrations where a newer
 *   service worker is waiting while the current page is already controlled by
 *   an older worker. Normal updates are activated by the SW and reload clients
 *   automatically.
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

export const updateAvailable = writable(false);

const UPDATE_RELOAD_TIMEOUT_MS = 5000;
const SERVICE_WORKER_MESSAGE_TIMEOUT_MS = 1000;

let waitingUpdateWorker: ServiceWorker | null = null;
let controllerChangeObserverInstalled = false;
let serviceWorkerMessageObserverInstalled = false;
let serviceWorkerUpdateReloadScheduled = false;

type ServiceWorkerVersionReply = {
  type?: string;
  version?: unknown;
};

type ServiceWorkerClientMessage = {
  type?: string;
  version?: unknown;
};

function hasActiveServiceWorkerController(): boolean {
  return typeof navigator !== 'undefined'
    && 'serviceWorker' in navigator
    && Boolean(navigator.serviceWorker.controller);
}

function setWaitingUpdateWorker(worker: ServiceWorker | null): void {
  waitingUpdateWorker = worker;
  updateAvailable.set(Boolean(worker) && hasActiveServiceWorkerController());
}

function observeServiceWorkerControllerChange(): void {
  if (controllerChangeObserverInstalled) return;
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
  if (typeof navigator.serviceWorker.addEventListener !== 'function') return;
  controllerChangeObserverInstalled = true;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    setWaitingUpdateWorker(null);
  });
}

export function handleServiceWorkerClientMessage(
  data: ServiceWorkerClientMessage | undefined,
  reloadPage: () => void = () => window.location.reload(),
): boolean {
  if (data?.type !== 'COGNIS_SW_UPDATED') return false;
  if (serviceWorkerUpdateReloadScheduled) return true;
  serviceWorkerUpdateReloadScheduled = true;
  setWaitingUpdateWorker(null);
  reloadPage();
  return true;
}

function observeServiceWorkerMessages(): void {
  if (serviceWorkerMessageObserverInstalled) return;
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
  if (typeof navigator.serviceWorker.addEventListener !== 'function') return;
  serviceWorkerMessageObserverInstalled = true;
  navigator.serviceWorker.addEventListener('message', (event) => {
    handleServiceWorkerClientMessage(event.data as ServiceWorkerClientMessage | undefined);
  });
}

async function resolveCurrentWaitingUpdateWorker(): Promise<ServiceWorker | null> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return null;
  try {
    const registration = await navigator.serviceWorker.getRegistration?.();
    if (registration) {
      setWaitingUpdateWorker(registration.waiting ?? null);
      return registration.waiting ?? null;
    }
  } catch {
    // Fall back to the worker observed during registration; update activation is best-effort.
  }
  return waitingUpdateWorker;
}

function reconcileWaitingUpdateWorker(registration: ServiceWorkerRegistration): void {
  setWaitingUpdateWorker(
    hasActiveServiceWorkerController()
      ? registration.waiting ?? null
      : null,
  );
}

function observeInstallingWorker(
  registration: ServiceWorkerRegistration,
  worker: ServiceWorker,
): void {
  reconcileWaitingUpdateWorker(registration);
  worker.addEventListener('statechange', () => {
    reconcileWaitingUpdateWorker(registration);
  });
}

function observeServiceWorkerRegistration(registration: ServiceWorkerRegistration): void {
  reconcileWaitingUpdateWorker(registration);

  if (registration.installing) {
    observeInstallingWorker(registration, registration.installing);
  }

  registration.addEventListener('updatefound', () => {
    if (registration.installing) {
      observeInstallingWorker(registration, registration.installing);
    }
  });
}

function postServiceWorkerMessageWithReply(
  worker: ServiceWorker,
  message: Record<string, unknown>,
  timeoutMs = SERVICE_WORKER_MESSAGE_TIMEOUT_MS,
): Promise<ServiceWorkerVersionReply | null> {
  if (typeof window === 'undefined' || typeof MessageChannel === 'undefined') {
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    const channel = new MessageChannel();
    let settled = false;
    const finish = (reply: ServiceWorkerVersionReply | null) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      channel.port1.onmessage = null;
      channel.port1.close();
      channel.port2.close();
      resolve(reply);
    };
    const timeout = window.setTimeout(() => finish(null), timeoutMs);
    channel.port1.onmessage = (event) => finish(event.data as ServiceWorkerVersionReply | null);
    channel.port1.start?.();

    try {
      worker.postMessage(message, [channel.port2]);
    } catch {
      finish(null);
    }
  });
}

async function serviceWorkerVersion(worker: ServiceWorker | null | undefined): Promise<string | null> {
  if (!worker) return null;
  const reply = await postServiceWorkerMessageWithReply(worker, { type: 'GET_VERSION' });
  return reply?.type === 'VERSION' && typeof reply.version === 'string' ? reply.version : null;
}

async function waitForControllerChange(targetVersion: string | null): Promise<boolean> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return false;
  if (typeof navigator.serviceWorker.addEventListener !== 'function') return false;

  return new Promise((resolve) => {
    let settled = false;
    const cleanup = () => {
      window.clearTimeout(timeout);
      navigator.serviceWorker.removeEventListener?.('controllerchange', handleControllerChange);
    };
    const finish = (activated: boolean) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(activated);
    };
    const timeout = window.setTimeout(() => finish(false), UPDATE_RELOAD_TIMEOUT_MS);
    const handleControllerChange = () => {
      void (async () => {
        if (!targetVersion) {
          finish(true);
          return;
        }
        const controllerVersion = await serviceWorkerVersion(navigator.serviceWorker.controller);
        finish(controllerVersion === targetVersion);
      })();
    };

    navigator.serviceWorker.addEventListener('controllerchange', handleControllerChange, { once: true });
  });
}

async function clearCognisServiceWorkerCaches(): Promise<void> {
  if (typeof window === 'undefined' || !('caches' in window)) return;
  const names = await window.caches.keys();
  await Promise.all(
    names
      .filter((name) => name.startsWith('cognis-'))
      .map((name) => window.caches.delete(name)),
  );
}

async function resetCognisServiceWorker(registration: ServiceWorkerRegistration | null): Promise<void> {
  try {
    await registration?.unregister();
  } catch {
    // Cache reset and reload are still useful if unregister fails.
  }
  await clearCognisServiceWorkerCaches();
  setWaitingUpdateWorker(null);
}

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
 * Hides the update banner for the current page lifetime. The waiting worker
 * is intentionally kept so the user can still apply it if another UI path
 * calls `applyUpdate()`.
 */
export function dismissUpdateBanner(): void {
  serviceWorkerUpdateReloadScheduled = false;
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

export function detectDisplayMode(): 'standalone' | 'browser' {
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
  const platform = navigator.platform || '';
  const maxTouchPoints = navigator.maxTouchPoints || 0;
  const isIos = (/iPad|iPhone|iPod/.test(ua) || (platform === 'MacIntel' && maxTouchPoints > 1))
    && (typeof window === 'undefined' || !('MSStream' in window));
  const isSafari = /Safari/i.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/i.test(ua);
  return isIos && isSafari;
}

export function isIosStandalonePwa(): boolean {
  if (typeof window === 'undefined' || typeof navigator === 'undefined') return false;
  return isIosSafari() && (window.navigator as Navigator & { standalone?: boolean }).standalone === true;
}

export function canAttemptPwaAuxiliaryWindow(): boolean {
  return typeof window !== 'undefined' && !isIosStandalonePwa();
}

// --- Service worker registration ------------------------------------------

/**
 * Service worker registration.
 *
 * New workers normally auto-activate and reload clients from the service
 * worker. A waiting worker is surfaced only as a legacy/stuck-registration
 * fallback when this page is already controlled by an older worker.
 */
export async function registerServiceWorker(): Promise<void> {
  if (typeof window === 'undefined') return;
  if (!('serviceWorker' in navigator)) return;

  try {
    observeServiceWorkerControllerChange();
    observeServiceWorkerMessages();
    const registration = await navigator.serviceWorker.register('/service-worker.js', {
      type: 'module',
      scope: '/'
    });
    observeServiceWorkerRegistration(registration);
    void registration.update().catch(() => {
      // Browser update checks are best-effort; registration itself succeeded.
    });
  } catch (error) {
    // Service worker registration failures are non-fatal.
    console.warn('[pwa] Service worker registration failed', error);
  }
}

export async function applyUpdate(reloadPage?: () => void): Promise<void> {
  if (typeof window === 'undefined') return;

  updateAvailable.set(false);

  let registration: ServiceWorkerRegistration | null = null;
  if ('serviceWorker' in navigator) {
    try {
      registration = await navigator.serviceWorker.getRegistration?.() ?? null;
    } catch {
      registration = null;
    }
  }

  const worker = registration?.waiting ?? await resolveCurrentWaitingUpdateWorker();

  if ('serviceWorker' in navigator && worker && navigator.serviceWorker.controller) {
    const targetVersion = await serviceWorkerVersion(worker);
    const activation = waitForControllerChange(targetVersion);
    try {
      worker.postMessage({ type: 'SKIP_WAITING' });
    } catch {
      // Fall through to activation timeout handling below.
    }

    const activated = await activation;
    if (!activated) {
      try {
        registration = await navigator.serviceWorker.getRegistration?.() ?? registration;
      } catch {
        // Keep the previous registration reference for best-effort reset.
      }
      if (registration?.waiting) {
        await resetCognisServiceWorker(registration);
      }
    }
  }

  (reloadPage ?? (() => window.location.reload()))();
}
