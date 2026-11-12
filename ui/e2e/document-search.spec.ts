/// <reference types="node" />
import { expect, test, type Locator } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import { deliverable, libraryItem } from './fixtures/resource-revisions/data';
import { installTaskCockpitFixture } from './task-cockpit-fixture';
import { installLibraryFixture } from './library-fixture';

async function expectToolbarSearch(bar: Locator, header: Locator, height: number) {
  await expect(header.locator('[data-find-bar]')).toBeVisible();
  expect((await header.boundingBox())!.height).toBe(height);
  const count = await bar.getByRole('status').evaluate(node => {
    const rect = node.getBoundingClientRect();
    const field = node.closest('.find-field')!.getBoundingClientRect();
    return { clipped: node.scrollWidth > node.clientWidth, left: rect.left, right: rect.right, fieldLeft: field.left, fieldRight: field.right };
  });
  expect(count.clipped).toBe(false);
  expect(count.left).toBeGreaterThanOrEqual(count.fieldLeft);
  expect(count.right).toBeLessThanOrEqual(count.fieldRight);
}

function twoPagePdf(): Buffer {
  const streams = [
    'BT /F1 18 Tf 40 250 Td (The release is ) Tj /F1 19 Tf (rea) Tj /F1 18 Tf (dy for review.) Tj ET',
    'BT /F1 18 Tf 40 250 Td (Ready for launch.) Tj ET',
  ];
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    ...[6, 7].map(stream => `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 320] /Resources << /Font << /F1 5 0 R >> >> /Contents ${stream} 0 R >>`),
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ...streams.map(stream => `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`)];
  let bytes = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((object, index) => { offsets.push(Buffer.byteLength(bytes)); bytes += `${index + 1} 0 obj\n${object}\nendobj\n`; });
  const start = Buffer.byteLength(bytes);
  bytes += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  offsets.slice(1).forEach(offset => { bytes += `${String(offset).padStart(10, '0')} 00000 n \n`; });
  return Buffer.from(`${bytes}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`);
}

for (const width of [390, 1440]) {
  test.describe(`${width}px document search`, () => {
    // Playwright's worker-blocking init script accesses navigator.serviceWorker
    // inside opaque iframes and throws. Each test already has a fresh context.
    test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390, serviceWorkers: 'allow' });
    test('chat uses the shared field inside its unchanged header without covering the timeline', async ({ page }, info) => {
      await installLibraryFixture(page);
      await page.route('**/api/v1/feedback/conversations/conv-task-chat/staging', route => route.fulfill({
        json: { conversation_id: 'conv-task-chat', version: 1, receipts: [], items: [] },
      }));
      await page.route('**/api/v1/search/health', route => route.fulfill({ json: { enabled: true } }));
      await page.route('**/api/v1/search/conversation/*', route => route.fulfill({ json: { matches: route.request().postDataJSON().q === 'remote' ? [
        { conversation_id: 'conv-task-chat', session_id: 'sess-task-chat', intaris_session_id: 'sess-task-chat', kind_rank: 0,
          match: { kind: 'reasoning', role: 'assistant', snippet: 'Remote historical reasoning match.', score: 1 } },
      ] : [] } }));
      await page.goto('/chat/conv-task-chat');
      const trigger = width === 390 ? page.getByRole('button', { name: 'More conversation options' })
        : page.getByRole('button', { name: 'Search conversation', exact: true });
      await expect(trigger).toBeVisible();
      const timeline = page.getByTestId('timeline-viewport');
      await expect(timeline).toBeVisible();
      if (width === 1440) await expect(page.getByRole('button', { name: 'S3 Stage 39 Agent', exact: true })).toBeVisible();
      await expect(page.getByText('The release is waiting for your approval. Deterministic evidence passed, and no implementation error remains.', { exact: true })).toBeVisible();
      await page.evaluate(async () => {
        await document.fonts.ready;
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      });
      const before = await timeline.boundingBox();
      const header = page.getByTestId('chat-header');
      const headerHeight = (await header.boundingBox())!.height;
      if (width === 390) {
        await trigger.tap();
        await page.getByRole('menuitem', { name: 'Search conversation', exact: true }).tap();
      } else await trigger.click();
      const field = page.getByRole('searchbox', { name: 'Search this conversation' });
      await expect(field).toBeVisible();
      expect((await timeline.boundingBox())!.y).toBe(before!.y);
      await field.fill('release is waiting');
      await field.press('Enter');
      const panel = page.getByRole('search', { name: 'Search this conversation' });
      await expect(panel.getByRole('status')).toHaveText('1 of 1');
      await expectToolbarSearch(panel, header, headerHeight);
      await expect(panel.getByRole('button')).toHaveCount(3);
      const match = page.locator('mark[data-chat-search-highlight="1"]').first();
      await expect(match).toBeVisible();
      await expect.poll(async () => (await match.boundingBox())!.y).toBeGreaterThanOrEqual((await header.boundingBox())!.y + headerHeight);
      await expect(page.getByText('Reconnecting…', { exact: true })).toHaveCount(0);
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      const bounds = await panel.boundingBox();
      expect(bounds!.x).toBeGreaterThanOrEqual(0);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
      await page.screenshot({ path: info.outputPath('chat-search.png') });
      await field.fill('remote');
      await field.press('Enter');
      await expect(page.getByRole('region', { name: 'Search match' })).toContainText('Remote historical reasoning match.');
      await expect.poll(async () => (await page.getByRole('region', { name: 'Search match' }).boundingBox())!.y)
        .toBeGreaterThanOrEqual((await header.boundingBox())!.y + headerHeight);
      await field.press('Escape');
      await expect(field).toHaveCount(0);
      await expect(trigger).toBeFocused();
    });
    for (const format of ['text', 'markdown', 'html', 'pdf'] as const) {
      const kind = format === 'markdown' ? 'deliverable' : 'artifact';
      test(`${format} finds rendered text without searching the Library or details`, async ({ page }, info) => {
        await installLibraryFixture(page);
        const id = `${kind === 'artifact' ? 'art' : 'dlv'}_${randomUUID().replace(/-/g, '')}`;
        const content = `# Readiness\n\nThe release is **ready** for review.\n\n${format === 'text' ? 'Background detail without a match.\n'.repeat(100) : ''}Ready for launch.\n\nNo other findings.`;
        const errors: string[] = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/report.pdf', route => route.fulfill({ contentType: 'application/pdf', body: twoPagePdf() }));
        await page.route(url => url.pathname.startsWith('/api/'), async route => {
          const path = new URL(route.request().url()).pathname;
          const json = (value: unknown) => route.fulfill({ json: value });
          if (path.endsWith('/report.html')) return route.fulfill({ contentType: 'text/html', body: '<!doctype html><body><h1>Readiness</h1><p>The release is <strong>ready</strong> for review.</p><p>Ready for launch.</p><p>No other findings.</p></body>' });
          if (path === '/api/auth/me') return json({ email: 'reader@cognis.test', name: 'Reader', role: 'user', is_active: true });
          if (path.endsWith('/sharing')) return json({ agents: [], project_id: null, owner_agent_id: null });
          if (path === `/api/v1/library/items/${kind}/${id}`) return json({ ...libraryItem(kind, id), format: format === 'markdown' ? 'markdown' : `text/${format === 'text' ? 'plain' : format}`, ...(format === 'pdf' ? { format: 'application/pdf' } : {}) });
          if (path.endsWith('/revisions')) return json({ revisions: [] });
          if (path.endsWith('/content')) return json({ ...deliverable(id), content });
          if (path.endsWith('/text-preview')) return json({ content, rich_payload: null, truncated: false });
          if (path.endsWith('/signed-url')) return json({ url: format === 'html'
            ? `/api/v1/artifacts/view/${randomUUID()}/${id}/report.html?exp=2147483647&sig=${'a'.repeat(64)}`
            : `/report.${format === 'text' ? 'txt' : format}` });
          if (path.endsWith('/sources/resolve')) return route.fulfill({ status: 404, json: { detail: 'No feedback source' } });
          if (path.endsWith('/feedback/counts')) return json({ counts: [] });
          return route.fallback();
        });
        await page.goto(`/library/${kind}/${id}`);
        if (format === 'html') await expect(page.frameLocator('iframe[title="Artifact preview"]').getByText('No other findings.')).toBeVisible();
        else if (format === 'pdf') await expect(page.locator('canvas[aria-label="PDF page 1"]')).toHaveAttribute('aria-busy', 'false');
        else await expect(page.getByRole('main', { name: 'Document preview' })).toContainText('No other findings.');
        await page.evaluate(async () => {
          await document.fonts.ready;
          await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
        });
        const contentRoot = page.locator('.document-find-content').first();
        const before = await contentRoot.boundingBox();
        const header = page.getByTestId('resource-reader').locator('header').first();
        const headerHeight = (await header.boundingBox())!.height;
        const magnifier = page.getByRole('button', { name: 'Search in document', exact: true }).first();
        if (width === 390) await magnifier.tap(); else await magnifier.click();
        const field = page.getByRole('searchbox', { name: 'Find in document', exact: true });
        await expect(field).toBeFocused();
        expect((await contentRoot.boundingBox())!.y).toBe(before!.y);
        await field.press('Escape');
        await expect(magnifier).toBeFocused();
        const more = page.getByLabel(format === 'markdown' ? 'More document actions' : 'More actions', { exact: true });
        if (format === 'markdown' || width === 390) {
          if (width === 390) await more.tap(); else await more.click();
          const menuSearch = page.locator('details[open]').getByRole('button', { name: 'Search in document', exact: true });
          if (width === 390) await menuSearch.tap(); else await menuSearch.click();
          await expect(field).toBeFocused();
          await expect(page.locator('details[open]')).toHaveCount(0);
          await field.press('Escape');
          await expect(more).toBeFocused();
        }
        await page.keyboard.press('Meta+f');
        await expect(field).toBeFocused();
        expect((await contentRoot.boundingBox())!.y).toBe(before!.y);
        await field.fill('ready');
        await expect(page.getByRole('search', { name: 'Find in document' })).toContainText('1 of 2');
        await expectToolbarSearch(page.getByRole('search'), header, headerHeight);
        await field.press('Enter');
        await expect(page.getByRole('search', { name: 'Find in document' })).toContainText('2 of 2');
        if (format === 'pdf') await expect(page.getByText('Page 2 / 2')).toBeVisible();
        if (format !== 'html') {
          const match = await page.evaluate(() => {
            const entry = [...CSS.highlights].find(([name]) => name.endsWith('-current'));
            const range = entry && [...entry[1]][0] as Range | undefined;
            const rect = range?.getBoundingClientRect();
            return rect ? { text: range?.toString(), top: rect.top, bottom: rect.bottom } : null;
          });
          expect(match?.text?.toLowerCase()).toBe('ready');
          const toolbarBounds = await header.boundingBox();
          expect(match!.top).toBeGreaterThanOrEqual(toolbarBounds!.y + toolbarBounds!.height);
          expect(match!.bottom).toBeLessThanOrEqual(900);
        }
        await field.press('Shift+Enter');
        await expect(page.getByRole('search', { name: 'Find in document' })).toContainText('1 of 2');
        if (format === 'pdf') {
          await page.getByRole('spinbutton', { name: 'Zoom percentage' }).fill('125');
          await page.getByRole('spinbutton', { name: 'Zoom percentage' }).blur();
          await expect(page.locator('canvas[aria-label="PDF page 1"]')).toHaveAttribute('aria-busy', 'false');
          expect(await page.evaluate(() => [...CSS.highlights].some(([name, ranges]) => name.endsWith('-current') && [...ranges].some(range => (range as Range).getBoundingClientRect().width > 0)))).toBe(true);
          await field.focus();
        }
        await page.keyboard.press('Meta+f');
        expect(await field.evaluate(node => [(node as HTMLInputElement).selectionStart, (node as HTMLInputElement).selectionEnd])).toEqual([0, 5]);
        await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
        const bounds = await page.getByRole('search').boundingBox();
        expect(bounds!.x).toBeGreaterThanOrEqual(0);
        expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
        await expect(page.getByText('Reconnecting…', { exact: true })).toHaveCount(0);
        await page.screenshot({ path: info.outputPath('document-search.png') });
        await field.fill('not present');
        await expect(page.getByRole('search')).toContainText('No matches');
        await expect(page.getByRole('button', { name: 'Next match' })).toBeDisabled();
        await field.press('Escape');
        await expect(field).toHaveCount(0);
        await expect(page.getByRole('main', { name: 'Document preview' })).toBeVisible();
        if (format === 'html') {
          await page.frameLocator('iframe[title="Artifact preview"]').getByText('Ready for launch.').click();
          await page.keyboard.press('Meta+f');
          await expect(field).toBeFocused();
          await field.fill('release is ready');
          await expect(page.getByRole('search')).toContainText('1 of 1');
        }
        expect(errors).toEqual([]);
      });
    }
  });
}
