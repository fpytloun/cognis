import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { createServer, type ViteDevServer } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

const directory = '/tmp/cognis-neutral-review/candidate';
let server: ViteDevServer;
let url: string;
test.beforeAll(async () => {
  const sourceRoot = process.env.COGNIS_REVIEW_SOURCE_ROOT ?? process.cwd();
  server = await createServer({
    configFile: false, root: sourceRoot,
    optimizeDeps: { noDiscovery: true, include: [] },
    resolve: { alias: { $lib: resolve(sourceRoot, 'src/lib') } },
    plugins: [{
      name: 'review-production-components',
      resolveId(id) { if (id.startsWith('$app/') || id.startsWith('$env/')) return `\0${id}`; },
      load(id) {
        if (id === '\0$app/environment') return 'export const browser = true; export const dev = true;';
        if (id === '\0$app/paths') return 'export const base = "";';
        if (id === '\0$app/navigation') return 'export const beforeNavigate = () => {}; export const goto = async () => {};';
        if (id.startsWith('\0$env/')) return 'export const env = {};';
      },
      configureServer(server) {
        server.middlewares.use('/review-production-components', (_req, res) => {
          res.setHeader('Content-Type', 'text/html');
          res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="fixture"></div><script type="module" src="/e2e/fixtures/theme-review/entry.ts"></script></body></html>');
        });
      },
    }, svelte()],
    server: { host: '127.0.0.1', port: 0 },
  });
  await server.listen();
  url = server.resolvedUrls!.local[0] + 'review-production-components';
});
test.afterAll(async () => { await server?.close(); });
test.use({ serviceWorkers: 'block' });
test('Classic native and web helpers retain original low-alpha paints', async ({ page }) => {
  await page.goto(`${url}?family=classic&mode=dark&restoration`);
  const native = page.getByTestId('classic-native-paint');
  await native.getByRole('button', { name: /read/ }).first().click();
  await expect(native.locator('.bg-native-5')).toHaveCSS('background-color', 'rgba(14, 165, 233, 0.05)');
  await expect(native.locator('.bg-native-5')).toHaveCSS('border-color', 'rgba(14, 165, 233, 0.25)');
  const link = page.getByTestId('classic-link-foreground');
  await expect(link).toHaveCSS('color', 'rgb(56, 189, 248)');
  await link.hover();
  await expect(link).toHaveCSS('color', 'rgb(186, 230, 253)');
  await expect(page.getByRole('button', { name: 'Original action foreground' })).toHaveCSS('background-color', 'rgb(14, 165, 233)');
  await expect(native.locator('.legacy-quiet').first()).toHaveCSS('color', 'rgb(100, 116, 139)');
  await expect(native.locator('.text-muted').filter({ hasText: '/repo/review.ts' }).first()).toHaveCSS('color', 'rgb(148, 163, 184)');
  await expect(page.locator('.hljs-comment')).toHaveCSS('color', 'rgb(100, 116, 139)');
  await expect(page.locator('.hljs-punctuation')).toHaveCSS('color', 'rgb(100, 116, 139)');
  await expect(page.locator('.json-number').first()).toHaveCSS('color', 'rgb(186, 230, 253)');
  await expect(page.locator('.json-boolean').first()).toHaveCSS('color', 'rgb(103, 232, 249)');
  await expect(page.locator('.hljs-variable').first()).toHaveCSS('color', 'rgb(226, 232, 240)');
  const web = page.getByTestId('classic-web-paint');
  await web.getByRole('button', { name: /web_fetch/ }).first().click();
  await expect(web.locator('.bg-violet-5')).toHaveCSS('background-color', 'rgba(139, 92, 246, 0.05)');
  await expect(web.locator('.bg-violet-5')).toHaveCSS('border-color', 'rgba(139, 92, 246, 0.25)');
  // A local opposite palette must not inherit the Classic alpha channels.
  await native.evaluate((node) => { node.dataset.themeFamily = 'paper'; node.dataset.resolvedTheme = 'light'; });
  const paperPaint = await native.evaluate(node =>
    getComputedStyle(node).getPropertyValue('--color-blue-subtle').trim().replace(/\s+/g, ', '));
  await expect(native.locator('.bg-native-5')).toHaveCSS('background-color', `rgb(${paperPaint})`);
});
test('Classic failed compaction and web result use distinct original failure paints', async ({ page }) => {
  await page.goto(`${url}?family=classic&mode=dark&restoration`);
  const compaction = page.getByTestId('classic-failed-compaction').locator('article');
  await expect(compaction).toHaveCSS('background-color', 'rgba(244, 63, 94, 0.1)');
  await expect(compaction).toHaveCSS('border-color', 'rgba(244, 63, 94, 0.3)');
  await expect(compaction).toHaveCSS('color', 'rgb(255, 228, 230)');
  const web = page.getByTestId('classic-web-failed-paint');
  await web.getByRole('button', { name: /web_fetch/ }).first().click();
  await expect(web.locator('.bg-error-5')).toHaveCSS('background-color', 'rgba(244, 63, 94, 0.05)');
  await expect(web.locator('.bg-error-5')).toHaveCSS('border-color', 'rgba(244, 63, 94, 0.3)');
  await expect(web.locator('.bg-error-10')).toHaveCSS('background-color', 'rgba(244, 63, 94, 0.1)');
  await expect(web.locator('.bg-error-10')).toHaveCSS('border-color', 'rgba(251, 113, 133, 0.25)');
  await expect(web.locator('.bg-error-10')).toHaveCSS('color', 'rgb(255, 228, 230)');
});
test('Classic dark restores original component RGBA paints', async ({ page }) => {
  await page.goto(`${url}?family=classic&mode=dark`);
  const compaction = page.locator('.compaction-info');
  await expect(compaction).toHaveCSS('background-color', 'rgba(6, 182, 212, 0.1)');
  await expect(compaction).toHaveCSS('border-color', 'rgba(6, 182, 212, 0.3)');
  await expect(compaction).toHaveCSS('color', 'rgb(207, 250, 254)');
  await page.getByRole('button', { name: /agent_conversation_interrupt/ }).click();
  const helper = page.locator('.helper-managed');
  await expect(helper).toHaveCSS('background-color', 'rgba(139, 92, 246, 0.05)');
  await expect(helper).toHaveCSS('border-color', 'rgba(167, 139, 250, 0.25)');
  const recap = page.getByRole('region', { name: 'Conversation recap' });
  await expect(recap).toHaveCSS('background-color', 'rgba(15, 23, 42, 0.9)');
  await expect(recap).toHaveCSS('border-color', 'rgba(14, 165, 233, 0.25)');
});
test('Paper production-component font characterization', async ({ page }) => {
  await page.goto(`${url}?family=paper&mode=dark`);
  await expect(page.locator('.tool-frame').first()).toBeVisible();
  const fonts = await page.evaluate(() => {
    const font = (selector: string) => getComputedStyle(document.querySelector(selector)!).fontFamily;
    return { body: font('body'), tool: font('.tool-frame'), toolHeader: font('.tool-frame button'),
      control: font('.chat-composer-input'), assistant: font('[data-message-role="assistant"] .chat-markdown'),
      user: font('[data-message-role="user"] .chat-markdown') };
  });
  console.log(`Paper font characterization ${JSON.stringify(fonts)}`);
  expect(fonts.body).toContain('sans-serif');
  expect(fonts.tool).toBe(fonts.body);
  expect(fonts.toolHeader).toBe(fonts.body);
  expect(fonts.control).toBe(fonts.body);
  expect(fonts.user).toBe(fonts.body);
  expect(fonts.assistant).toContain('Georgia');
});
for (const mode of ['light', 'dark']) {
  for (const width of [1440, 390]) {
    test(`Paper ${mode} full response shell ${width}`, async ({ page }) => {
      await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
      let mutations = 0;
      await page.route('**/api/v1/attention-actions/**', async (route) => {
        if (route.request().method() !== 'GET') { mutations++; await route.abort(); return; }
        await route.fulfill({ json: {
          action_id: 'review-question', revision: 1, kind: 'step_question', title: 'Response required',
          status: 'pending', availability: 'actionable', can_resolve: true, has_action_form: true,
          allowed_actions: [{ action: 'continue', label: 'Send response', intent: 'primary', input: 'structured' },
            { action: 'cancel', label: 'Cancel request', intent: 'danger', input: 'none' }],
          display: { questions: [{ id: 'scope', question: 'Do you authorize completion of the local source-code and regression-test review for the already approved fixes, including cross-client synchronization, drag, restore/error classification, and image durable submission? This local preview does not authorize pushing, deployment, history rewriting, installed-app changes, or data deletion. After your answer, the agent will report its evidence and stop; the parent will allocate the next bounded phase.',
            options: [{ id: 'review', label: 'Approve scoped implementation and regression tests' },
              { id: 'blocked', label: 'Keep blocked' }], multiple: false, allow_custom: true, required: true }] },
        } });
      });
      await page.goto(`${url}?family=paper&mode=${mode}&full-shell`);
      const shell = page.getByRole('dialog', { name: 'Response required' });
      await expect(shell).toBeVisible();
      const legend = shell.locator('legend');
      await expect(legend).toHaveCSS('font-size', '15px');
      const box = await legend.boundingBox();
      const option = await shell.locator('label').first().boundingBox();
      expect(option!.y).toBeGreaterThanOrEqual(box!.y + box!.height);
      const fonts = await page.evaluate(() => {
        const font = (selector: string) => getComputedStyle(document.querySelector(selector)!).fontFamily;
        return { body: font('body'), tool: font('.tool-frame'), control: font('.chat-composer-input'),
          assistant: font('[data-message-role="assistant"] .chat-markdown'), prompt: font('legend'),
          header: font('[role="dialog"] h1'), choice: font('[role="dialog"] label'),
          button: font('[role="dialog"] button') };
      });
      console.log(`Paper ${mode} ${width} fonts ${JSON.stringify(fonts)}`);
      expect(fonts.body).toContain('sans-serif');
      expect(fonts.tool).toBe(fonts.body);
      expect(fonts.control).toBe(fonts.body);
      expect(fonts.assistant).toContain('Georgia');
      expect(fonts.header).toBe(fonts.body);
      expect(fonts.choice).toBe(fonts.body);
      expect(fonts.button).toBe(fonts.body);
      await shell.getByRole('radio').first().check();
      for (const name of ['Send response', 'Cancel request']) {
        const button = shell.getByRole('button', { name, exact: true });
        await button.scrollIntoViewIfNeeded();
        await expect(button).toBeVisible();
      }
      await shell.getByTestId('attention-dialog-close').focus();
      await page.keyboard.press('Tab');
      expect(await shell.evaluate((node) => node.contains(document.activeElement))).toBe(true);
      expect(await page.locator(':focus').evaluate((node) => node.matches(':focus-visible'))).toBe(true);
      await legend.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${directory}/paper-${mode}-full-response-${width}.png` });
      expect(mutations).toBe(0);
    });
  }
}
test('deployed baseline production-component captures', async ({ page }) => {
  test.skip(!process.env.COGNIS_REVIEW_SOURCE_ROOT, 'Requires the exact deployed source snapshot');
  const beforeDirectory = '/tmp/cognis-neutral-review/before';
  mkdirSync(beforeDirectory, { recursive: true });
  for (const family of ['porcelain', 'classic', 'paper', 'gruvbox']) {
    for (const mode of ['light', 'dark']) {
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.goto(`${url}?family=${family}&mode=${mode}`);
      await expect(page.locator('html')).toHaveAttribute('data-theme-family', family);
      await expect(page.locator('.chat-composer-input')).toBeVisible();
      await page.screenshot({ path: `${beforeDirectory}/${family}-${mode}-chat-1440.png` });
      const queue = page.getByRole('region', { name: 'Queued messages' });
      await queue.getByRole('button', { name: /Expand queued/ }).click();
      const memory = page.locator('article').filter({ has: page.getByRole('button', { name: /memory_add/ }) }).last();
      await memory.getByRole('button', { name: /memory_add/ }).click();
      await memory.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${beforeDirectory}/${family}-${mode}-memory-info-1440.png` });
      const managed = page.getByRole('button', { name: /agent_conversation_interrupt/ });
      await managed.click();
      await managed.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${beforeDirectory}/${family}-${mode}-expanded-group-1440.png` });
      await page.getByTestId('attention-action-renderer').scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${beforeDirectory}/${family}-${mode}-question-1440.png` });
      await page.locator('.chat-composer-input').scrollIntoViewIfNeeded();
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: `${beforeDirectory}/${family}-${mode}-chat-390.png` });
    }
  }
});
for (const family of ['porcelain', 'classic', 'paper', 'gruvbox']) {
  for (const mode of ['light', 'dark']) {
    test(`local candidate actual components ${family} ${mode}`, async ({ page }) => {
      mkdirSync(directory, { recursive: true });
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.goto(`${url}?family=${family}&mode=${mode}`);
      const root = page.locator('html');
      await expect(root).toHaveAttribute('data-theme-family', family);
      await expect(root).toHaveAttribute('data-resolved-theme', mode);
      const assistant = page.locator('[data-message-role="assistant"] .chat-markdown');
      const composer = page.locator('.chat-composer-input');
      await expect(assistant).toHaveCSS('font-size', family === 'paper' ? '15px' : '14px');
      await expect(composer).toHaveCSS('font-size', family === 'paper' ? '15px' : '14px');
      await expect(assistant.locator('code')).toHaveCSS('font-size', '14px');
      if (family === 'gruvbox' || family === 'porcelain') {
        const outgoing = page.locator('[data-message-role="user"]');
        expect(await outgoing.evaluate((node) => {
          const style = getComputedStyle(node);
          return style.backgroundColor === `rgb(${style.getPropertyValue('--color-accent').trim().replace(/ /g, ', ')})`;
        })).toBe(false);
        const contrasts = await outgoing.evaluate((node) => {
          const luminance = (color: string) => color.match(/[\d.]+/g)!.slice(0, 3).map(Number)
            .map((c) => c / 255).map((c) => c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4)
            .reduce((sum, c, i) => sum + c * [.2126, .7152, .0722][i], 0);
          const background = luminance(getComputedStyle(node).backgroundColor);
          return [...node.querySelectorAll('p, a')].map((leaf) => {
            const foreground = luminance(getComputedStyle(leaf).color);
            return (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
          });
        });
        for (const ratio of contrasts) expect(ratio).toBeGreaterThanOrEqual(4.5);
      }
      const stable = await assistant.elementHandle();
      await composer.fill('Retain this unsent note.');
      await page.screenshot({ path: `${directory}/${family}-${mode}-chat-1440.png` });
      const queue = page.getByRole('region', { name: 'Queued messages' });
      await queue.getByRole('button', { name: /Expand queued/ }).click();
      await expect(queue.getByText('1 attachment', { exact: true })).toBeVisible();
      await queue.getByRole('button', { name: /Edit queued/ }).click();
      await expect(queue.locator('textarea')).toBeVisible();
      await queue.getByRole('button', { name: 'Cancel', exact: true }).click();
      const memory = page.locator('article').filter({ has: page.getByRole('button', { name: /memory_add/ }) }).last();
      await memory.getByRole('button', { name: /memory_add/ }).click();
      const detail = "Unknown category 'cognis'. Valid categories include preferences and technical.";
      await expect(memory.getByText(detail, { exact: true })).toHaveCount(1);
      await expect(memory.getByText(detail, { exact: true })).toBeVisible();
      await expect(memory.getByText('failed', { exact: false }).first()).toBeVisible();
      await memory.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${directory}/${family}-${mode}-memory-info-1440.png` });
      const raw = memory.getByRole('button', { name: /Raw payload/ });
      await raw.click();
      await expect(memory).toContainText('memory_validation_error');
      await expect(memory).toContainText('correct_arguments_then_retry');
      await raw.click();
      const managed = page.getByRole('button', { name: /agent_conversation_interrupt/ });
      await managed.click();
      await managed.scrollIntoViewIfNeeded();
      await expect(page.getByText('Interrupt managed conversation', { exact: true })).toBeVisible();
      await page.screenshot({ path: `${directory}/${family}-${mode}-expanded-group-1440.png` });
      const request = page.getByTestId('attention-action-renderer');
      await request.scrollIntoViewIfNeeded();
      const legend = request.locator('legend');
      const firstOption = request.locator('label').first();
      const boxes = await Promise.all([legend.boundingBox(), firstOption.boundingBox()]);
      expect(boxes[1]!.y).toBeGreaterThanOrEqual(boxes[0]!.y + boxes[0]!.height);
      await page.screenshot({ path: `${directory}/${family}-${mode}-question-1440.png` });
      await composer.scrollIntoViewIfNeeded();
      await composer.focus();
      await expect(composer).toBeFocused();
      expect(await stable!.evaluate((node) => node.isConnected)).toBe(true);
      await expect(composer).toHaveValue('Retain this unsent note.');
      await page.setViewportSize({ width: 390, height: 844 });
      await page.screenshot({ path: `${directory}/${family}-${mode}-chat-390.png` });
    });
  }
}
