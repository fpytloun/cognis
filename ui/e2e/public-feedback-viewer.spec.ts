import { expect, test } from '@playwright/test';

test('public mobile reader scrolls, switches appearance, hides comments, and offers audio/downloads', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const token = crypto.randomUUID();
  const source = {
    source_token: crypto.randomUUID(), content_digest: 'a'.repeat(64),
    content_ref: `dlv_${crypto.randomUUID().replace(/-/g, '')}`, title: 'Mobile reader',
    format: 'rich', canonical_text: null,
  };
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/public/feedback/exchange')) return route.fulfill({ json: {
      share_id: crypto.randomUUID(), capability: crypto.randomUUID(), expires_at: '2099-01-01T00:00:00Z',
      source, allow_comments: false, discussion: 'own_only', permitted_locator_kinds: ['whole'],
    } });
    if (path.endsWith('/public/feedback/source')) return route.fulfill({ json: {
      format: 'rich', mime_type: 'application/json', filename: 'Mobile reader', content: 'Long document',
      rich_payload: { version: 1, blocks: [{ type: 'markdown', content: '## Public reading\n\n' + 'Readable text on mobile.\n\n'.repeat(160) }] },
      bytes_path: null,
    } });
    return route.fulfill({ status: 401, json: { detail: 'Not authenticated' } });
  });
  await page.goto(`/feedback#${token}`);
  const reader = page.locator('.rich-deliverable');
  await expect(reader).toBeVisible();
  await expect(page.getByRole('button', { name: 'Comment options' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Listen to this document' })).toBeVisible();
  await page.getByLabel('Download document').click();
  for (const name of ['PDF', 'Single HTML', 'Markdown']) {
    await expect(page.getByRole('button', { name, exact: true })).toBeVisible();
  }
  await page.keyboard.press('Escape');
  const theme = await reader.getAttribute('data-viewer-theme');
  if (!theme) throw new Error('Reader theme was not resolved');
  const toggle = page.getByRole('button', { name: /Switch to .* theme/ });
  await toggle.click();
  if (await reader.getAttribute('data-viewer-theme') === theme) await toggle.click();
  await expect(reader).not.toHaveAttribute('data-viewer-theme', theme);
  const headerColor = await page.getByTestId('rich-viewer-chrome').evaluate(e => getComputedStyle(e).backgroundColor);
  await expect.poll(() => page.evaluate(() => document.querySelector('meta[name="theme-color"]')?.getAttribute('content')))
    .toBe(headerColor);
  await page.mouse.move(200, 600);
  await page.mouse.wheel(0, 700);
  await expect.poll(() => page.locator('main').evaluate(e => e.scrollTop)).toBeGreaterThan(100);
  await page.locator('main').evaluate(e => e.scrollTo(0, e.scrollHeight));
  await expect.poll(() => page.locator('main').evaluate(e => e.scrollHeight - e.clientHeight - e.scrollTop)).toBeLessThan(2);
});
