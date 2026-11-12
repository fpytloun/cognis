import { expect, test } from '@playwright/test';

type RecoveryWindow = typeof window & {
  historyFixture: {
    snapshots: number;
    admissionRequests: number;
    releaseSnapshot: (() => void) | null;
    resolveFailedAdmission: (() => void) | null;
    cachedRecoveryIds: () => Promise<string[]>;
    unexpectedRequests: string[];
  };
};

test.beforeEach(async ({ page }) => {
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin !== 'http://127.0.0.1:4193' || url.pathname.startsWith('/api/')
      ? route.abort() : route.continue();
  });
});

for (const width of [390, 1280]) {
  test(`explicit refresh rebuilds history, keeps input and persists recovery (${width}px)`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await page.goto('/history.html?full-only&failed-admission&force-refresh&hold-snapshot');
    const timeline = page.getByTestId('timeline-viewport');
    await expect(timeline.getByText('Cached-only activity', { exact: true })).toBeVisible();
    await expect.poll(() => page.evaluate(() => (window as RecoveryWindow).historyFixture.admissionRequests)).toBe(1);
    await page.evaluate(() => (window as RecoveryWindow).historyFixture.resolveFailedAdmission?.());
    await expect(timeline.locator('article').filter({ hasText: 'Retained failed input' })
      .getByText('failed', { exact: true })).toBeVisible();
    const panel = page.getByTestId('timeline-diagnostic-export');
    const button = panel.getByRole('button', { name: 'Force refresh' });
    await expect(panel.getByRole('button', { name: 'Download chat diagnostics' })).toBeEnabled();
    const bounds = await button.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.height).toBeGreaterThanOrEqual(44);
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    await button.click();
    await expect(panel.getByRole('button', { name: 'Refreshing…' })).toBeDisabled();
    await expect(timeline.getByText('Cached-only activity', { exact: true })).toBeVisible();
    await page.evaluate(() => (window as RecoveryWindow).historyFixture.releaseSnapshot?.());
    await expect(panel.getByRole('status')).toHaveText('History refreshed.');
    await expect(timeline.getByText('Cached-only activity', { exact: true })).toHaveCount(0);
    await expect(timeline.getByText('Retained failed input', { exact: true })).toHaveCount(1);
    await expect(timeline.getByText('Newer canonical response', { exact: true })).toBeVisible();
    const cachedIds = await page.evaluate(() => (window as RecoveryWindow).historyFixture.cachedRecoveryIds());
    expect(cachedIds).toContain('user:failed-input');
    expect(cachedIds).not.toContain('message:cached-only');
    expect(await page.evaluate(() => (window as RecoveryWindow).historyFixture.snapshots)).toBe(1);
    expect(await page.evaluate(() => (window as RecoveryWindow).historyFixture.unexpectedRequests)).toEqual([]);
    await panel.screenshot({ path: testInfo.outputPath(`force-refresh-${width}.png`) });
  });
}

test('failed refresh leaves cached history and recoverable input intact', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/history.html?full-only&failed-admission&force-refresh&refresh-error');
  const timeline = page.getByTestId('timeline-viewport');
  await expect(timeline.getByText('Cached-only activity', { exact: true })).toBeVisible();
  const panel = page.getByTestId('timeline-diagnostic-export');
  await panel.getByRole('button', { name: 'Force refresh' }).click();
  await expect(panel.getByRole('alert')).toHaveText('Refresh failed. Your input has been kept. Try again.');
  await expect(timeline.getByText('Cached-only activity', { exact: true })).toBeVisible();
  await expect(timeline.getByText('Retained failed input', { exact: true })).toHaveCount(1);
  await expect(panel.getByRole('button', { name: 'Force refresh' })).toBeEnabled();
  expect(await page.evaluate(() => (window as RecoveryWindow).historyFixture.unexpectedRequests)).toEqual([]);
});

test('narrow refresh and reopen do not replay old confirmed or processed inputs', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/history.html?full-only&force-refresh&failed-admission&old-input-echoes');
  const timeline = page.getByTestId('timeline-viewport');
  const panel = page.getByTestId('timeline-diagnostic-export');
  await expect(panel).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as RecoveryWindow).historyFixture.admissionRequests))
    .toBeGreaterThanOrEqual(3);
  await expect(timeline.getByText('Previously confirmed input', { exact: true })).toBeVisible();
  await page.evaluate(() => (window as RecoveryWindow).historyFixture.resolveFailedAdmission?.());
  await expect(timeline.getByText('Older processed input', { exact: true })).toHaveCount(0);
  await expect(timeline.getByText('Unconfirmed recoverable input', { exact: true })).toBeVisible();
  await panel.getByRole('button', { name: 'Force refresh', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('History refreshed.');
  await expect(timeline.getByText('Previously confirmed input', { exact: true })).toHaveCount(0);
  await expect(timeline.getByText('Older processed input', { exact: true })).toHaveCount(0);
  await expect(timeline.getByText('Unconfirmed recoverable input', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as RecoveryWindow).historyFixture.cachedRecoveryIds()))
    .toEqual(expect.arrayContaining(['user:confirmed-input', 'user:legacy-input', 'user:unconfirmed-input']));
  await page.reload();
  await expect(timeline.getByText('Unconfirmed recoverable input', { exact: true })).toBeVisible();
  await expect(timeline.getByText('Previously confirmed input', { exact: true })).toHaveCount(0);
  await expect(timeline.getByText('Older processed input', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => (window as RecoveryWindow).historyFixture.unexpectedRequests)).toEqual([]);
  await page.screenshot({ path: info.outputPath('old-input-refresh-reopen.png') });
});
