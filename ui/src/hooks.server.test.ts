import { expect, it, vi } from 'vitest';
import type { RequestEvent } from '@sveltejs/kit';
import { handle } from './hooks.server';
vi.mock('$app/environment', () => ({ dev: true }));
it('permits local Blob preview frames without opening cross-origin embedding or changing other fences', async () => {
  const response = await handle({ event: {} as RequestEvent, resolve: async () => new Response('Local shell') });
  const policy = response.headers.get('content-security-policy');
  expect(policy).toContain("frame-src 'self' blob:");
  expect(policy).toContain("frame-ancestors 'self'");
  expect(policy).toContain("object-src 'none'");
  expect(policy).toContain("default-src 'self'");
  expect(policy).not.toMatch(/frame-src[^;]*https?:/);
});
