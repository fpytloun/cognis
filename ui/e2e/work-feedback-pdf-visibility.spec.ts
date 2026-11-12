import { expect, test, type Page } from '@playwright/test';
import { createHash, randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';

const quote = 'Synthetic Work Feedback acceptance fixture';
const digest = createHash('sha256').update(quote).digest('hex');

function pdf() {
  const content = `BT /F1 10 Tf 72 750 Td (${quote}) Tj ET`;
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${content.length} >>\nstream\n${content}\nendstream`];
  let text = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, index) => { offsets.push(Buffer.byteLength(text)); text += `${index + 1} 0 obj\n${object}\nendobj\n`; });
  const start = Buffer.byteLength(text);
  text += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}`;
  return Buffer.from(`${text}trailer << /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`);
}

async function setup(page: Page) {
  page.on('pageerror', error => console.error(`PDF visibility fixture: ${error.message}`));
  const saved: { anchor: { locator: { kind: string; range?: { start: number; end: number; exact: string }; shapes: { x: number; y: number; width: number; height: number }[] } } }[] = [];
  await page.route('**/pdf-visibility-fixture.pdf', route => route.fulfill({ contentType: 'application/pdf', body: pdf() }));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/auth/me')) return json({ email: 'pdf-review@example.test', display_name: 'PDF reviewer', is_admin: false, is_active: true });
    if (path.endsWith('/sources/resolve')) return json({
      source: route.request().postDataJSON().source, context: { kind: 'standalone' }, subject_id: 'pdf-visibility-source',
      source_token: 'pdf-visibility-source-token', content_digest: digest, availability: 'available', format: 'pdf', title: 'visibility.pdf',
      lineage_id: null, revision: 1, latest_content_ref: null, permitted_locator_kinds: ['whole', 'pdf'],
      capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false },
    });
    if (path.endsWith('/sources/text')) return json({
      scope: { kind: 'pdf_page', page: 1 }, content_digest: digest,
      descriptor: { representation: 'feedback-text-v1', text_digest: digest, length: quote.length, available_ranges: [[0, quote.length]] },
      text_page: { representation: 'feedback-text-v1', text_digest: digest, start: 0, end: quote.length, total_length: quote.length, text: quote },
    });
    if (path.endsWith('/threads') && route.request().method() === 'GET') return json({ items: [], next_cursor: null });
    if (path.includes('/drafts/')) return json(route.request().method() === 'PUT' ? { version: 1 } : { detail: 'No draft' }, route.request().method() === 'PUT' ? 200 : 404);
    if (path.endsWith('/threads') && route.request().method() === 'POST') {
      const body = route.request().postDataJSON(); saved.push(body);
      return json({ thread_id: randomUUID(), subject_id: 'pdf-visibility-source', version: 1, state: 'open', audience: 'internal',
        hidden: false, anchor: { ...body.anchor, anchor_id: randomUUID() }, entries: [] }, 201);
    }
    return json({ detail: 'Not found' }, 404);
  });
  await page.goto('/work-feedback-inline-fixture?surface=pdf');
  await expect(page.getByRole('heading', { name: 'PDF source visibility fixture' })).toBeVisible();
  await expect(page.locator('.textLayer')).toHaveText(quote);
  // The auto-fit label may already round to 76; commit a different value first.
  for (const percent of [75, 76]) {
    await page.getByRole('spinbutton', { name: 'Zoom percentage' }).fill(String(percent));
    await page.getByRole('spinbutton', { name: 'Zoom percentage' }).press('Tab');
    await expect.poll(async () => (await page.getByLabel('PDF page 1', { exact: true }).boundingBox())!.width).toBeCloseTo(595.28 * percent / 100 * 96 / 72, 1);
  }
  await expect(page.getByRole('spinbutton', { name: 'Zoom percentage' })).toHaveValue('76');
  await page.getByRole('button', { name: 'Annotate', pressed: false }).first().click();
  await expect(page.getByLabel('PDF page 1', { exact: true })).toHaveAttribute('aria-busy', 'false');
  await expect.poll(async () => (await page.getByLabel('PDF page 1', { exact: true }).boundingBox())!.width).toBeCloseTo(595.28 * .76 * 96 / 72, 1);
  return saved;
}

test.use({ viewport: { width: 1480, height: 1040 } });

test('PDF quote and exact end stay highlighted and uncovered after focus at 76% zoom', async ({ page }, info) => {
  const saved = await setup(page);
  await page.locator('.textLayer').evaluate((root, exact) => {
    const node = root.querySelector('span')!.firstChild!;
    if (node.textContent !== exact) throw new Error('Expected the complete exact quote');
    const range = document.createRange(); range.selectNodeContents(node);
    getSelection()!.removeAllRanges(); getSelection()!.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
  }, quote);
  await expect.poll(() => page.evaluate(() => getSelection()?.toString())).toBe(quote);
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  const input = page.getByRole('textbox', { name: 'Comment', exact: true });
  await expect(input).toBeFocused(); await input.fill('Keep the entire selected PDF quote visible.');
  const measure = () => page.locator('.textLayer').evaluate(root => {
    const node = root.querySelector('span')!.firstChild!, range = document.createRange();
    range.selectNodeContents(node); const quote = range.getBoundingClientRect();
    range.setStart(node, node.textContent!.length - 1); const end = range.getBoundingClientRect();
    const card = document.querySelector('section[aria-label="Add comment"]')!.getBoundingClientRect();
    const rect = (value: DOMRect) => ({ left: value.left, top: value.top, right: value.right, bottom: value.bottom, width: value.width, height: value.height });
    const overlapping = card.left < quote.right && card.right > quote.left && card.top < quote.bottom && card.bottom > quote.top;
    const retained = [...document.querySelectorAll('[data-feedback-target-highlight]')].filter(element => {
      const style = getComputedStyle(element), box = element.getBoundingClientRect();
      return style.backgroundColor !== 'rgba(0, 0, 0, 0)' &&
        box.left <= end.left + 1 && box.right >= end.right - 1 && box.top <= end.top + 1 && box.bottom >= end.bottom - 1;
    });
    const hit = document.elementFromPoint(end.left + end.width / 2, end.top + end.height / 2);
    return { quote: rect(quote), end: rect(end), card: rect(card), overlapping, retained: retained.length,
      endVisible: !!hit && root.contains(hit),
      focused: document.activeElement === document.querySelector('section[aria-label="Add comment"] textarea') };
  });
  await expect.poll(async () => (await measure()).overlapping).toBe(false);
  await expect.poll(async () => (await measure()).retained).toBeGreaterThan(0);
  const measurements = await measure(); expect(measurements.endVisible).toBe(true); expect(measurements.focused).toBe(true);
  const path = info.outputPath('pdf-quote-after-focus.json'); await writeFile(path, JSON.stringify(measurements, null, 2));
  await info.attach('pdf-quote-after-focus', { path, contentType: 'application/json' });
  await page.screenshot({ path: info.outputPath('pdf-quote-after-focus.png') });
  await page.getByRole('button', { name: 'Show target', exact: true }).click();
  await page.getByRole('button', { name: 'Resume draft', exact: true }).click();
  await expect(input).toBeFocused(); await expect(input).toHaveValue('Keep the entire selected PDF quote visible.');
  await expect.poll(async () => (await measure()).overlapping).toBe(false);
  await expect.poll(async () => (await measure()).retained).toBeGreaterThan(0);
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0].anchor.locator).toMatchObject({ kind: 'pdf', shapes: [], range: { start: 0, end: quote.length, exact: quote } });
});

test('PDF area anchors retain their normalized page mapping at 76% zoom', async ({ page }) => {
  const saved = await setup(page);
  await page.getByRole('button', { name: 'Mark area', exact: true }).click();
  await expect(page.getByRole('combobox', { name: 'Markup', exact: true })).toHaveValue('rectangle');
  const canvas = (await page.getByLabel('PDF page 1', { exact: true }).boundingBox())!;
  const start = { x: Math.round(canvas.x + canvas.width * .2), y: Math.round(canvas.y + canvas.height * .25) };
  const end = { x: Math.round(canvas.x + canvas.width * .5), y: Math.round(canvas.y + canvas.height * .6) };
  await page.mouse.move(start.x, start.y); await page.mouse.down();
  await page.mouse.move(end.x, end.y, { steps: 12 }); await page.mouse.up();
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Preserve page-space area coordinates.');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0].anchor.locator.kind).toBe('pdf');
  const shape = saved[0].anchor.locator.shapes[0];
  expect(shape.x).toBeCloseTo((start.x - canvas.x) / canvas.width * 595.28, 1);
  expect(shape.y).toBeCloseTo((start.y - canvas.y) / canvas.height * 841.89, 1);
  expect(shape.width).toBeCloseTo((end.x - start.x) / canvas.width * 595.28, 1);
  expect(shape.height).toBeCloseTo((end.y - start.y) / canvas.height * 841.89, 1);
});
