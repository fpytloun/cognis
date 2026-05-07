/// <reference types="@sveltejs/kit" />
/// <reference lib="webworker" />

// Cognis PWA service worker.
//
// Strategy (deliberately conservative):
//   - Precache the static app shell (SvelteKit build artifacts + prerendered
//     HTML + static files). These are user-agnostic and hash-busted per deploy.
//   - Runtime-cache static-looking assets (images/fonts) only. User-specific
//     API payloads are NEVER cached to avoid cross-user data leaks on shared
//     devices after logout/login.
//   - API requests pass through to the network. If the network fails, return
//     a structured JSON offline error (503), NEVER the HTML shell (which
//     would break JSON-consuming code paths).
//   - WebSocket upgrades and /api/* POST/PUT/PATCH/DELETE requests pass
//     through unchanged.
//   - Navigation requests fall back to the precached app shell so the SPA
//     still boots offline.
//
// Update UX:
//   - New SW versions install in the background. `skipWaiting()` is only
//     called on explicit `SKIP_WAITING` message from the page, so the user
//     sees the "Reload" banner and controls when the update applies.
//   - `clients.claim()` is called on activate so the newly-activated SW
//     takes control of already-open tabs immediately after reload.

import { build, files, prerendered, version } from '$service-worker';

const sw = self as unknown as ServiceWorkerGlobalScope;

const PRECACHE = `cognis-precache-${version}`;
const RUNTIME = `cognis-runtime-${version}`;

const PRECACHE_URLS = [
  ...build,
  ...files.filter((f) => !f.endsWith('.map')),
  ...prerendered
];

sw.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(PRECACHE);
      await cache.addAll(PRECACHE_URLS);
      // Do NOT skipWaiting here — wait for user to confirm via the update
      // banner. The page will post SKIP_WAITING when ready.
    })()
  );
});

sw.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(
        names
          .filter((n) => n.startsWith('cognis-') && n !== PRECACHE && n !== RUNTIME)
          .map((n) => caches.delete(n))
      );
      // Take control of existing clients; required so the reload after
      // SKIP_WAITING is served by the new SW.
      await sw.clients.claim();
    })()
  );
});

type WebPushPayload = {
  title?: string;
  body?: string;
  url?: string;
  tag?: string;
  kind?: string;
  conversation_id?: string;
  icon?: unknown;
};

type ActiveConversationMessage = {
  type?: string;
  conversation_id?: string | null;
  active?: boolean;
};

const activeConversationByClient = new Map<string, string>();

sw.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') {
    void sw.skipWaiting();
    return;
  }
  const data = event.data as ActiveConversationMessage | undefined;
  if (data?.type === 'ACTIVE_CONVERSATION') {
    const sourceId = (event.source as Client | null | undefined)?.id;
    if (!sourceId) return;
    const conversationId = typeof data.conversation_id === 'string' ? data.conversation_id.trim() : '';
    if (data.active && conversationId) activeConversationByClient.set(sourceId, conversationId);
    else activeConversationByClient.delete(sourceId);
  }
});

function parsePushPayload(event: PushEvent): WebPushPayload {
  if (!event.data) return {};
  try {
    return event.data.json() as WebPushPayload;
  } catch {
    return { body: event.data.text() };
  }
}

function notificationIcon(value: unknown): string {
  if (typeof value !== 'string' || value.startsWith('//')) return '/pwa/icon-192.png';
  try {
    const url = new URL(value, sw.location.origin);
    if (url.origin !== sw.location.origin) return '/pwa/icon-192.png';
    return url.href;
  } catch {
    return '/pwa/icon-192.png';
  }
}

function isIosWebKit(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent || '');
}

function sameOriginTarget(value: unknown): string | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value, sw.location.origin);
    if (url.origin !== sw.location.origin) return null;
    return url.href;
  } catch {
    return null;
  }
}

function notificationTarget(notification: Notification): string {
  const data = notification.data as { url?: unknown; conversation_id?: unknown } | undefined;
  const dataUrl = sameOriginTarget(data?.url);
  if (dataUrl) return dataUrl;
  if (typeof data?.conversation_id === 'string' && data.conversation_id.trim()) {
    return new URL(`/chat/${encodeURIComponent(data.conversation_id)}`, sw.location.origin).href;
  }
  if (/^conv_[A-Za-z0-9_-]+$/.test(notification.tag || '')) {
    return new URL(`/chat/${encodeURIComponent(notification.tag)}`, sw.location.origin).href;
  }
  return new URL('/chat', sw.location.origin).href;
}

function arrayBufferToUrlBase64(value: ArrayBuffer | null): string | null {
  if (!value) return null;
  const bytes = new Uint8Array(value);
  let raw = '';
  for (const byte of bytes) raw += String.fromCharCode(byte);
  return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

function urlBase64ToArrayBuffer(value: string): ArrayBuffer {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    output[i] = raw.charCodeAt(i);
  }
  return output.buffer as ArrayBuffer;
}

async function fetchApplicationServerKey(): Promise<ArrayBuffer | null> {
  try {
    const response = await fetch('/api/v1/push/vapid-public-key', { credentials: 'same-origin' });
    if (!response.ok) return null;
    const payload = await response.json() as { enabled?: boolean; public_key?: string | null };
    if (!payload.enabled || !payload.public_key) return null;
    return urlBase64ToArrayBuffer(payload.public_key);
  } catch {
    return null;
  }
}

async function uploadPushSubscription(subscription: PushSubscription): Promise<void> {
  const json = subscription.toJSON() as {
    endpoint?: string;
    expirationTime?: number | null;
    keys?: { p256dh?: string; auth?: string };
  };
  const endpoint = json.endpoint ?? subscription.endpoint;
  const p256dh = json.keys?.p256dh;
  const auth = json.keys?.auth;
  if (!endpoint || !p256dh || !auth) return;
  const response = await fetch('/api/v1/push/subscriptions', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      endpoint,
      expirationTime: json.expirationTime ?? null,
      keys: { p256dh, auth },
      platform: 'pwa'
    })
  });
  if (!response.ok) {
    throw new Error('Push subscription upload failed');
  }
}

sw.addEventListener('pushsubscriptionchange', (event) => {
  const changeEvent = event as ExtendableEvent & { oldSubscription?: PushSubscription | null };
  changeEvent.waitUntil(
    (async () => {
      const existingKey = arrayBufferToUrlBase64(changeEvent.oldSubscription?.options.applicationServerKey ?? null);
      const applicationServerKey = existingKey
        ? urlBase64ToArrayBuffer(existingKey)
        : await fetchApplicationServerKey();
      if (!applicationServerKey) return;
      const subscription = await sw.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey
      });
      await uploadPushSubscription(subscription);
    })()
  );
});

function conversationPath(conversationId: string | undefined): string | null {
  if (!conversationId) return null;
  return `/chat/${encodeURIComponent(conversationId)}`;
}

function isForegroundClient(client: WindowClient): boolean {
  return client.focused || client.visibilityState === 'visible';
}

async function hasForegroundClientFor(target: URL, conversationId: string | undefined): Promise<boolean> {
  const targetConversationPath = conversationPath(conversationId);
  const clients = await sw.clients.matchAll({ type: 'window', includeUncontrolled: true });
  return clients.some((client) => {
    const windowClient = client as WindowClient;
    try {
      const url = new URL(windowClient.url);
      if (url.origin !== sw.location.origin) return false;
      if (conversationId && activeConversationByClient.get(windowClient.id) === conversationId) return true;
      if (!isForegroundClient(windowClient)) return false;
      return url.pathname === target.pathname
        || (targetConversationPath !== null && url.pathname === targetConversationPath);
    } catch {
      return false;
    }
  });
}

sw.addEventListener('push', (event) => {
  const payload = parsePushPayload(event);
  event.waitUntil(
    (async () => {
      const target = new URL(payload.url || '/chat', sw.location.origin);
      if (await hasForegroundClientFor(target, payload.conversation_id)) return;
      await sw.registration.showNotification(payload.title || 'Cognis', {
        body: payload.body || 'Cognis needs your attention.',
        icon: notificationIcon(payload.icon),
        badge: '/pwa/icon-192.png',
        tag: payload.tag || 'cognis',
        data: {
          url: `${target.pathname}${target.search}${target.hash}`,
          conversation_id: payload.conversation_id,
          kind: payload.kind || 'notification'
        },
      });
    })()
  );
});

sw.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const target = notificationTarget(event.notification);
      const clients = await sw.clients.matchAll({ type: 'window', includeUncontrolled: true });

      for (const client of clients) {
        const windowClient = client as WindowClient;
        try {
          if (new URL(windowClient.url).href !== target) continue;
          await windowClient.focus();
          return;
        } catch {
          // Try the next window client.
        }
      }

      if (isIosWebKit()) {
        await sw.clients.openWindow(target);
        return;
      }

      for (const client of clients) {
        const windowClient = client as WindowClient;
        try {
          if (new URL(windowClient.url).origin !== sw.location.origin) continue;
          const navigated = await windowClient.navigate(target);
          await (navigated ?? windowClient).focus();
          return;
        } catch {
          // Try the next window client.
        }
      }

      await sw.clients.openWindow(target);
    })()
  );
});

/**
 * Returns true if the request must bypass the SW entirely (no caching, no
 * offline fallback). This covers:
 *   - non-GET requests
 *   - WebSocket upgrades and SSE streams
 *   - ALL API calls (so user-specific data is never cached)
 *   - cross-origin requests
 */
function isPassthrough(request: Request, url: URL): boolean {
  if (request.method !== 'GET') return true;

  if (request.headers.get('upgrade') === 'websocket') return true;
  const accept = request.headers.get('accept') ?? '';
  if (accept.includes('text/event-stream')) return true;

  // All /api/** endpoints pass through. Cognis API responses are scoped to
  // the current user via JWT; caching them in a shared browser cache would
  // leak data across user sessions after logout/login on the same device.
  if (url.pathname.startsWith('/api/')) return true;

  // Well-known endpoints: JWKS etc. are public but rotate — passthrough is
  // simpler than caching and doesn't cost anything the browser won't already
  // do with HTTP cache headers.
  if (url.pathname.startsWith('/.well-known/')) return true;

  if (url.origin !== sw.location.origin) return true;

  return false;
}

function isPrecached(url: URL): boolean {
  return PRECACHE_URLS.includes(url.pathname);
}

async function cacheFirst(request: Request): Promise<Response> {
  const cache = await caches.open(PRECACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const runtime = await caches.open(RUNTIME);
      void runtime.put(request, response.clone()).catch(() => {});
    }
    return response;
  } catch {
    // If a precached asset is missing locally (shouldn't happen post-install)
    // return a 503 rather than anything misleading.
    return new Response('Offline', { status: 503, statusText: 'Offline' });
  }
}

async function staleWhileRevalidate(request: Request): Promise<Response> {
  const cache = await caches.open(RUNTIME);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request)
    .then((response) => {
      if (response.ok) void cache.put(request, response.clone()).catch(() => {});
      return response;
    })
    .catch(() => cached);
  return cached ?? (await fetchPromise) ?? new Response('', { status: 504 });
}

sw.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (isPassthrough(event.request, url)) return;

  // Precached app shell + build assets.
  if (isPrecached(url)) {
    event.respondWith(cacheFirst(event.request));
    return;
  }

  // Static-looking images/fonts: stale-while-revalidate.
  if (/\.(?:png|jpg|jpeg|webp|avif|svg|gif|ico|woff2?)$/i.test(url.pathname)) {
    event.respondWith(staleWhileRevalidate(event.request));
    return;
  }

  // Navigation requests (SPA): try network first; fall back to precached
  // shell so the app still boots offline. Non-navigation requests fall
  // through untouched.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      (async () => {
        try {
          return await fetch(event.request);
        } catch {
          const cache = await caches.open(PRECACHE);
          const shell =
            (await cache.match('/index.html')) ??
            (await cache.match('/')) ??
            (await caches.match('/index.html'));
          if (shell) return shell;
          return new Response('Offline', { status: 503 });
        }
      })()
    );
  }
});

export {};
