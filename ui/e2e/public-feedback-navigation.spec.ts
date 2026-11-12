import { expect, test } from '@playwright/test';
import type { PublicFeedbackExchange } from '../src/lib/feedback/public-api';

function share(title: string, allow_comments: boolean): PublicFeedbackExchange {
  return {
    share_id: crypto.randomUUID(), capability: crypto.randomUUID(), expires_at: '2099-01-01T00:00:00Z',
    allow_comments, discussion: 'own_only', permitted_locator_kinds: ['whole'],
    source: { source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
      content_ref: `art_${crypto.randomUUID().replace(/-/g, '')}`, title, format: 'plain', canonical_text: null },
  };
}

for (const native of [false, true]) {
  test(`same-document link reopening refreshes permissions and retains only the matching guest draft (native=${native})`, async ({ page, baseURL }) => {
    await page.context().addCookies([{ name: 'owner-session-sentinel', value: 'must-not-be-sent', url: baseURL! }]);
    if (native) await page.addInitScript(() => Object.assign(window, {
      __cognisNative: { version: 1, generation: crypto.randomUUID(), server: `${location.origin}/`, surface: 'main' },
      webkit: { messageHandlers: { cognis: { postMessage() {} } } },
    }));
    const first = share('Pinned first navigation source', false);
    const second = share('Pinned second navigation source', true);
    second.source.content_digest = 'b'.repeat(64);
    const firstToken = crypto.randomUUID(), secondToken = crypto.randomUUID();
    const guest = { guest_session_id: crypto.randomUUID(), guest_capability: crypto.randomUUID(),
      expires_at: '2099-01-01T00:00:00Z' };
    const capabilities = new Map<string, PublicFeedbackExchange>();
    const exchanged: string[] = [];
    const unexpected: string[] = [];
    const requests: { path: string; headers: Record<string, string> }[] = [];
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const headers = await request.allHeaders();
      requests.push({ path, headers });
      if (path === '/api/v1/public/feedback/exchange') {
        const token = request.postDataJSON().share_token;
        const value = token === firstToken ? first : token === secondToken ? second : null;
        if (value) {
          exchanged.push(token);
          const snapshot = { ...value, capability: crypto.randomUUID() };
          capabilities.set(snapshot.capability, snapshot);
          await route.fulfill({ json: snapshot }); return;
        }
      }
      const value = capabilities.get(headers['x-cognis-feedback-capability']);
      if (value && path === '/api/v1/public/feedback/source') {
        await route.fulfill({ json: { format: 'plain', mime_type: 'text/plain',
          filename: value.source.title, content: `Exact content for ${value.source.content_ref}`,
          rich_payload: null, bytes_path: null } }); return;
      }
      if (value && path === '/api/v1/public/feedback/sources/resolve') {
        expect(request.postDataJSON().source).toEqual({ kind: 'document', content_ref: value.source.content_ref });
        await route.fulfill({ json: {
          subject_id: value.share_id, source_token: value.source.source_token,
          source: { kind: 'document', content_ref: value.source.content_ref }, context: { kind: 'standalone' },
          content_digest: value.source.content_digest, canonical_text: null, availability: 'available',
          lineage_id: null, revision: null, latest_content_ref: null, format: 'plain', title: value.source.title,
          permitted_locator_kinds: ['whole'], capabilities: {
            read: true, comment: value.allow_comments, moderate: false, dispatch: false, disclose: false },
        } }); return;
      }
      if (value?.share_id === first.share_id && path === '/api/v1/public/feedback/guest-sessions') {
        await route.fulfill({ json: guest }); return;
      }
      if (value && path === '/api/v1/public/feedback/threads' && request.method() === 'GET') {
        await route.fulfill({ json: { items: [], next_cursor: null } }); return;
      }
      if (value && path === '/api/v1/public/feedback/outcomes') {
        await route.fulfill({ json: { items: [] } }); return;
      }
      unexpected.push(path);
      await route.fulfill({ status: 403, json: { detail: 'Unexpected endpoint.' } });
    });
    const firstLink = `/feedback#${encodeURIComponent(firstToken)}`;
    async function loaded(value: PublicFeedbackExchange) {
      await expect(page.getByText(`Exact content for ${value.source.content_ref}`)).toBeVisible();
      await expect.poll(() => page.evaluate(() => location.hash)).toBe('');
      await page.getByRole('button', { name: /^Feedback/ }).first().click();
      await expect(page.getByRole('heading', { name: 'Comments', exact: true })).toBeVisible();
    }
    await page.goto(firstLink);
    await loaded(first);
    await expect(page.getByText('Commenting is disabled for this link.')).toBeVisible();
    const originalDocument = await page.evaluateHandle(() => document);

    first.allow_comments = true;
    await page.goto(firstLink);
    await loaded(first);
    await expect(page.getByRole('button', { name: 'Join as guest', exact: true })).toBeVisible();
    await page.getByRole('textbox', { name: 'Display name (optional)' }).fill('Returning visitor');
    await page.getByRole('button', { name: 'Join as guest', exact: true }).click();
    await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Keep this unsent guest draft.');

    await page.goto(firstLink);
    await loaded(first);
    await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toHaveValue('Keep this unsent guest draft.');
    await expect(page.getByRole('button', { name: 'Join as guest', exact: true })).toHaveCount(0);
    expect(exchanged).toEqual([firstToken, firstToken, firstToken]);

    await page.goto(`/feedback#${encodeURIComponent(secondToken)}`);
    await loaded(second);
    await expect(page.getByRole('button', { name: 'Join as guest', exact: true })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toHaveCount(0);
    await expect(page.getByText(`Exact content for ${first.source.content_ref}`)).toHaveCount(0);
    expect(await page.evaluate(original => original === document, originalDocument)).toBe(true);
    expect(exchanged).toEqual([firstToken, firstToken, firstToken, secondToken]);
    expect(unexpected).toEqual([]);
    for (const { path, headers } of requests) {
      expect(headers.cookie).toBeUndefined();
      expect(headers.authorization).toBeUndefined();
      if (path.endsWith('/exchange')) {
        expect(headers['x-cognis-feedback-capability']).toBeUndefined();
        expect(headers['x-cognis-feedback-guest']).toBeUndefined();
      } else {
        const value = capabilities.get(headers['x-cognis-feedback-capability'])!;
        expect(value).toBeDefined();
        if (value.share_id === second.share_id) expect(headers['x-cognis-feedback-guest']).toBeUndefined();
      }
    }
    expect(requests.some(({ path, headers }) => path.endsWith('/source')
      && headers['x-cognis-feedback-guest'] === guest.guest_capability)).toBe(true);
  });
}
