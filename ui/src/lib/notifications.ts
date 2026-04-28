/**
 * Browser notification utilities.
 *
 * Uses standard Web Push for installed PWAs, with a tab-local Notification
 * fallback while the app is open.
 */

import { api } from '$lib/api/client';

const PERMISSION_KEY = 'cognis_notification_permission_asked';
const WEB_PUSH_ENABLED_KEY = 'cognis_web_push_enabled';

/** Whether the browser supports the Notification API. */
export function isSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window;
}

/** Whether this browser can subscribe to Web Push from the current context. */
export function isWebPushSupported(): boolean {
  return (
    typeof window !== 'undefined'
    && 'serviceWorker' in navigator
    && 'PushManager' in window
    && 'Notification' in window
  );
}

/** Whether the app is running as an installed standalone PWA. */
export function isStandaloneDisplay(): boolean {
  if (typeof window === 'undefined') return false;
  return (
    window.matchMedia?.('(display-mode: standalone)').matches
    || (window.navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

/** iOS Safari can only expose Web Push from a Home Screen web app. */
export function needsIosHomeScreenInstall(): boolean {
  if (typeof window === 'undefined') return false;
  const ua = navigator.userAgent || '';
  const isIos = /iPad|iPhone|iPod/.test(ua) && !('MSStream' in window);
  const isSafari = /Safari/i.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/i.test(ua);
  return isIos && isSafari && !isStandaloneDisplay() && !isWebPushSupported();
}

/** Current permission state. */
export function permissionState(): NotificationPermission | 'unsupported' {
  if (!isSupported()) return 'unsupported';
  return Notification.permission;
}

/** Whether we have permission to show notifications. */
export function isGranted(): boolean {
  return isSupported() && Notification.permission === 'granted';
}

/** Whether we've already asked the user (avoid repeated prompts). */
export function hasAskedPermission(): boolean {
  if (typeof localStorage === 'undefined') return false;
  return localStorage.getItem(PERMISSION_KEY) === 'true';
}

export function hasEnabledWebPush(): boolean {
  if (typeof localStorage === 'undefined') return false;
  return localStorage.getItem(WEB_PUSH_ENABLED_KEY) === 'true';
}

function setWebPushEnabled(enabled: boolean): void {
  if (typeof localStorage === 'undefined') return;
  if (enabled) localStorage.setItem(WEB_PUSH_ENABLED_KEY, 'true');
  else localStorage.removeItem(WEB_PUSH_ENABLED_KEY);
}

/** Whether the user is not actively focused on this browser context. */
export function shouldNotifyWhenUnfocused(): boolean {
  if (typeof document === 'undefined') return false;
  return document.visibilityState !== 'visible' || !document.hasFocus();
}

/**
 * Request notification permission from the user.
 * Returns the resulting permission state.
 * Only asks once — subsequent calls return the cached state.
 */
export async function requestPermission(): Promise<NotificationPermission | 'unsupported'> {
  if (!isSupported()) return 'unsupported';
  if (Notification.permission !== 'default') return Notification.permission;
  try {
    const result = await Notification.requestPermission();
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(PERMISSION_KEY, 'true');
    }
    return result;
  } catch {
    return 'denied';
  }
}

function urlBase64ToArrayBuffer(value: string): ArrayBuffer {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    output[i] = raw.charCodeAt(i);
  }
  return output.buffer as ArrayBuffer;
}

function arrayBufferToUrlBase64(value: ArrayBuffer | null): string | null {
  if (!value) return null;
  const bytes = new Uint8Array(value);
  let raw = '';
  for (const byte of bytes) raw += String.fromCharCode(byte);
  return window.btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

export interface EnableWebPushResult {
  ok: boolean;
  status: 'enabled' | 'unsupported' | 'denied' | 'unavailable' | 'error';
  message: string;
}

/** Subscribe this browser to native Web Push notifications. */
export async function enableWebPush(): Promise<EnableWebPushResult> {
  if (!isWebPushSupported()) {
    if (needsIosHomeScreenInstall()) {
      return {
        ok: false,
        status: 'unsupported',
        message: 'On iPhone or iPad, add Cognis to the Home Screen and open it there before enabling notifications.'
      };
    }
    return { ok: false, status: 'unsupported', message: 'This browser does not support Web Push notifications.' };
  }

  try {
    const permission = await requestPermission();
    if (permission !== 'granted') {
      return { ok: false, status: 'denied', message: 'Notifications were not allowed.' };
    }

    const keyResponse = await api.push.vapidPublicKey();
    if (!keyResponse.enabled || !keyResponse.public_key) {
      return {
        ok: false,
        status: 'unavailable',
        message: keyResponse.reason ?? 'Web Push is not configured on this Cognis server.'
      };
    }

    const registration = await navigator.serviceWorker.ready;
    let existing = await registration.pushManager.getSubscription();
    const existingKey = arrayBufferToUrlBase64(existing?.options.applicationServerKey ?? null);
    if (existing && existingKey && existingKey !== keyResponse.public_key) {
      await existing.unsubscribe();
      existing = null;
    }
    const subscription = existing ?? await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToArrayBuffer(keyResponse.public_key)
    });
    const json = subscription.toJSON() as {
      endpoint?: string;
      expirationTime?: number | null;
      keys?: { p256dh?: string; auth?: string };
    };
    const endpoint = json.endpoint ?? subscription.endpoint;
    const p256dh = json.keys?.p256dh;
    const auth = json.keys?.auth;
    if (!endpoint || !p256dh || !auth) {
      return { ok: false, status: 'error', message: 'Browser returned an incomplete push subscription.' };
    }

    await api.push.subscribe({
      endpoint,
      expirationTime: json.expirationTime ?? null,
      keys: { p256dh, auth },
      platform: isStandaloneDisplay() ? 'pwa' : 'browser'
    });
    setWebPushEnabled(true);
    return { ok: true, status: 'enabled', message: 'Native notifications are enabled.' };
  } catch (error) {
    return {
      ok: false,
      status: 'error',
      message: error instanceof Error ? error.message : 'Unable to enable notifications.'
    };
  }
}

/**
 * Reconcile a previously enabled browser subscription with the backend.
 *
 * Browsers can drop PushManager subscriptions and development databases can be
 * recreated. Keep the local enabled flag truthful so notification fallback and
 * prompts do not assume Web Push is healthy when no usable subscription exists.
 */
export async function reconcileWebPushSubscription(): Promise<boolean> {
  if (!hasEnabledWebPush()) return false;
  if (!isWebPushSupported() || !isGranted()) {
    setWebPushEnabled(false);
    return false;
  }

  try {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) {
      setWebPushEnabled(false);
      return false;
    }
    const json = subscription.toJSON() as {
      endpoint?: string;
      expirationTime?: number | null;
      keys?: { p256dh?: string; auth?: string };
    };
    const endpoint = json.endpoint ?? subscription.endpoint;
    const p256dh = json.keys?.p256dh;
    const auth = json.keys?.auth;
    if (!endpoint || !p256dh || !auth) {
      setWebPushEnabled(false);
      return false;
    }
    await api.push.subscribe({
      endpoint,
      expirationTime: json.expirationTime ?? null,
      keys: { p256dh, auth },
      platform: isStandaloneDisplay() ? 'pwa' : 'browser'
    });
    setWebPushEnabled(true);
    return true;
  } catch {
    setWebPushEnabled(false);
    return false;
  }
}

/**
 * Show a browser notification.
 *
 * @param title - Notification title (e.g. agent name)
 * @param body - Notification body text
 * @param conversationId - If provided, clicking the notification navigates to this conversation
 */
export function showNotification(
  title: string,
  body: string,
  conversationId?: string,
): void {
  if (!isGranted()) return;
  try {
    const notification = new Notification(title, {
      body,
      icon: '/favicon.png',
      tag: conversationId ?? 'cognis',
      // Reuse the same tag to avoid notification spam for the same conversation
    });
    notification.onclick = () => {
      window.focus();
      if (conversationId) {
        window.location.href = `/chat/${conversationId}`;
      }
      notification.close();
    };
    // Auto-close after 10 seconds
    setTimeout(() => notification.close(), 10_000);
  } catch {
    // Notification constructor can throw in some environments
  }
}

/**
 * Show a notification for a new message in a conversation the user isn't viewing.
 * Only shows if the document is hidden or the user is on a different conversation.
 */
export function notifyIfHidden(
  title: string,
  body: string,
  conversationId: string,
  activeConversationId: string | null,
): void {
  if (!shouldNotifyWhenUnfocused()) return;
  if (hasEnabledWebPush()) return;
  showNotification(title, body, conversationId);
}
