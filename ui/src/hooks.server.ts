import type { Handle } from '@sveltejs/kit';
import { dev } from '$app/environment';

// NOTE: This header only ever applies to the Node-based `vite dev` / `vite
// preview` servers. The real production deployment builds via
// @sveltejs/adapter-static and serves the resulting static files through
// Cognis's Python `SPAMiddleware` (cognis/ui_assets.py), which sets no CSP
// header of its own — so this file's CSP never reaches production traffic.
// It exists purely to give local `npm run dev`/`preview` a reasonably strict
// baseline. Standalone/artifact deliverable routes get their own dedicated,
// stricter CSP directly from the backend (cognis/api/routes/deliverables.py,
// artifacts.py) since those serve less-trusted rendered content.
//
// In dev mode specifically, SvelteKit's SSR HTML includes inline hydration
// bootstrap scripts (and Vite's HMR client) that have no nonce/hash, so a
// strict `script-src 'self'` blocks the app from mounting at all. `dev` is
// statically inlined to `false` in production builds, so this relaxation is
// dead-code-eliminated and can never leak into a built artifact regardless.
const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "img-src 'self' data: blob: http: https:",
  "font-src 'self' data:",
  "object-src 'none'",
  "connect-src 'self' http: https: ws: wss:",
  dev ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'" : "script-src 'self'",
  "style-src 'self' 'unsafe-inline'"
].join('; ');

export const handle: Handle = async ({ event, resolve }) => {
  const response = await resolve(event);
  response.headers.set('Content-Security-Policy', contentSecurityPolicy);
  return response;
};
