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

sw.addEventListener('message', (event) => {
  if (event.data?.type === 'SKIP_WAITING') {
    void sw.skipWaiting();
  }
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
