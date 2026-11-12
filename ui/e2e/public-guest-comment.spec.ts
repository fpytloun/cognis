import { expect, test } from '@playwright/test';

// Compiled public viewer with synthetic API responses, not real-API acceptance.
for (const format of ['plain', 'rich'] as const) {
  for (const width of [1440, 390]) {
    test(`public ${format} comment icon offers guest registration at ${width}px`, async ({ page }, testInfo) => {
      await page.setViewportSize({ width, height: 900 });
      const errors: string[] = [];
      page.on('pageerror', error => errors.push(error.message));
      const source = {
        source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
        content_ref: `dlv_${crypto.randomUUID().replace(/-/g, '')}`,
        title: 'Shared review document', format, canonical_text: null,
      };
      const writes: string[] = [];
      await page.route(url => url.pathname.startsWith('/api/'), async route => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (request.method() !== 'GET') writes.push(path);
        if (path.endsWith('/public/feedback/exchange')) return route.fulfill({ json: {
          share_id: crypto.randomUUID(), capability: crypto.randomUUID(), expires_at: '2099-01-01T00:00:00Z',
          source, allow_comments: true, discussion: 'own_only', permitted_locator_kinds: ['whole'],
        } });
        if (path.endsWith('/public/feedback/source')) return route.fulfill({ json: {
          format, mime_type: format === 'rich' ? 'application/json' : 'text/plain',
          filename: 'Shared review document', content: 'Review this document before joining.',
          rich_payload: format === 'rich' ? { version: 1, blocks: [
            { type: 'markdown', content: '## Review scope\n\nReview this document before joining.' },
          ] } : null,
          bytes_path: null,
        } });
        if (path.endsWith('/public/feedback/guest-sessions')) return route.fulfill({ json: {
          guest_session_id: crypto.randomUUID(), guest_capability: crypto.randomUUID(), expires_at: '2099-01-01T00:00:00Z',
        } });
        if (path.endsWith('/public/feedback/threads')) return route.fulfill({ json: { items: [], next_cursor: null } });
        if (path.endsWith('/public/feedback/outcomes')) return route.fulfill({ json: { items: [] } });
        return route.fulfill({ status: 401, json: { detail: 'Not authenticated' } });
      });
      await page.goto(`/feedback#${crypto.randomUUID()}`);
      await expect(page.getByText('Review this document before joining.', { exact: true })).toBeVisible();
      const document = page.getByText('Review this document before joining.', { exact: true });
      const beforeSearch = await document.boundingBox();
      const toolbar = page.locator('[data-find-toolbar]').first();
      const toolbarHeight = (await toolbar.boundingBox())!.height;
      await page.getByRole('button', { name: 'Search in document', exact: true }).first().click();
      const find = page.getByRole('searchbox', { name: 'Find in document', exact: true });
      await expect(find).toBeFocused();
      expect((await document.boundingBox())!.y).toBe(beforeSearch!.y);
      await expect(toolbar.getByRole('searchbox')).toBeFocused();
      expect((await toolbar.boundingBox())!.height).toBe(toolbarHeight);
      await find.fill('Review this document before joining.');
      await expect(page.getByRole('search', { name: 'Find in document' })).toContainText('1 of 1');
      await find.press('Escape');
      await expect(find).toHaveCount(0);
      if (width === 390 || format === 'rich') {
        const more = page.getByLabel(format === 'rich' ? 'More document actions' : 'More actions', { exact: true });
        await more.click();
        await page.locator('details[open]').getByRole('button', { name: 'Search in document', exact: true }).click();
        await expect(find).toBeFocused();
        await find.press('Escape');
        await expect(more).toBeFocused();
      }
      const comment = page.locator('.comment-toggle');
      await expect(comment).toBeEnabled();
      await expect(comment).toHaveAttribute('aria-pressed', 'false');
      await comment.click();
      const name = page.getByRole('textbox', { name: 'Display name (optional)' });
      await expect(name).toBeVisible();
      const join = page.getByRole('button', { name: 'Join as guest' });
      await expect(join).toBeVisible();
      await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toHaveCount(0);
      expect(writes.filter(path => !path.endsWith('/exchange'))).toEqual([]);
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      const box = await join.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.y + box!.height).toBeLessThanOrEqual(900);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      await page.screenshot({ path: testInfo.outputPath('guest-join.png') });
      await name.fill('Reviewer');
      await join.click();
      await expect(join).toHaveCount(0);
      await page.getByRole('button', { name: 'Close comments', exact: true }).click();
      await comment.click();
      await expect(page.getByRole('button', { name: 'Hide comments', exact: true })).toHaveAttribute('aria-pressed', 'true');
      expect(writes.filter(path => !path.endsWith('/exchange'))).toEqual(['/api/v1/public/feedback/guest-sessions']);
      expect(errors).toEqual([]);
    });
  }
}
