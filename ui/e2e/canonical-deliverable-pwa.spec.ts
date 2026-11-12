import { readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';
import { expect, test, type Page } from '@playwright/test';

const build = resolve('standalone-build');
const entry = JSON.parse(readFileSync(resolve(build, '.vite/manifest.json'), 'utf8'))['src/standalone.ts'];
const document = {
  title: 'Sample inventory: what changes and how it works',
  instanceId: 'dlv_owner',
  content: '',
  payload: {
    metadata: { toc: { enabled: true, depth: 4 } },
    blocks: [{ type: 'markdown', content: '# Sample inventory\n\n' + Array.from({ length: 11 }, (_, i) =>
      `## Section ${i + 1}\n\n${'Inventory design and operation details. '.repeat(10)}`
    ).join('\n\n') }],
  },
};
const viewUrl = '/api/v1/deliverables/dlv_owner/view';
const csp = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; sandbox allow-scripts allow-same-origin; base-uri 'none'; frame-ancestors 'self'";

async function setup(page: Page, { installed = true, owner = true, expired = false, contentDocument = document } = {}) {
  const created: unknown[] = [];
  await page.addInitScript(({ installed }) => {
    const original = window.matchMedia.bind(window);
    window.matchMedia = query => {
      const media = original(query);
      if (query === '(display-mode: standalone)') Object.defineProperty(media, 'matches', { value: installed });
      return media;
    };
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true, value: { writeText: async () => {} },
    });
  }, { installed });
  await page.route('**/standalone-assets/assets/*', route => {
    const file = basename(new URL(route.request().url()).pathname);
    return route.fulfill({ body: readFileSync(resolve(build, 'assets', file)),
      contentType: file.endsWith('.css') ? 'text/css' : file.endsWith('.woff2') ? 'font/woff2' : 'text/javascript' });
  });
  await page.route(`**${viewUrl}`, route => route.fulfill({
    contentType: 'text/html; charset=utf-8', headers: { 'Content-Security-Policy': csp },
    body: `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${entry.css.map((file: string) => `<link rel="stylesheet" href="/standalone-assets/${file}">`).join('')}</head><body><div id="cognis-deliverable-root"></div><template id="cognis-deliverable-payload" data-share-expired="${expired}" data-pdf-url="/download.pdf" data-html-url="/download.html" data-markdown-url="/download.md">${JSON.stringify(contentDocument)}</template><script type="module" src="/standalone-assets/${entry.file}"></script></body></html>`,
  }));
  await page.route('**/api/v1/deliverables/dlv_owner/viewer-context', route => route.fulfill({
    status: owner ? 200 : 401, json: owner ? { share_link_url: '/api/v1/deliverables/dlv_owner/share-link' } : { error: 'Authentication required' },
  }));
  await page.route('**/api/v1/deliverables/dlv_owner/share-link', route => {
    created.push(route.request().postDataJSON());
    return route.fulfill({ json: { url: 'https://cognis.fpy.cz/created-share-link' } });
  });
  await page.route('**/app-return', route => route.fulfill({
    contentType: 'text/html', body: `<a href="${viewUrl}">Open document</a>`,
  }));
  await page.route('**/', route => route.fulfill({ contentType: 'text/html', body: 'App home' }));
  return created;
}

for (const width of [390, 1440]) {
  test(`canonical PWA preview retains TOC, owner expiry controls and close at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    const created = await setup(page, { expired: true });
    // The real signed redirect and exact capability expiry are covered by API tests.
    await page.goto(viewUrl);
    await expect(page).toHaveURL(new RegExp(`${viewUrl}$`));
    await expect(page.getByRole('status')).toContainText('Shared link expired');
    await expect(page.locator('summary[aria-label="Download document"]')).toBeVisible();
    if (width === 390) await page.getByRole('button', { name: 'Open table of contents' }).click();
    await expect(page.getByRole('button', { name: 'Section 11', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Section 11', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Section 11', exact: true })).toBeFocused();
    await page.evaluate(() => scrollTo(0, 0));
    const share = page.getByRole('button', { name: 'Share deliverable' });
    await expect(share).toBeEnabled();
    await share.click();
    for (const label of ['1 day', '7 days (default)', '30 days', '90 days']) {
      await expect(page.getByRole('button', { name: label, exact: true })).toBeVisible();
    }
    await page.getByRole('button', { name: '90 days', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Share link copied' })).toBeVisible();
    expect(created).toEqual([{ ttl_seconds: 90 * 86400 }]);
    await page.getByRole('button', { name: 'Share link copied' }).click();
    await page.getByRole('button', { name: 'Custom…' }).click();
    const date = new Date(Date.now() + 2 * 86400000);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    await page.getByLabel('Expiration date and time').fill(local);
    expect(await page.getByRole('group', { name: 'Share link expiration' }).evaluate(node => {
      const rect = node.getBoundingClientRect(); return rect.left >= 0 && rect.right <= innerWidth;
    })).toBe(true);
    await page.getByRole('button', { name: 'Create link', exact: true }).click();
    expect(created[1]).toEqual({ expires_at: new Date(local).toISOString() });
    await page.getByRole('button', { name: 'Close preview' }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByText('App home')).toBeVisible();
  });
}

test('PWA close returns to previous app page under production sandbox', async ({ page }) => {
  await setup(page);
  await page.goto('/app-return');
  await page.getByRole('link', { name: 'Open document' }).click();
  await page.getByRole('button', { name: 'Close preview' }).click();
  await expect(page).toHaveURL(/\/app-return$/);
});

test('browser and anonymous preview keep canonical TOC without owner/PWA controls', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setup(page, { installed: false, owner: false });
  await page.goto(viewUrl);
  await expect(page.getByRole('button', { name: 'Section 11', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Share deliverable' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Close preview' })).toHaveCount(0);
});

test('selection copy preserves colon-prefixed Unicode prose under production sandbox', async ({ page, browserName }) => {
  const first = 'baterie: kouřový senzor a PIR v zimní zahradě (0 %).';
  const second = 'Z-Wave: zkontrolovat napájení/jistič.';
  await setup(page, { owner: false, contentDocument: {
    ...document, content: `${first}\n\n${second}`,
    payload: { ...document.payload, blocks: [{ type: 'markdown', content: `${first}\n\n${second}` }] },
  } });
  await page.goto(viewUrl);
  await expect(page.getByText(first, { exact: true })).toBeVisible();
  await page.evaluate(({ first, second }) => {
    const paragraphs = Array.from(window.document.querySelectorAll('p'));
    const start = paragraphs.find(p => p.textContent === first)!;
    const end = paragraphs.find(p => p.textContent === second)!;
    const range = window.document.createRange();
    range.setStart(start.firstChild!, 0);
    range.setEnd(end.firstChild!, second.length);
    getSelection()!.removeAllRanges();
    getSelection()!.addRange(range);
    window.addEventListener('copy', event => {
      (window as unknown as { copiedSelection: unknown }).copiedSelection = {
        intercepted: event.defaultPrevented,
        plainText: event.clipboardData?.getData('text/plain'),
        html: event.clipboardData?.getData('text/html'),
        types: Array.from(event.clipboardData?.types ?? []),
      };
    }, { once: true });
  }, { first, second });
  if (browserName === 'webkit') {
    // Playwright's mobile WebKit keyboard does not dispatch the native Copy command.
    await page.evaluate(() => window.document.execCommand('copy'));
  } else {
    await page.keyboard.press('ControlOrMeta+C');
  }
  const copied = await page.evaluate(() =>
    (window as unknown as { copiedSelection: { intercepted: boolean; plainText: string; html: string; types: string[] } }).copiedSelection);
  expect(copied.intercepted).toBe(true);
  expect(copied.plainText).toBe(`${first}\n\n${second}`);
  expect(copied.html).toContain(first);
  expect(copied.types.sort()).toEqual(['text/html', 'text/plain']);
});
