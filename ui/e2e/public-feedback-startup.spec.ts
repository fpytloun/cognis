import { expect, test, type Page } from '@playwright/test';

async function publicSession(page: Page, baseURL: string, native: boolean) {
  await page.context().addCookies([{ name: 'owner-session-sentinel', value: 'must-not-be-sent', url: baseURL }]);
  if (native) await page.addInitScript(() => {
    Object.assign(window, {
      __cognisNative: { version: 1, generation: crypto.randomUUID(), server: `${location.origin}/`, surface: 'main' },
      webkit: { messageHandlers: { cognis: { postMessage() {} } } },
    });
  });
}

for (const native of [false, true]) {
  test(`cold public feedback clears startup and accepts a genuine click (native=${native})`, async ({ page, baseURL }) => {
    await publicSession(page, baseURL!, native);
    const privateRequests: string[] = [];
    const requests: { path: string; headers: Record<string, string> }[] = [];
    let release!: () => void, started!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    const exchangeStarted = new Promise<void>(resolve => { started = resolve; });
    const capability = crypto.randomUUID();
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname;
      if (!path.startsWith('/api/v1/public/feedback/')) {
        privateRequests.push(path);
        await route.fulfill({ status: 401, json: { detail: 'Private endpoints are unavailable.' } }); return;
      }
      requests.push({ path, headers: await route.request().allHeaders() });
      if (path.endsWith('/exchange')) {
        started(); await pending;
        await route.fulfill({ json: {
          share_id: crypto.randomUUID(), capability, expires_at: '2099-01-01T00:00:00Z',
          allow_comments: true, discussion: 'own_only', permitted_locator_kinds: ['whole'],
          source: { source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
            content_ref: `art_${crypto.randomUUID().replace(/-/g, '')}`, format: 'plain',
            title: 'Public startup source', canonical_text: null },
        } });
      } else if (path.endsWith('/source')) {
        await route.fulfill({ json: { format: 'plain', mime_type: 'text/plain', filename: 'source.txt',
          content: 'Cold public source is interactive.', rich_payload: null, bytes_path: null } });
      } else await route.fulfill({ status: 404, json: { detail: 'Unexpected public request.' } });
    });
    try {
      await page.goto(`/feedback#${crypto.randomUUID()}`);
      await exchangeStarted;
      // Hydration readiness is independent of the still-pending capability exchange.
      await expect(page.locator('#cognis-startup-loading')).toHaveCount(0);
      await expect(page.getByText('Loading public document…')).toBeVisible();
      release();
      await expect(page.getByText('Cold public source is interactive.')).toBeVisible();
      await expect.poll(() => new URL(page.url()).hash).toBe('');
      await page.getByRole('button', { name: /^Feedback/ }).first().click();
      await expect(page.getByRole('heading', { name: 'Comments', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Join as guest', exact: true })).toBeVisible();
      expect(privateRequests).toEqual([]);
      expect(requests.length).toBeGreaterThanOrEqual(2);
      for (const request of requests) {
        expect(request.headers.cookie).toBeUndefined();
        expect(request.headers.authorization).toBeUndefined();
        expect(request.headers['x-cognis-feedback-capability']).toBe(request.path.endsWith('/exchange') ? undefined : capability);
      }
    } finally { release(); }
  });
}

test('cold native public feedback exposes a rejected-link error without a startup blocker', async ({ page, baseURL }) => {
  await publicSession(page, baseURL!, true);
  const privateRequests: string[] = [];
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname;
    if (path !== '/api/v1/public/feedback/exchange') privateRequests.push(path);
    return route.fulfill({ status: 403, json: { detail: 'This feedback link has been revoked.' } });
  });
  await page.goto(`/feedback#${crypto.randomUUID()}`);
  await expect(page.getByRole('alert')).toHaveText('This feedback link has been revoked.');
  await expect(page.locator('#cognis-startup-loading')).toHaveCount(0);
  expect(privateRequests).toEqual([]);
});

test('cold native malformed public link exposes its route error without private bootstrap', async ({ page, baseURL }) => {
  await publicSession(page, baseURL!, true);
  const apiRequests: string[] = [];
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    apiRequests.push(new URL(route.request().url()).pathname);
    return route.fulfill({ status: 401, json: { detail: 'Unexpected API request.' } });
  });
  await page.goto('/feedback#%E0%A4%A');
  await expect(page.getByRole('alert')).toHaveText('This feedback link is malformed.');
  await expect(page.locator('#cognis-startup-loading')).toHaveCount(0);
  expect(apiRequests).toEqual([]);
});
