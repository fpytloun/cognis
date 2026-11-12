import { randomUUID } from 'node:crypto';
import { expect, test, type APIResponse } from '@playwright/test';
import { login } from './helpers';

// Real API acceptance against the seeded local stack, not an intercepted fixture.
test('document badges load before interaction and follow resolve/reopen', async ({ page }) => {
  await login(page);
  async function body(response: APIResponse) {
    expect(response.ok(), `HTTP ${response.status()}: ${await response.text()}`).toBe(true);
    return response.json();
  }
  const artifact = await body(await page.request.post('/api/v1/library/artifacts', { data: {
    filename: 'comment-count-acceptance.md', mime_type: 'text/markdown',
    content: '# Document review\n\nThe comment badge shows pending comments before the review panel is opened.\n',
  } }));
  const context = { kind: 'standalone' };
  const source = await body(await page.request.post('/api/v1/feedback/sources/resolve', { data: {
    source: { kind: 'document', content_ref: artifact.id }, context,
  } }));
  for (const text of ['Check the first paragraph.', 'Keep the explanation concise.']) {
    await body(await page.request.post('/api/v1/feedback/threads', { data: {
      creation_key: randomUUID(), context, audience: 'internal', body: text,
      anchor: { schema_version: 1, source_token: source.source_token,
        content_digest: source.content_digest, locator: { kind: 'whole' } },
    } }));
  }
  const revision = process.env.COGNIS_EVIDENCE_REVISION ?? 'worktree';
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`/library?lib_item=${artifact.id}&lib_item_kind=artifact`);
    const reader = page.getByTestId('resource-reader');
    await expect(reader.getByRole('heading', { name: 'Document review', exact: true })).toBeVisible();
    const badge = reader.locator('.comment-toggle');
    await expect(badge).toHaveText('2');
    await expect(page.getByRole('region', { name: 'Feedback panel' })).toHaveCount(0);
    await expect(reader.getByText(/Loading|Saving/)).toHaveCount(0);
    const rect = await badge.boundingBox();
    expect(rect).not.toBeNull();
    expect(rect!.x).toBeGreaterThanOrEqual(0);
    expect(rect!.x + rect!.width).toBeLessThanOrEqual(width);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await page.screenshot({ path: test.info().outputPath(`comment-count-${revision}-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  const reader = page.getByTestId('resource-reader');
  const badge = reader.locator('.comment-toggle');
  await badge.click();
  const panel = page.getByRole('region', { name: 'Feedback panel' });
  const first = panel.getByRole('article', { name: 'Open comment', exact: true })
    .filter({ hasText: 'Check the first paragraph.' });
  await expect(first).toBeVisible();
  await first.getByRole('button', { name: 'Resolve', exact: true }).click();
  await expect(badge).toHaveText('1');
  // Source revalidation can dismiss the presentation; reopen through the real control.
  if (!await panel.isVisible()) await badge.click();
  await panel.getByRole('button', { name: 'Resolved 1', exact: true }).click();
  await panel.getByRole('button', { name: 'Reopen', exact: true }).click();
  await expect(badge).toHaveText('2');
  await page.reload();
  await expect(page.getByTestId('resource-reader').locator('.comment-toggle')).toHaveText('2');
});
