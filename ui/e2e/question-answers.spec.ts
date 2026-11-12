import { expect, test } from '@playwright/test';
import { createServer, type ViteDevServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';
import { mkdirSync } from 'node:fs';

let server: ViteDevServer;
let url: string;
const screenshots = '/tmp/cognis-question-answer-review';
test.beforeAll(async () => {
  server = await createServer({
    configFile: false, root: process.cwd(), cacheDir: 'node_modules/.vite-question-review',
    optimizeDeps: { noDiscovery: true, include: [] },
    resolve: { alias: { $lib: resolve('src/lib') } },
    plugins: [{
      name: 'question-answer-review',
      resolveId(id) { if (id.startsWith('$app/') || id.startsWith('$env/')) return `\0${id}`; },
      load(id) {
        if (id === '\0$app/paths') return 'export const base = "";';
        if (id === '\0$app/environment') return 'export const browser = true; export const dev = true;';
        if (id === '\0$app/navigation') return 'export const goto = async () => {}; export const beforeNavigate = () => {};';
        if (id.startsWith('\0$env/')) return 'export const env = {};';
      },
      configureServer(server) {
        server.middlewares.use('/question-answer-review', (_req, res) => {
          res.setHeader('Content-Type', 'text/html');
          res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="fixture"></div><script type="module" src="/e2e/fixtures/question-answers/entry.ts"></script></body></html>');
        });
      },
    }, svelte()],
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  url = server.resolvedUrls!.local[0] + 'question-answer-review';
  mkdirSync(screenshots, { recursive: true });
});
test.afterAll(async () => { await server?.close(); });

for (const width of [900, 390]) {
  test(`pending panels collapse, preserve drafts and stay bounded at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 700 });
    await page.goto(url);
    for (const id of ['pending-question', 'pending-approval']) {
      const fixture = page.getByTestId(id);
      const panel = fixture.locator('section').first();
      const toggle = fixture.getByRole('button', { name: /requested more input|Approval required/ });
      const input = fixture.getByRole('textbox');
      await input.fill('Keep this draft');
      expect((await panel.boundingBox())!.height).toBeLessThanOrEqual(350);
      await toggle.focus();
      await page.keyboard.press('Enter');
      await expect(toggle).toHaveAttribute('aria-expanded', 'false');
      await expect(input).toBeHidden();
      expect((await panel.boundingBox())!.height).toBeLessThan(90);
      await page.keyboard.press('Enter');
      await expect(input).toHaveValue('Keep this draft');
      await expect(toggle).toHaveAttribute('aria-expanded', 'true');
      await page.setViewportSize({ width, height: 400 });
      expect((await panel.boundingBox())!.height).toBeLessThanOrEqual(208);
      await page.setViewportSize({ width, height: 700 });
    }
    const drawer = page.getByTestId('ongoing').locator('section');
    await drawer.getByRole('button', { name: /Ongoing work/ }).hover();
    await expect(drawer).toHaveCSS('overflow', 'hidden');
    expect(await drawer.evaluate((node) => parseFloat(getComputedStyle(node).borderTopLeftRadius))).toBeGreaterThan(0);
    await drawer.screenshot({ path: `${screenshots}/ongoing-hover-${width}.png` });
    await page.getByTestId('pending-approval').screenshot({ path: `${screenshots}/pending-approval-${width}.png` });
  });
}

for (const width of [900, 390]) {
  for (const mode of ['dark', 'light']) {
    test(`question pagination is compact, height-stable and scrollable at ${width}px in ${mode}`, async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.goto(`${url}?mode=${mode}`);
      const single = page.getByTestId('single');
      const multi = page.getByTestId('multiple');
      const compact = page.getByTestId('compact');
      await expect(single.getByText('You answered question')).toBeVisible();
      await expect(single.locator('strong').first()).toHaveText('one sample label');
      await expect(multi.getByText('Answer 1 / 3')).toBeVisible();
      const firstHeight = (await multi.boundingBox())!.height;
      expect((await compact.boundingBox())!.height).toBeLessThan(firstHeight - 100);
      await single.screenshot({ path: `${screenshots}/single-${width}-${mode}.png` });
      await compact.screenshot({ path: `${screenshots}/compact-${width}-${mode}.png` });
      await multi.getByRole('button', { name: 'Next answer' }).click();
      await expect(multi.getByText('Answer 2 / 3')).toBeVisible();
      expect((await multi.boundingBox())!.height).toBe(firstHeight);
      const request = multi.getByRole('group', { name: 'Agent request content' });
      const answer = multi.getByRole('group', { name: 'Your answer content' });
      await multi.getByRole('button', { name: 'Next answer' }).blur();
      await multi.screenshot({ path: `${screenshots}/multiple-${width}-${mode}.png` });
      for (const area of [request, answer]) {
        expect(await area.evaluate((node) => node.scrollHeight > node.clientHeight)).toBe(true);
        await area.focus();
        await page.keyboard.press('End');
        await expect.poll(() => area.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
      }
      await multi.getByRole('button', { name: 'Next answer' }).click();
      await expect(multi.getByText('Answer 3 / 3')).toBeVisible();
      expect((await multi.boundingBox())!.height).toBe(firstHeight);
      await expect(multi.getByRole('button', { name: 'Next answer' })).toBeDisabled();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
  }
}
