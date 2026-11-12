// Renderer acceptance through the real adapter/controller using synthetic HTTP contracts.
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';

const cwd = fileURLToPath(new URL('../../', import.meta.url));
if (execFileSync('git', ['status', '--porcelain', '--untracked-files=all', '--', '.'], { cwd, encoding: 'utf8' }).trim()) {
  throw new Error('Commit the task-owned UI checkpoint before capturing revision-labelled evidence.');
}
const port = process.env.USAGE_CAPTURE_PORT || '5208';
const origin = `http://127.0.0.1:${port}`;
const output = process.env.USAGE_CAPTURE_DIR;
if (!output) throw new Error('Set USAGE_CAPTURE_DIR to a task-owned directory.');
await mkdir(output, { recursive: true });
const server = spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--config', 'vite.usage.config.ts', '--port', port], { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
let browser;
const evidence = [], errors = [];
try {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.once('exit', (code) => reject(new Error(`Fixture server exited (${code})`)));
    server.stdout.on('data', (chunk) => { if (chunk.toString().includes(`127.0.0.1:${port}`)) resolve(); });
    server.stderr.on('data', (chunk) => process.stderr.write(chunk));
  });
  browser = await chromium.launch({ headless: true });
  const surfaces = ['overview', 'costs', 'performance', 'companions', 'work', 'explorer', 'limits'];
  for (const theme of ['light', 'dark']) {
    for (const [name, width, height] of [['phone', 390, 844], ['laptop', 1366, 900], ['wide', 1920, 1080]]) {
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: theme });
      const page = await context.newPage();
      await page.route(/^https?:\/\/[^/]+\/api\//, (route) => {
        errors.push(`Unexpected fixture request: ${new URL(route.request().url()).pathname}`);
        return route.abort('blockedbyclient');
      });
      const applyTheme = () => page.evaluate((value) => {
        document.documentElement.dataset.themeFamily = 'classic';
        document.documentElement.dataset.resolvedTheme = value;
        document.documentElement.classList.toggle('dark', value === 'dark');
      }, theme);
      page.on('pageerror', (error) => { errors.push(error.message); console.error(error.message); });
      page.on('console', (message) => { if (message.type() === 'error') console.error(message.text()); });
      await page.route('**/provider-monitor**', (route) => route.fulfill({ json: [{
        provider_id: 'usage-fixture-provider-alpha', display_name: 'Fixture Alpha', usage: {
          primary: { used_percent: 74, window_duration_mins: 300, resets_at: null },
          secondary: { used_percent: 32, window_duration_mins: 10080, resets_at: null },
          plan_type: 'Pro'
        }
      }] }));
      await page.route('**/usage/providers/*/history?*', (route) => route.fulfill({ json: {
        samples: Array.from({ length: 6 }, (_, index) => ({
          observed_at: `2026-11-03T${String(index + 10).padStart(2, '0')}:00:00Z`,
          window: 'primary', utilization: String(20 + index * 10)
        })), next_cursor: null
      } }));
      for (const surface of surfaces) {
        await page.goto(`${origin}/?usage_surface=${surface}&usage_scope=personal`);
        await applyTheme();
        await expect(page.getByRole('heading', { name: 'Usage', exact: true })).toBeVisible();
        await expect(page.getByRole('status', { name: 'Loading usage', exact: true })).toHaveCount(0);
        if (surface === 'limits') await expect(page.getByText('5 hours', { exact: true }).first()).toBeVisible();
        else if (['explorer', 'companions', 'work'].includes(surface)) await expect(page.getByRole('table').first()).toBeVisible();
        else await expect(page.locator('main svg[role="group"]').first()).toBeVisible();
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        if (surface === 'limits') {
          const size = await page.getByRole('img', { name: /limit usage over/ }).first().evaluate((svg) => ({
            viewBox: svg.viewBox.baseVal.width, rendered: svg.getBoundingClientRect().width
          }));
          expect(Math.abs(size.viewBox - size.rendered)).toBeLessThanOrEqual(1);
        }
        const geometry = await page.evaluate(() => ({
          viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth,
          buttonRadius: getComputedStyle(document.querySelector('main button')).borderRadius,
          theme: getComputedStyle(document.body).backgroundColor
        }));
        expect(geometry.scrollWidth).toBeLessThanOrEqual(width);
        expect(geometry.buttonRadius).not.toBe('0px');
        const path = `${output}/${name}-${theme}-${surface}.png`;
        await page.screenshot({ path, fullPage: true });
        evidence.push({ path, surface, theme, width, height, geometry, fixtureBacked: true });
      }
      // Error and empty-state acceptance at each width/theme.
      await page.goto(`${origin}/?usage_surface=overview`);
      await applyTheme();
      await page.getByLabel('Fixture state').selectOption('error');
      await expect(page.getByRole('alert').first()).toContainText('Reference usage-fixture-diagnostic-17');
      await page.screenshot({ path: `${output}/${name}-${theme}-error.png`, fullPage: true });
      await page.getByLabel('Fixture state').selectOption('empty');
      await expect(page.getByRole('status', { name: 'Loading usage', exact: true })).toHaveCount(0);
      await expect(page.getByRole('alert')).toHaveCount(0);
      await page.screenshot({ path: `${output}/${name}-${theme}-empty.png`, fullPage: true });
      await context.close();
    }
  }
  expect(errors).toEqual([]);
  await writeFile(`${output}/manifest.json`, JSON.stringify({
    base: execFileSync('git', ['rev-parse', 'HEAD'], { cwd, encoding: 'utf8' }).trim(),
    sourceRoot: cwd,
    diff: execFileSync('git', ['diff', '--stat', 'HEAD'], { cwd, encoding: 'utf8' }),
    diffSha256: createHash('sha256').update(execFileSync('git', ['diff', '--binary', 'HEAD'], { cwd, maxBuffer: 16 * 1024 * 1024 })).digest('hex'),
    evidence, errors, apiBacked: false
  }, null, 2));
  console.log(`Captured ${evidence.length} view screenshots and 12 state screenshots in ${output}`);
} finally {
  await browser?.close();
  if (server.exitCode === null && server.signalCode === null) {
    const stopped = once(server, 'exit'); server.kill('SIGTERM'); await stopped;
  }
}
