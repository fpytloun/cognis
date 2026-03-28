import type { Handle } from '@sveltejs/kit';

const contentSecurityPolicy = [
  "default-src 'self'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "img-src 'self' data: blob: http: https:",
  "font-src 'self' data:",
  "object-src 'none'",
  "connect-src 'self' http: https: ws: wss:",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'"
].join('; ');

export const handle: Handle = async ({ event, resolve }) => {
  const response = await resolve(event);
  response.headers.set('Content-Security-Policy', contentSecurityPolicy);
  return response;
};
