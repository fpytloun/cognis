import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  serviceWorkers: 'block',
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

test('conversation A to B to A reuses a fetched body without a loading frame', async ({ page }, testInfo) => {
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' },
  }));
  await page.routeWebSocket('**/api/ws', ws => {
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      if (JSON.parse(String(message)).type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  const report = {
    deliverable_id: 'dlv-reopen-report', format: 'markdown',
    content: '# Reopened conversation report\n\nReady before the first visible frame.',
    outputs: {}, render_metadata: {}, export_metadata: {}, validation_warnings: [],
    step_run_id: null, version: 1, attempt_number: 1, title: null, target: null,
    status: 'approved', evaluator_feedback: null, created_at: null, updated_at: null,
  };
  const reference = {
    id: 'deliverable-reopen-row', kind: 'assistant_deliverable',
    sort_key: '0001', source_refs: [], stable: true, status: 'complete',
    deliverable_id: report.deliverable_id, format: 'markdown',
  };
  // Keep the real conversation controller, registry, route transitions and auth
  // lifecycle. Only the server responses are synthetic.
  await installTaskCockpitFixture(page, { conversationTimelineItems: [reference] });
  let reads = 0;
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/deliverables/dlv-reopen-report*', async route => {
    reads++;
    await pending;
    await route.fulfill({ json: report });
  });
  await page.goto('/chat/conv-task-chat');
  const loading = page.getByRole('status').filter({ hasText: 'Loading deliverable…' });
  await expect(loading).toBeVisible();
  await expect(loading.locator('svg')).toHaveCSS('animation-name', 'spin');
  await expect(page.locator('textarea').first()).toBeEditable();
  await page.screenshot({ path: testInfo.outputPath('cold-loading.png') });
  release();
  await expect(page.getByRole('heading', { name: 'Reopened conversation report' })).toBeVisible();
  // Browser history uses SvelteKit's client-side route transition, not a reload
  // that would deliberately discard all in-memory state.
  await page.evaluate(() => {
    const link = document.createElement('a');
    link.href = '/chat/conv-task-chat-b';
    link.textContent = 'Switch conversation';
    document.body.append(link);
    link.click();
    link.remove();
  });
  await expect(page).toHaveURL(/conv-task-chat-b$/);
  await expect(page.locator('textarea').first()).toBeEditable();
  await expect(page.getByRole('heading', { name: 'Reopened conversation report' })).toHaveCount(0);
  await page.evaluate(() => {
    (window as typeof window & { reopenLoadingFrames?: number }).reopenLoadingFrames = 0;
    const observer = new MutationObserver(() => {
      if (location.pathname !== '/chat/conv-task-chat') return;
      if ([...document.querySelectorAll('[role="status"]')].some(node =>
        node.textContent?.includes('Loading deliverable…'))) {
        (window as typeof window & { reopenLoadingFrames: number }).reopenLoadingFrames++;
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  });
  await page.goBack();
  await expect(page).toHaveURL(/\/chat\/conv-task-chat$/);
  await expect(page.getByRole('heading', { name: 'Reopened conversation report' })).toBeVisible();
  expect(reads).toBe(1);
  expect(await page.evaluate(() =>
    (window as typeof window & { reopenLoadingFrames: number }).reopenLoadingFrames)).toBe(0);
  await page.screenshot({ path: testInfo.outputPath('warm-reopen.png') });
});
