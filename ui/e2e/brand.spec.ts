import { expect, test } from '@playwright/test';
import { COMPONENT_RUNTIME_ORIGIN } from './component-runtime';

// Local-only: run against Vite with COGNIS_E2E_URL=http://127.0.0.1:5197.
// No credentials or backend required.
test('static loading and recovery remain usable without application startup', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ javaScriptEnabled: false, reducedMotion: 'reduce' });
  const page = await context.newPage();
  await page.goto(`${baseURL}/login`);
  const loader = page.getByRole('status', { name: 'Loading Cognis' });
  await expect(loader).toBeVisible();
  expect(await loader.locator('img').evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
  expect(await loader.locator('.progress').evaluate((el) => getComputedStyle(el).animationName)).toBe('none');
  await page.screenshot({ path: test.info().outputPath('loading.png') });
  await context.close();
});

test('normal login, real component variants, sizing, and recovery', async ({ page }) => {
  await page.route((url) => url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ status: 401, json: { detail: 'Unauthenticated' } }));
  await page.goto('/login');
  await expect(page.getByRole('heading', { name: 'Cognis', exact: true })).toBeVisible();
  await expect(page.locator('#cognis-startup-loading')).toHaveCount(0);
  const logo = page.locator('.cognis-logo');
  await expect(logo).toHaveCount(1);
  const rem = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
  expect(await logo.boundingBox()).toMatchObject({ width: 4 * rem, height: 4 * rem });
  await page.screenshot({ path: test.info().outputPath('login.png') });

  const gallery = await page.context().newPage();
  try {
    await gallery.goto(`${COMPONENT_RUNTIME_ORIGIN}/brand.html`);
    for (const size of [16, 18, 22, 32, 48, 96]) {
      const mark = gallery.getByRole('img', { name: `mono ${size}`, exact: true }).first();
      await expect(mark).toBeVisible();
      expect(await mark.boundingBox()).toMatchObject({ width: size, height: size });
    }
    await gallery.screenshot({ path: test.info().outputPath('light-dark-variants.png') });
  } finally {
    await gallery.close();
  }
  await page.evaluate(() => window.dispatchEvent(new Event('vite:preloadError', { cancelable: true })));
  await expect(page.getByRole('heading', { name: 'Reload Cognis' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Reload app' })).toBeEnabled();
  await page.screenshot({ path: test.info().outputPath('recovery.png') });
});
