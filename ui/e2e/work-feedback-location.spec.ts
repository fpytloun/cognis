import { expect, test } from '@playwright/test';

test('saved canonical Markdown anchors locate their rendered table cell and title', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/e2e/fixtures/table-range/index.html');
  await expect(page.getByRole('cell', { name: 'Zulu source text', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Locate saved cell', exact: true }).click();
  await expect(page.getByLabel('Saved anchor location')).toHaveText('exact');
  await page.getByRole('button', { name: 'Locate saved title', exact: true }).click();
  await expect(page.getByLabel('Saved anchor location')).toHaveText('exact');
  expect(errors).toEqual([]);
  await page.screenshot({ path: info.outputPath('saved-comment-location.png') });
});

for (const width of [390, 1280]) {
  test(`agent answers show the original comment and source at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [], unmocked: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(url => url.pathname.startsWith('/api/'), async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/auth/me') return route.fulfill({ json: {
        email: 'reviewer@cognis.test', name: 'Reviewer', role: 'user', is_active: true,
      } });
      if (path === '/api/v1/feedback/sources/resolve' && route.request().method() === 'POST') {
        return route.fulfill({ json: {
          subject_id: 'table-fixture-subject', source_token: 'table-fixture-token',
          source: { kind: 'document', content_ref: 'table-fixture-document' },
          content_digest: 'a'.repeat(64), availability: 'available', permitted_locator_kinds: ['rich'],
          context: { kind: 'chat', conversation_id: 'table-fixture-conversation' },
          lineage_id: null, revision: 1, latest_content_ref: null,
          capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false },
          title: 'Release data', format: 'rich', canonical_text: null,
        } });
      }
      if (path === '/api/v1/feedback/subjects/table-fixture-subject/threads' && route.request().method() === 'GET') {
        return route.fulfill({ json: { items: [], next_cursor: null } });
      }
      if (path.startsWith('/api/v1/feedback/drafts/') && route.request().method() === 'GET') {
        return route.fulfill({ status: 404, json: { detail: 'No saved draft' } });
      }
      if (path === '/api/v1/feedback/threads/table-fixture-thread' && route.request().method() === 'GET') {
        return route.fulfill({ json: {
          thread_id: 'table-fixture-thread', subject_id: 'table-fixture-subject', version: 1,
          state: 'open', hidden: false, audience: 'internal',
          anchor: { schema_version: 1, anchor_id: 'table-fixture-anchor',
            source: { kind: 'document', content_ref: 'table-fixture-document' }, content_digest: 'a'.repeat(64),
            locator: { kind: 'rich', block_id: 'release-table', identity: 'semantic', item_key: null,
              field_path: ['rows', '0', '0'], range: null } },
          entries: [{ entry_id: 'table-fixture-entry', entry_version_id: 'table-fixture-entry-version', version: 1,
            thread_id: 'table-fixture-thread', body: 'Who owns these release checks, and do they block rollout?',
            author: { kind: 'verified_user', label: 'Reviewer', is_self: true }, audience: 'internal',
            reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [] }],
        } });
      }
      unmocked.push(`${route.request().method()} ${path}`);
      return route.fulfill({ status: 404, json: { detail: 'Not part of the comment fixture' } });
    });
    await page.goto('/e2e/fixtures/table-range/index.html?reply');
    await expect(page.getByRole('button', { name: 'Locate comment: Who owns these release checks, and do they block rollout?' })).toBeVisible();
    await expect(page.getByText('The rollout remains blocked', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open original revision' })).toBeVisible();
    await expect(page.getByText('Loading original comment…')).toHaveCount(0);
    await page.getByRole('button', { name: 'Locate comment: Who owns these release checks, and do they block rollout?' }).click();
    const highlight = page.locator('[data-feedback-target-highlight]').first();
    await expect(highlight).toBeVisible();
    const cell = await page.locator('tbody tr').first().locator('td').first().boundingBox();
    await expect.poll(async () => {
      const box = await highlight.boundingBox();
      return !!box && !!cell && Math.abs(box.x - cell.x) < 2 && Math.abs(box.y - cell.y) < 2
        && Math.abs(box.width - cell.width) < 2 && Math.abs(box.height - cell.height) < 2;
    }).toBe(true);
    await expect(page.locator('main')).not.toHaveAttribute('data-feedback-located', '');
    await expect(page.getByText('The annotation is not available in this view.', { exact: false })).toHaveCount(0);
    expect(errors).toEqual([]);
    expect(unmocked).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`agent-comment-context-${width}.png`), fullPage: true });
  });
}
