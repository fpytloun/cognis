import { test, expect } from '@playwright/test';
import { resolve } from 'node:path';

const source = {
  subject_id: 'html-browser-subject', source_token: 'html-browser-source-proof',
  source: { kind: 'document', content_ref: 'art_4ad53de97cfe4253931563af979f4d0b' },
  context: { kind: 'chat', conversation_id: 'html-browser-origin' }, content_digest: 'a'.repeat(64),
  availability: 'available', permitted_locator_kinds: ['whole', 'html'], canonical_text: null,
  lineage_id: null, revision: 1, latest_content_ref: null, title: 'HTML lifetime', format: 'html',
  capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false },
};
const files = new Map<string, { body: string | Buffer; contentType: string }>();
let entry = '';
test.beforeAll(async () => {
  const { build } = await import('vite');
  const { svelte } = await import('@sveltejs/vite-plugin-svelte');
  const result = await build({
    configFile: false, logLevel: 'error',
    plugins: [svelte(), {
      name: 'browser-fixture-sveltekit-environment',
      resolveId(id) {
        if (id === '$app/environment') return '\0html-host-environment';
        if (id === '$env/dynamic/public') return '\0html-host-public-env';
      },
      load(id) {
        if (id === '\0html-host-environment') return 'export const browser=true,dev=false,building=false;export const version="fixture";';
        if (id === '\0html-host-public-env') return 'export const env={};';
      },
    }],
    resolve: { alias: { $lib: resolve('src/lib'), '$app/paths': resolve('src/standalone-paths.ts') } },
    build: { write: false, minify: false, rollupOptions: { input: 'e2e/fixtures/html-host.ts' } },
  });
  const outputs = Array.isArray(result) ? result.flatMap(value => value.output) : 'output' in result ? result.output : [];
  for (const output of outputs) {
    if (output.type === 'chunk' && output.isEntry) entry = `/${output.fileName}`;
    files.set(`/${output.fileName}`, {
      body: output.type === 'chunk' ? output.code : typeof output.source === 'string' ? output.source : Buffer.from(output.source),
      contentType: output.fileName.endsWith('.css') ? 'text/css' : output.fileName.endsWith('.woff2') ? 'font/woff2' : 'text/javascript',
    });
  }
});

test('real Host/controller preserves opaque HTML observation and capture through binding snapshots', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const drafts: unknown[] = [];
  await page.route('https://html-host.test/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.startsWith('/api/auth/')) return route.fulfill({ json: {
      status: 'authenticated', user: { email: 'html-host@cognis.test', name: 'HTML host fixture', role: 'user' },
      email: 'html-host@cognis.test', expires_at: new Date(Date.now() + 3600000).toISOString(),
    } });
    if (path.endsWith('/sources/resolve')) return route.fulfill({ json: source });
    if (path.endsWith('/threads')) return route.fulfill({ json: { items: [], next_cursor: null } });
    if (path.includes('/drafts/')) {
      if (route.request().method() === 'PUT') {
        drafts.push(route.request().postDataJSON()); return route.fulfill({ json: { version: drafts.length } });
      }
      return route.fulfill({ status: 404, json: { detail: 'No draft' } });
    }
    const asset = files.get(path);
    if (asset) return route.fulfill(asset);
    if (path !== '/') return route.abort();
    const styles = [...files.keys()].filter(value => value.endsWith('.css')).map(value => `<link rel="stylesheet" href="${value}">`).join('');
    // The application stylesheet locks body scrolling; the real reader supplies
    // a bounded scrolling content owner, which this isolated fixture also needs.
    return route.fulfill({ contentType: 'text/html', body: `<!doctype html><html><head>${styles}</head><body><main id="fixture" style="height:100dvh;overflow:auto"></main><script type="module" src="${entry}"></script></body></html>` });
  });
  await page.goto('https://html-host.test/');
  const frame = page.frameLocator('iframe');
  await expect(frame.getByRole('heading')).toHaveText('Observed heading');
  await frame.getByRole('button', { name: 'Run action' }).click();
  await expect(frame.getByRole('button')).toHaveText('Action performed');
  await page.getByRole('button', { name: 'Feedback', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Feedback panel', exact: true });
  await panel.getByRole('button', { name: /^Comment$/i }).click();
  await panel.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Feedback', exact: true })).toBeHidden();
  await frame.getByRole('heading').click();
  const submit = page.getByRole('button', { name: 'Comment on element', exact: true });
  await expect(submit).toBeEnabled();
  await page.screenshot({ path: info.outputPath('html-observation-retained.png'), fullPage: true });
  await submit.click();
  await expect(page.getByRole('dialog', { name: 'Feedback', exact: true })).toBeVisible();
  await panel.getByLabel('Comment', { exact: true }).fill('Retained element candidate');
  await expect.poll(() => drafts.length).toBeGreaterThan(0);
  expect(drafts.at(-1)).toMatchObject({ anchor: { locator: { kind: 'html', element_key: 'heading' } } });
  await panel.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Replace preview' }).click();
  await expect(frame.getByRole('heading')).toHaveText('Replacement heading');
  await expect(submit).toBeDisabled();
  expect(await page.locator('iframe').getAttribute('sandbox')).toBe('allow-scripts');
  await page.getByRole('button', { name: 'Feedback', exact: true }).click();
  await panel.getByRole('button', { name: /^Area$/i }).click();
  await panel.getByRole('button', { name: 'Close', exact: true }).click();
  const pixels = await page.locator('iframe').screenshot();
  await page.getByLabel('Choose PNG/WebP screenshot').setInputFiles({
    name: 'observed-viewport.png', mimeType: 'image/png', buffer: pixels,
  });
  await expect(page.getByAltText('Unconfirmed screenshot, local only')).toBeVisible();
  await page.getByLabel('The crop shows the entire intended artifact viewport, excluding private application chrome.').check();
  await page.getByRole('button', { name: 'Confirm crop and freeze', exact: true }).click();
  await expect(page.getByAltText('Confirmed frozen HTML observation')).toBeVisible();
  await page.getByText('Keyboard pin', { exact: true }).click();
  await page.getByRole('button', { name: 'Add pin', exact: true }).click();
  await expect(page.getByLabel('Annotation highlights', { exact: true }).locator('path')).toHaveCount(1);
  await expect(page.getByAltText('Confirmed frozen HTML observation')).toBeVisible();
  await expect(page.getByText('Unverified user-provided screenshot. Its authenticity is not asserted.')).toBeVisible();
  await page.screenshot({ path: info.outputPath('html-capture-frozen.png'), fullPage: true });
  await page.getByRole('button', { name: 'Comment on markup', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Feedback', exact: true })).toBeVisible();
  await expect(panel.getByText('Comment on html selection', { exact: true })).toBeVisible();
  await expect(panel.getByText('Saving adopts the observed screenshot for this annotation.', { exact: true })).toBeVisible();
  const beforeCaptureDraft = drafts.length;
  await panel.getByLabel('Comment', { exact: true }).fill('Confirmed observed pixel markup');
  await expect.poll(() => drafts.length).toBeGreaterThan(beforeCaptureDraft);
  // The real controller intentionally excludes unadopted geometry from remote
  // drafts in persistRemoteDraft; its live candidate retains the confirmed capture.
  expect(drafts.at(-1)).toMatchObject({ anchor: null, body: 'Confirmed observed pixel markup' });
  await panel.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Revoke preview guard' }).click();
  await expect(page.locator('iframe')).toHaveCount(0);
  await expect(page.getByAltText('Confirmed frozen HTML observation')).toHaveCount(0);
  expect(errors).toEqual([]);
});
