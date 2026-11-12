import { expect, test } from '@playwright/test';

type CarriedWindow = typeof window & {
  historyFixture: { advanceCarried(): void; confirmCarried(): void; unexpectedRequests: string[] };
};

test('interrupted assistant stays before the next turn and yields to its canonical phase', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4193' || url.pathname.startsWith('/api/')) return route.abort();
    return route.continue();
  });
  await page.goto('/history.html?full-only&carried-response');
  const timeline = page.getByTestId('timeline-viewport');
  const retained = timeline.getByText('Retained interrupted response.', { exact: true });
  await expect(retained).toBeVisible();
  await page.evaluate(() => (window as CarriedWindow).historyFixture.advanceCarried());
  await expect(timeline.getByText('Newer canonical response', { exact: true })).toBeVisible();
  await expect(timeline.getByText('Current live progress', { exact: true })).toBeVisible();
  await expect(retained).toHaveCount(1);
  const before = await timeline.locator('[data-timeline-row-key]').allTextContents();
  const oldIndex = before.findIndex(text => text.includes('Retained interrupted response.'));
  const newIndex = before.findIndex(text => text.includes('Newer canonical response'));
  expect(oldIndex).toBeGreaterThanOrEqual(0);
  expect(oldIndex).toBeLessThan(newIndex);
  await expect(timeline.locator('article').filter({ hasText: 'Retained interrupted response.' })
    .getByText('partial', { exact: true })).toBeVisible();

  await page.evaluate(() => (window as CarriedWindow).historyFixture.confirmCarried());
  await expect(retained).toHaveCount(1);
  const after = await timeline.locator('[data-timeline-row-key]').allTextContents();
  expect(after.findIndex(text => text.includes('Retained interrupted response.')))
    .toBeLessThan(after.findIndex(text => text.includes('Newer canonical response')));
  await expect(timeline.getByText('Current live progress', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as CarriedWindow).historyFixture.unexpectedRequests)).toEqual([]);
});
