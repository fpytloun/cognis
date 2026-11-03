import { readFileSync } from 'node:fs';
import { resolve, basename } from 'node:path';
import { expect, test } from '@playwright/test';
import { selectViewerTheme } from './viewer-theme';

const build = resolve('standalone-build');
const manifest = JSON.parse(readFileSync(resolve(build, '.vite/manifest.json'), 'utf8'));
const entry = manifest['src/standalone.ts'];
for (const scenario of ['design-document-composed', 'design-document-legacy', 'compact-alert-dashboard', 'daily-pulse-v2']) {
const fixture = JSON.parse(readFileSync(resolve(`src/lib/rich-scenarios/scenarios/${scenario}.json`), 'utf8'));
for (const width of [390, 1440]) {
  test(`real standalone ${scenario} continuous canvas and styling at ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route('**/api/v1/deliverables/standalone-assets/assets/*', async route => {
      const file = basename(new URL(route.request().url()).pathname);
      await route.fulfill({
        body: readFileSync(resolve(build, 'assets', file)),
        contentType: file.endsWith('.css') ? 'text/css' : file.endsWith('.woff2') ? 'font/woff2' : 'text/javascript',
      });
    });
    await page.route('**/rich-canvas-smoke', route => route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
        ${entry.css.map((css: string) => `<link rel="stylesheet" href="/api/v1/deliverables/standalone-assets/${css}">`).join('')}
        </head><body><div id="cognis-deliverable-root"></div>
        <template id="cognis-deliverable-payload">${JSON.stringify({ ...fixture, instanceId: fixture.id }).replaceAll('<', '\\u003c')}</template>
        <script type="module" src="/api/v1/deliverables/standalone-assets/${entry.file}"></script></body></html>`,
    }));
    await page.goto('/rich-canvas-smoke');
    const viewer = page.getByTestId('rich-deliverable');
    const chrome = page.getByTestId('rich-viewer-chrome');
    for (const theme of ['light', 'dark'] as const) {
      await page.evaluate(() => window.scrollTo(0, 0));
      await selectViewerTheme(viewer, theme);
      await expect(viewer).toHaveAttribute('data-viewer-theme', theme);
      const box = await viewer.boundingBox();
      expect(box!.x).toBe(0);
      expect(box!.y).toBe(0);
      expect(box!.width).toBe(width);
      expect(box!.height).toBeGreaterThanOrEqual(900);
      await expect(viewer).toHaveCSS('border-radius', '0px');
      await expect(page.locator('html')).toHaveCSS('background-color', await viewer.evaluate(el => getComputedStyle(el).backgroundColor));
      await expect(page.locator('html')).toHaveCSS('color-scheme', theme);
      expect(await viewer.evaluate(el => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
      const body = await viewer.locator('.rich-body').boundingBox();
      if (width === 390) {
        expect(body!.x).toBe(16);
        expect(body!.width).toBe(358);
      }
      for (const heading of await viewer.locator('h1,h2,h3,h4').all()) {
        expect(await heading.evaluate(el => getComputedStyle(el).fontFamily)).toContain('Archivo');
      }
      for (const hero of await viewer.locator('.rich-hero:not(.has-media)').all()) {
        await expect(hero).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
        await expect(hero).toHaveCSS('border-top-width', '0px');
      }
      if (scenario === 'compact-alert-dashboard') {
        await expect(viewer.locator('.rich-lede')).toContainText('Invented exercise after two simulator resets');
        await expect(viewer.locator('.rich-section-header h3').first()).toHaveCSS('text-transform', 'none');
        for (const metric of await viewer.locator('.rich-metric').all()) {
          expect((await metric.boundingBox())!.height).toBeLessThan(130);
          await expect(metric).toHaveCSS('box-shadow', 'none');
        }
        if (width === 390) {
          const metrics = await viewer.locator('.rich-metric').all();
          expect((await metrics[0].boundingBox())!.y).toBe((await metrics[1].boundingBox())!.y);
        }
      }
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `/tmp/rich-${scenario}-${theme}-${width}.png`, fullPage: true });
      await page.evaluate(() => window.scrollTo(0, 600));
      await expect.poll(async () => (await chrome.boundingBox())!.y).toBeLessThan(-400);
    }
  });
}
}

test('full view fills viewport and toolbar scrolls away without changing embedded frame', async ({ page }) => {
  await page.goto('/rich-deliverable-fixture?scenario=design-document-composed&surface=embedded');
  const embedded = page.getByTestId('rich-deliverable');
  const border = await embedded.evaluate(el => getComputedStyle(el).borderTopWidth);
  await embedded.getByRole('button', { name: 'Open full view' }).click();
  const full = page.getByTestId('rich-deliverable-full-view');
  const panel = full.locator('.rich-full-panel');
  expect((await panel.boundingBox())!.width).toBe(page.viewportSize()!.width);
  await full.evaluate(el => { el.scrollTop = 600; });
  await expect.poll(async () => (await full.getByTestId('rich-viewer-chrome').boundingBox())!.y).toBeLessThan(-400);
  await page.keyboard.press('Escape');
  await expect(full).toHaveCount(0);
  await expect(embedded).toHaveCSS('border-top-width', border);
});
