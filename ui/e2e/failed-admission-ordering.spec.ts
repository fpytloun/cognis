import { expect, test } from '@playwright/test';

type AdmissionWindow = typeof window & {
  historyFixture: {
    resolveFailedAdmission: (() => void) | null;
    admissionRequests: number;
    unexpectedRequests: string[];
  };
};

test('a failed accepted input is retained, labelled and bounded by its canonical successor', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== 'http://127.0.0.1:4193' || url.pathname.startsWith('/api/')) return route.abort();
    return route.continue();
  });
  await page.goto('/history.html?full-only&failed-admission');
  const timeline = page.getByTestId('timeline-viewport');
  await expect(timeline.getByText('Retained failed input', { exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as AdmissionWindow).historyFixture.admissionRequests)).toBe(1);
  await page.evaluate(() => (window as AdmissionWindow).historyFixture.resolveFailedAdmission?.());
  const retained = timeline.locator('article').filter({ hasText: 'Retained failed input' });
  await expect(retained.getByText('failed', { exact: true })).toBeVisible();
  await expect(timeline.getByText('Retained failed input', { exact: true })).toHaveCount(1);
  const rows = await timeline.locator('[data-timeline-row-key]').allTextContents();
  expect(rows.findIndex(text => text.includes('Retained failed input')))
    .toBeLessThan(rows.findIndex(text => text.includes('Later accepted input')));
  expect(rows.findIndex(text => text.includes('Later accepted input')))
    .toBeLessThan(rows.findIndex(text => text.includes('Newer canonical response')));
  expect(await page.evaluate(() => (window as AdmissionWindow).historyFixture.unexpectedRequests)).toEqual([]);
});

test('queued failures and stops recover in order in shared full and scoped histories', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin !== 'http://127.0.0.1:4193' || url.pathname.startsWith('/api/')
      ? route.abort() : route.continue();
  });
  await page.goto('/history.html?queued-admission');
  const surfaces = [page.getByTestId('timeline-viewport'), page.getByTestId('scoped-timeline-viewport')];
  for (const surface of surfaces) await expect(surface).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as AdmissionWindow).historyFixture.admissionRequests)).toBe(4);
  await page.evaluate(() => (window as AdmissionWindow).historyFixture.resolveFailedAdmission?.());
  const inputs = ['Retained failed input', 'Retained queued input 1', 'Retained queued input 2', 'Retained queued input 3'];
  for (const surface of surfaces) {
    const positions: number[] = [];
    for (const [index, content] of inputs.entries()) {
      const text = surface.getByText(content, { exact: true });
      await expect(text).toHaveCount(1);
      const article = surface.locator('article').filter({ hasText: content });
      await expect(article.getByText(index % 2 ? 'cancelled' : 'failed', { exact: true })).toBeVisible();
      positions.push(await text.evaluate(element => element.getBoundingClientRect().top));
    }
    positions.push(await surface.getByText('Later accepted input', { exact: true })
      .evaluate(element => element.getBoundingClientRect().top));
    for (let index = 1; index < positions.length; index++) expect(positions[index - 1]).toBeLessThan(positions[index]);
    await expect(surface.getByText('Newer canonical response', { exact: true })).toBeVisible();
  }
  expect(await page.evaluate(() => (window as AdmissionWindow).historyFixture.admissionRequests)).toBe(4);
  expect(await page.evaluate(() => (window as AdmissionWindow).historyFixture.unexpectedRequests)).toEqual([]);
});
