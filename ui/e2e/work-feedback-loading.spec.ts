import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({ status: 404, json: { detail: 'Fixture endpoint is unavailable.' } }));
  await page.route('**/api/auth/me', route => route.fulfill({ json: {
    email: 'loading-review@example.test', display_name: 'Loading reviewer', is_admin: false, is_active: true,
  } }));
  await page.route('**/api/v1/feedback/**', route => route.fulfill({ json: {
    conversation_id: 'loading-browser-conversation', version: 1, items: [], receipts: [],
  } }));
  await page.goto('/work-feedback-loading-fixture');
  await expect(page.getByRole('button', { name: 'Open code', exact: true })).toBeVisible();
});
test('cold modal Escape cancels download completion and warm modal reopens without a loading frame', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/src/lib/components/CodeBlockModal.svelte*', async route => { await held; await route.continue(); });
  await page.getByRole('button', { name: 'Open code', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Loading code preview' })).toBeVisible();
  await page.keyboard.press('Escape');
  release();
  await expect(page.getByRole('dialog', { name: 'code preview' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Open code', exact: true }).click();
  await expect(page.getByText('const loaded = true;', { exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Open code', exact: true }).click();
  await expect(page.getByText('const loaded = true;', { exact: true })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Loading code preview' })).toHaveCount(0);
});
test('cold image completion cannot mount after account invalidation', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/src/lib/components/ImageLightbox.svelte*', async route => { await held; await route.continue(); });
  await page.getByRole('button', { name: 'Open image', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Loading image preview' })).toBeVisible();
  // Simulate an external account transition while the modal owns pointer input.
  await page.getByRole('button', { name: 'Invalidate account' }).evaluate(button => (button as HTMLButtonElement).click());
  release();
  await expect.poll(async () => (await page.getByRole('alert').count()) > 0
    || (await page.getByRole('dialog', { name: 'image preview' }).count()) === 0).toBeTruthy();
  await expect(page.getByRole('img', { name: 'Loaded image' })).toHaveCount(0);
});
test('cold review is fenced by source context and diff/audio mounts never synthesize playback', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/src/lib/components/feedback/FeedbackReview.svelte*', async route => { await held; await route.continue(); });
  await page.getByRole('button', { name: 'Open review', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Loading feedback controls' })).toBeVisible();
  await page.getByRole('button', { name: 'Switch source' }).click();
  release();
  await expect(page.getByRole('alert')).toContainText('source changed');
  await page.getByRole('button', { name: 'Open diff', exact: true }).click();
  await expect(page.getByText('src/loading.ts', { exact: true })).toBeVisible();
  await page.evaluate(() => {
    (window as Window & { loadingPlayCalls?: number }).loadingPlayCalls = 0;
    HTMLMediaElement.prototype.play = async () => { (window as Window & { loadingPlayCalls?: number }).loadingPlayCalls!++; };
  });
  await page.getByRole('button', { name: 'Open audio', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Listen to this document' })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Loading audio controls' })).toHaveCount(0);
  expect(await page.evaluate(() => (window as Window & { loadingPlayCalls?: number }).loadingPlayCalls)).toBe(0);
});
test('attention history and Escape are eager during download, and warm reopen skips module loading', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const actions: string[] = [];
  await page.route('**/api/v1/attention-actions/**', async route => {
    actions.push(new URL(route.request().url()).pathname);
    await route.fulfill({ status: 503, json: { detail: 'Attention fixture unavailable.' } });
  });
  await page.route('**/src/lib/components/attention/FocusedAttentionAction.svelte*', async route => { await held; await route.continue(); });
  await page.getByRole('button', { name: 'Open attention', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Fixture attention' })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Loading attention action' })).toBeVisible();
  expect(await page.evaluate(() => window.history.state.cognisAttentionActionId)).toBe('loading-attention');
  await expect(page.getByRole('button', { name: 'Close Fixture attention' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Fixture attention' })).toHaveCount(0);
  expect(actions).toEqual([]);
  release();
  await page.getByRole('button', { name: 'Open attention', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Attention fixture unavailable.');
  await page.getByRole('button', { name: 'Close Fixture attention' }).click();
  await expect(page.getByRole('dialog', { name: 'Fixture attention' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Open attention', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Loading attention action' })).toHaveCount(0);
  await expect(page.getByLabel('Attention callbacks')).toHaveText('0/0');
});
test('attention cold action replacement loads only the current action without replacing its history host', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const actions: string[] = [];
  await page.route('**/api/v1/attention-actions/**', async route => {
    actions.push(new URL(route.request().url()).pathname);
    await route.fulfill({ status: 503, json: { detail: 'Attention fixture unavailable.' } });
  });
  await page.route('**/src/lib/components/attention/FocusedAttentionAction.svelte*', async route => { await held; await route.continue(); });
  await page.getByRole('button', { name: 'Open attention', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Loading attention action' })).toBeVisible();
  const token = await page.evaluate(() => window.history.state.cognisAttentionHistoryToken);
  await page.getByRole('button', { name: 'Switch action' }).evaluate(button => (button as HTMLButtonElement).click());
  release();
  await expect(page.getByRole('alert')).toContainText('Attention fixture unavailable.');
  expect(actions).toEqual(['/api/v1/attention-actions/changed-loading-attention']);
  expect(await page.evaluate(() => window.history.state.cognisAttentionHistoryToken)).toBe(token);
  await expect(page.getByLabel('Attention callbacks')).toHaveText('0/0');
});
test('attention module errors stay in the eager host with retry and close, never action settlement', async ({ page }) => {
  await page.route('**/src/lib/components/attention/FocusedAttentionAction.svelte*', route => route.abort('failed'));
  await page.getByRole('button', { name: 'Open attention', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry attention action' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Close Fixture attention' })).toBeVisible();
  const token = await page.evaluate(() => window.history.state.cognisAttentionHistoryToken);
  await page.getByRole('button', { name: 'Retry attention action' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  expect(await page.evaluate(() => window.history.state.cognisAttentionHistoryToken)).toBe(token);
  await expect(page.getByLabel('Attention callbacks')).toHaveText('0/0');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Fixture attention' })).toHaveCount(0);
});
test('attention Back and beforeNavigate unwind the eager history entry while content is pending', async ({ page }) => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/src/lib/components/attention/FocusedAttentionAction.svelte*', async route => { await held; await route.continue(); });
  await page.getByRole('button', { name: 'Open attention', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Loading attention action' })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole('dialog', { name: 'Fixture attention' })).toHaveCount(0);
  await page.getByRole('button', { name: 'Open attention', exact: true }).click();
  await page.getByRole('link', { name: 'Navigate fixture' }).evaluate(link => (link as HTMLAnchorElement).click());
  await expect(page).toHaveURL(/attention=next$/);
  await expect(page.getByRole('dialog', { name: 'Fixture attention' })).toHaveCount(0);
  release();
  await expect(page.getByLabel('Attention callbacks')).toHaveText('0/0');
});
