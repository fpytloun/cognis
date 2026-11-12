// Serial real-screen qualification of the built app and actual standalone entries.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium, expect } from '@playwright/test';
import { preview } from 'vite';

const root = process.cwd();
const output = process.env.MERMAID_CAPTURE_DIR;
assert.ok(output, 'Set MERMAID_CAPTURE_DIR to a task-owned evidence directory');
assert.equal(execFileSync('git', ['status', '--porcelain', '--', '.'], { encoding: 'utf8' }).trim(), '');
const revision = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
const hash = createHash('sha256');
for (const file of ['RichDeliverable.svelte', 'mermaid-presentation.ts', 'namespace-svg.ts']) {
  hash.update(file).update(await readFile(`src/lib/components/rich/${file}`));
}
const sourceHash = hash.digest('hex');
await mkdir(output, { recursive: true });
const standalone = resolve(root, 'standalone-build');
const payload = {
  content: 'Read-only Mermaid qualification', instanceId: 'mermaid-qualification',
  title: 'Mermaid ownership checkpoint',
  payload: { blocks: [{ type: 'mermaid', title: 'Request path', source: 'flowchart LR\nA[Request] --> B[Response]' }] },
};
async function html(offline) {
  const directory = offline ? `${standalone}/offline` : standalone;
  const manifest = JSON.parse(await readFile(`${directory}/.vite/manifest.json`, 'utf8'));
  const entry = Object.values(manifest).find((item) => item.isEntry);
  const css = new Set();
  const visited = new Set();
  function styles(item) {
    if (visited.has(item.file)) return;
    visited.add(item.file);
    for (const file of item.css ?? []) css.add(file);
    for (const id of item.imports ?? []) styles(manifest[id]);
  }
  styles(entry);
  const prefix = '/api/v1/deliverables/standalone-assets/';
  const stylesheet = offline
    ? `<style>${(await Promise.all([...css].map((file) => readFile(`${directory}/${file}`, 'utf8')))).join('\n')}</style>`
    : [...css].map((file) => `<link rel="stylesheet" href="${prefix}${file}">`).join('');
  const script = offline
    ? `<script>${(await readFile(`${directory}/${entry.file}`, 'utf8')).replaceAll('</script', '<\\/script')}</script>`
    : `<script type="module" src="${prefix}${entry.file}"></script>`;
  return `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">${stylesheet}</head><body><div id="cognis-deliverable-root"></div><template id="cognis-deliverable-payload" data-offline="${offline}">${JSON.stringify(payload).replaceAll('<', '\\u003c')}</template>${script}</body></html>`;
}
const connectedHtml = await html(false);
const offlineFile = resolve(output, `${revision.slice(0, 12)}-offline.html`);
await writeFile(offlineFile, await html(true));
const assets = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1:5199');
    if (url.pathname === '/fixture') {
      response.writeHead(200, { 'content-type': 'text/html' }).end(connectedHtml);
      return;
    }
    const prefix = '/api/v1/deliverables/standalone-assets/';
    const file = resolve(standalone, url.pathname.slice(prefix.length));
    if (!url.pathname.startsWith(prefix) || !file.startsWith(`${standalone}/`)) {
      response.writeHead(404).end();
      return;
    }
    const mime = { '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml' };
    response.writeHead(200, { 'content-type': mime[extname(file)] ?? 'application/octet-stream' }).end(await readFile(file));
  } catch {
    response.writeHead(404).end();
  }
});
await new Promise((done, reject) => { assets.once('error', reject); assets.listen(5199, '127.0.0.1', done); });
let app;
let browser;
const results = [];
const mutations = [];
const errors = [];
const failures = [];
async function scenario(name, run) {
  try {
    await run();
  } catch (error) {
    failures.push({ name, message: String(error) });
  }
}
try {
  app = await preview({ root, preview: { host: '127.0.0.1', port: 5198, strictPort: true } });
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, reducedMotion: 'reduce', colorScheme: 'dark', serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('request', (request) => { if (request.method() !== 'GET') mutations.push({ method: request.method(), url: request.url() }); });
  page.on('pageerror', (error) => errors.push(error.message));
  async function capture(name, scope = page.locator('[data-testid="rich-deliverable"]').first()) {
    const diagram = scope.locator('.rich-mermaid svg').first();
    await expect(diagram).toBeVisible();
    await expect(scope.locator('pre[data-mermaid-source]')).toHaveCount(0);
    await page.evaluate(async () => {
      await document.fonts.ready;
      await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    });
    const geometry = await diagram.evaluate((svg) => {
      const box = svg.getBoundingClientRect();
      const parent = svg.parentElement.getBoundingClientRect();
      const ids = [...svg.querySelectorAll('[id]')].map((node) => node.id);
      const references = [...svg.querySelectorAll('[aria-labelledby], [aria-describedby]')].flatMap((node) =>
        ['aria-labelledby', 'aria-describedby'].flatMap((key) => (node.getAttribute(key) ?? '').split(/\s+/).filter(Boolean)));
      return { left: box.left, right: box.right, parentLeft: parent.left, parentRight: parent.right,
        width: box.width, height: box.height, duplicateIds: ids.length - new Set(ids).size,
        missingAccessibleReferences: references.filter((id) => !ids.includes(id)),
        parentOverflowX: getComputedStyle(svg.parentElement).overflowX,
        parentClientWidth: svg.parentElement.clientWidth, parentScrollWidth: svg.parentElement.scrollWidth,
        viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth };
    });
    const path = resolve(output, `${revision.slice(0, 12)}-${sourceHash.slice(0, 12)}-${name}.png`);
    await page.screenshot({ path, fullPage: true });
    results.push({ name, path, geometry, revision, sourceHash });
    expect(geometry.width).toBeGreaterThan(0);
    expect(geometry.height).toBeGreaterThan(0);
    expect(geometry.left).toBeGreaterThanOrEqual(geometry.parentLeft - 1);
    // Existing rich-blocks.css intentionally provides an overflow:auto diagram
    // surface. Qualify its containment, not a ban on the supported inner scroll.
    if (!['auto', 'scroll'].includes(geometry.parentOverflowX)) {
      expect(geometry.right).toBeLessThanOrEqual(geometry.parentRight + 1);
    }
    expect(geometry.parentLeft).toBeGreaterThanOrEqual(-1);
    expect(geometry.parentRight).toBeLessThanOrEqual(geometry.viewport + 1);
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewport + 1);
    expect(geometry.duplicateIds).toBe(0);
    expect(geometry.missingAccessibleReferences).toEqual([]);
  }
  await page.goto('http://127.0.0.1:5198/rich-deliverable-block-fixture?block=mermaid');
  await page.evaluate(() => document.documentElement.setAttribute('data-resolved-theme', 'dark'));
  await scenario('embedded-dark', () => capture('embedded-dark'));
  await scenario('fullscreen-dark', async () => {
    await page.getByRole('button', { name: 'Open fullscreen' }).first().click();
    await capture('fullscreen-dark', page.getByTestId('rich-deliverable-full-view'));
    await page.getByRole('button', { name: /^Close$/ }).click();
  });
  await expect(page.getByTestId('rich-deliverable-full-view')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => document.documentElement.setAttribute('data-resolved-theme', 'light'));
  await expect(page.locator('[data-testid="rich-deliverable"]').first()).toHaveAttribute('data-viewer-theme', 'light');
  await scenario('embedded-light-phone', () => capture('embedded-light-phone'));
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('http://127.0.0.1:5199/fixture');
  await scenario('connected-standalone-light', () => capture('connected-standalone-light'));
  await context.setOffline(true);
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto(pathToFileURL(offlineFile).href);
  await scenario('offline-standalone-dark', () => capture('offline-standalone-dark'));
  await writeFile(resolve(output, 'visual.json'), JSON.stringify({ revision, sourceHash, browser: browser.version(), mutations, errors, failures, results }, null, 2));
  console.log(JSON.stringify({ revision, captures: results.length, mutations, errors, failures }));
  expect(mutations).toEqual([]);
  expect(errors).toEqual([]);
  expect(failures).toEqual([]);
} finally {
  await browser?.close();
  if (app) await new Promise((done) => app.httpServer.close(done));
  await new Promise((done) => assets.close(done));
}
