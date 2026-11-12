import { expect, test } from '@playwright/test';

for (const kind of ['pdf', 'jpx-pdf']) {
  test(`web ${kind} renders actual pixels with an owned blob worker`, async ({ page }) => {
    await page.goto('/');
    await page.waitForFunction(() => 'artifactPreviewFixture' in window);
    await page.evaluate(async kind => {
      const fixture = (window as unknown as { artifactPreviewFixture: { open(kind: string): Promise<boolean> } }).artifactPreviewFixture;
      await fixture.open(kind);
    }, kind);
    await expect(page.getByText('Page 1 / 2')).toBeVisible();
    await page.waitForFunction(() => {
      const canvas = document.querySelector('canvas');
      if (!canvas?.width) return false;
      const pixel = canvas.getContext('2d')!.getImageData(
        Math.floor(canvas.width / 6), Math.floor(canvas.height * 5 / 6), 1, 1,
      ).data;
      return pixel[0] > 200 && pixel[1] < 50;
    });
    await page.getByRole('button', { name: 'Next', exact: true }).click();
    await expect(page.getByText('Page 2 / 2')).toBeVisible();
    await page.evaluate(async () => {
      const fixture = (window as unknown as { artifactPreviewFixture: { close(): Promise<boolean> } }).artifactPreviewFixture;
      await fixture.close();
    });
    await page.waitForFunction(() => (window as unknown as { artifactPreviewFixture: { activeWorkers: number } }).artifactPreviewFixture.activeWorkers === 0);
  });
}

test('PDF zoom fits the full page initially and preserves actual sizes across resize', async ({ page }) => {
  await page.goto('/');
  await page.waitForFunction(() => 'artifactPreviewFixture' in window);
  await page.evaluate(async () => {
    await (window as unknown as { artifactPreviewFixture: { open(kind: string): Promise<boolean> } }).artifactPreviewFixture.open('a4-pdf');
  });
  const canvas = page.locator('canvas');
  const percent = page.getByRole('spinbutton', { name: 'Zoom percentage' });
  await expect(page.getByText('Page 1 / 2')).toBeVisible();
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().height)).toBeGreaterThan(100);
  const bounds = await canvas.evaluate(c => {
    const page = c.getBoundingClientRect();
    const viewport = c.parentElement!.getBoundingClientRect();
    return { width: page.width, height: page.height, viewportWidth: viewport.width, viewportHeight: viewport.height };
  });
  expect(bounds.width).toBeLessThanOrEqual(bounds.viewportWidth);
  expect(bounds.height).toBeLessThanOrEqual(bounds.viewportHeight);
  expect(Number(await percent.inputValue())).toBeLessThanOrEqual(100);
  await page.getByRole('button', { name: 'Actual size (100%)' }).click();
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().width)).toBeCloseTo(595 * 96 / 72, 0);
  await page.getByRole('button', { name: 'Zoom out' }).click();
  await expect(percent).toHaveValue('75');
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().width)).toBeCloseTo(595, 0);
  await percent.fill('25');
  await percent.press('Enter');
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().width)).toBeCloseTo(595 / 3, 0);
  await page.evaluate(() => { document.getElementById('fixture')!.style.width = '400px'; });
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(page.getByText('Page 2 / 2')).toBeVisible();
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().width)).toBeCloseTo(595 / 3, 0);
  await page.getByRole('button', { name: 'Fit width' }).click();
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().width)).toBeGreaterThan(350);
  await page.getByRole('button', { name: 'Actual size (100%)' }).click();
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().width)).toBeCloseTo(595 * 96 / 72, 0);
  await expect.poll(async () => canvas.evaluate(c => c.parentElement!.scrollWidth > c.parentElement!.clientWidth)).toBe(true);
  await page.getByRole('button', { name: 'Fit page' }).click();
  await expect.poll(async () => canvas.evaluate(c => c.getBoundingClientRect().height <= c.parentElement!.clientHeight)).toBe(true);
});
