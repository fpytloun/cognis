import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { createServer, type ViteDevServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

let server: ViteDevServer;
let url: string;
test.beforeAll(async () => {
  server = await createServer({
    configFile: false,
    root: process.cwd(),
    // This fixture imports only ESM packages. Avoid an asynchronous dependency
    // discovery reload while the first browser assertion is mounting the chat.
    optimizeDeps: { noDiscovery: true, include: [] },
    resolve: { alias: { $lib: fileURLToPath(new URL('../src/lib', import.meta.url)) } },
    plugins: [
      {
        name: 'palette-fixture',
        resolveId(id) {
          if (id.startsWith('$app/') || id.startsWith('$env/')) return `\0${id}`;
        },
        load(id) {
          if (id === '\0$app/environment') return 'export const browser = true; export const dev = true;';
          if (id === '\0$app/paths') return 'export const base = "";';
          if (id.startsWith('\0$env/')) return 'export const env = {};';
        },
        configureServer(server) {
          server.middlewares.use('/palette-fixture', (_req, res) => {
            res.setHeader('Content-Type', 'text/html');
            res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="fixture"></div><script type="module" src="/e2e/fixtures/theme-palettes/entry.ts"></script></body></html>');
          });
        },
      },
      svelte(),
    ],
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  url = server.resolvedUrls!.local[0] + 'palette-fixture';
});
test.afterAll(async () => { await server?.close(); });

const pairs = [
  ['classic', 'light', '248 250 252', '#e9eef4'],
  ['classic', 'dark', '2 6 23', '#07111a'],
  ['porcelain', 'light', '249 249 250', '#ececed'],
  ['porcelain', 'dark', '17 18 19', '#18191a'],
  ['gruvbox', 'light', '251 241 199', '#f2e5bc'],
  ['gruvbox', 'dark', '40 40 40', '#1b1b1b'],
  ['paper', 'light', '250 249 245', '#efede6'],
  ['paper', 'dark', '38 38 36', '#1f1e1d'],
] as const;

test('all eight palettes are local, preserve node identity, and keep chat text/code paired', async ({ page }) => {
  await page.goto(url);
  await expect(page.locator('[data-message-role="assistant"]')).toBeVisible();
  const originalMessage = await page.locator('[data-message-role="assistant"]').elementHandle();
  for (const [family, mode, canvas, chrome] of pairs) {
    await page.evaluate(({ family, mode }) => {
      document.documentElement.dataset.themeFamily = family;
      document.documentElement.dataset.resolvedTheme = mode;
    }, { family, mode });
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--color-canvas').trim())).toBe(canvas);
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--app-chrome-bg').trim())).toBe(chrome);
    expect(await originalMessage!.evaluate((node) => node === document.querySelector('[data-message-role="assistant"]'))).toBe(true);
    await expect(page.locator('.chat-markdown').first()).toHaveCSS('font-size', family === 'paper' ? '15px' : '14px');
    await expect(page.locator('textarea')).toHaveCSS('font-size', family === 'paper' ? '15px' : '14px');
    const assistant = page.locator('[data-message-role="assistant"]');
    const body = assistant.locator('.chat-markdown');
    if (family === 'paper') {
      await expect(body).toHaveCSS('font-family', /Georgia/);
      await expect(assistant).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
      expect(await page.locator('[data-message-role="user"]').evaluate((node) => getComputedStyle(node).backgroundColor))
        .not.toBe(await page.evaluate(() => `rgb(${getComputedStyle(document.documentElement).getPropertyValue('--color-accent').trim().split(' ').join(', ')})`));
    } else {
      await expect(body).not.toHaveCSS('font-family', /Georgia/);
    }
    await expect(body.locator('code').first()).not.toHaveCSS('font-family', /Georgia/);
    await expect(assistant.locator('[data-copy-exclude]').last()).not.toHaveCSS('font-family', /Georgia/);
    // Opposite-mode local reader must reset aliases as well as primitive colors.
    const readerMode = mode === 'light' ? 'dark' : 'light';
    await page.locator('#reader').evaluate((node, mode) => { (node as HTMLElement).dataset.resolvedTheme = mode; }, readerMode);
    const reader = await page.locator('#reader').evaluate((node) => {
      const style = getComputedStyle(node);
      return [style.getPropertyValue('--color-canvas').trim(), style.getPropertyValue('--theme-void').trim(), style.getPropertyValue('--color-syntax-keyword').trim()];
    });
    expect(reader).toEqual(readerMode === 'dark'
      ? ['2 6 23', 'rgb(2 6 23)', '125 211 252']
      : ['248 250 252', 'rgb(248 250 252)', '109 40 217']);
    await page.screenshot({ path: test.info().outputPath(`${family}-${mode}.png`) });
  }
  for (const [family, mode, canvas, chrome] of pairs) {
    await page.locator('#reader').evaluate((node, { family, mode }) => {
      (node as HTMLElement).dataset.themeFamily = family;
      (node as HTMLElement).dataset.resolvedTheme = mode;
    }, { family, mode });
    expect(await page.locator('#reader').evaluate((node) => {
      const style = getComputedStyle(node);
      return [style.getPropertyValue('--color-canvas').trim(), style.getPropertyValue('--app-chrome-bg').trim()];
    })).toEqual([canvas, chrome]);
  }
  await page.locator('textarea').focus();
  expect(await page.locator('.chat-composer-surface').evaluate((node) => getComputedStyle(node).boxShadow)).toContain('2px');
});

test('CSS boot defaults follow OS and native rail stays transparent', async ({ page }) => {
  for (const mode of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: mode });
    await page.goto(url);
    await expect(page.locator('[data-message-role="assistant"]')).toBeVisible();
    const expected = mode === 'light' ? '248 250 252' : '2 6 23';
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--color-canvas').trim())).toBe(expected);
    await page.evaluate(() => { document.documentElement.dataset.nativeSurface = 'rail'; });
    await expect(page.locator('html')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
    await expect(page.locator('body')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  }
});

test('coarse-pointer composer retains 16px text without changing the viewport', async ({ browser }) => {
  const context = await browser.newContext({ hasTouch: true, viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await page.goto(url);
  await expect(page.locator('textarea')).toHaveCSS('font-size', '16px');
  await page.locator('textarea').focus();
  expect(await page.evaluate(() => window.visualViewport?.scale)).toBe(1);
  await context.close();
});
