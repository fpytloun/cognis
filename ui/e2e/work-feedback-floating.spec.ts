import { createHash, randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';
import type { FeedbackAnchor, FeedbackLocator, FeedbackThread, ResolvedFeedbackSource } from '../src/lib/feedback/types';

const text = 'Readable native source with syntax colors and overlapping comments.';
const digest = (value: string) => createHash('sha256').update(value).digest('hex');
const descriptor = { representation: 'feedback-text-v1' as const, text_digest: digest(text), length: text.length, available_ranges: [[0, text.length]] };
function pdfBytes() {
  const stream = `BT /F1 10 Tf 72 760 Td (${text}) Tj ET`;
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
  let value = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((object, index) => { offsets.push(value.length); value += `${index + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = value.length;
  value += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return Buffer.from(value);
}
async function setup(page: Page, kind = 'text', count = 12) {
  page.on('pageerror', error => console.error(`Floating fixture: ${error.message}`));
  const source: ResolvedFeedbackSource = {
    subject_id: randomUUID(), source_token: randomUUID(), source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
    context: { kind: 'standalone' }, content_digest: digest(`floating:${kind}:${text}`), availability: 'available',
    permitted_locator_kinds: ['whole', kind === 'text' ? 'text' : kind === 'diff' ? 'diff' : kind === 'pdf' ? 'pdf' : 'html'],
    lineage_id: null, revision: 1, latest_content_ref: null, title: 'Floating source', format: kind,
    capabilities: { read: true, comment: true, moderate: true, dispatch: false, disclose: false },
    canonical_text: kind === 'text' ? { ...descriptor, available_ranges: [[0, text.length]] } : null,
  };
  const range = { ...descriptor, start: 9, end: 15, exact: 'native', prefix: text.slice(0, 9), suffix: text.slice(15) };
  const threads: FeedbackThread[] = Array.from({ length: kind === 'text' ? count : 0 }, (_, index) => {
    const id = randomUUID();
    return { thread_id: id, subject_id: source.subject_id, version: 1, state: 'open', hidden: false, audience: 'internal',
      anchor: { schema_version: 1, anchor_id: randomUUID(), source: source.source, locator: { kind: 'text', range }, content_digest: source.content_digest },
      entries: [{ entry_id: randomUUID(), entry_version_id: randomUUID(), version: 1, thread_id: id, body: `Saved comment ${index + 1}`,
        author: { kind: index === 11 ? 'guest_unverified' : 'verified_user', label: index === 11 ? 'Visitor' : `Reviewer ${index + 1}`, is_self: index !== 11 },
        audience: 'internal', reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [] }] };
  });
  const saved: FeedbackLocator[] = [], replies: string[] = [];
  await page.route('**/floating-fixture.pdf', route => route.fulfill({ contentType: 'application/pdf', body: pdfBytes() }));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    if (path === '/api/auth/me') return route.fulfill({ json: { email: 'floating-review@example.test', display_name: 'Floating reviewer', is_admin: false, is_active: true } });
    if (path.endsWith('/sources/resolve')) return route.fulfill({ json: source });
    if (path.includes('/drafts/')) return route.fulfill(method === 'PUT' ? { json: { version: 1 } } : { status: 404, json: { detail: 'No remote draft' } });
    if (path.endsWith('/sources/text')) {
      const body = request.postDataJSON(), start = body.start ?? 0, end = Math.min(text.length, start + body.limit);
      return route.fulfill({ json: { scope: body.scope, content_digest: source.content_digest, descriptor,
        text_page: { representation: 'feedback-text-v1', text_digest: digest(text), start, end, total_length: text.length, text: text.slice(start, end) } } });
    }
    if (path.endsWith('/threads') && method === 'POST') {
      const body = request.postDataJSON() as { anchor: FeedbackAnchor; body: string }, id = randomUUID();
      saved.push(body.anchor.locator);
      const thread: FeedbackThread = { thread_id: id, subject_id: source.subject_id, version: 1, state: 'open', hidden: false, audience: 'internal',
        anchor: { schema_version: 1, anchor_id: randomUUID(), source: source.source, locator: body.anchor.locator, content_digest: source.content_digest },
        entries: [{ entry_id: randomUUID(), entry_version_id: randomUUID(), version: 1, thread_id: id, body: body.body,
          author: { kind: 'verified_user', label: 'Floating reviewer', is_self: true }, audience: 'internal', reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [] }] };
      threads.push(thread); return route.fulfill({ json: thread });
    }
    if (path.endsWith('/threads')) return route.fulfill({ json: { items: threads, next_cursor: null } });
    const parent = threads.find(thread => path.includes(thread.thread_id));
    if (parent && method === 'POST' && !path.endsWith('/state')) {
      const body = request.postDataJSON(); replies.push(body.body);
      const entry = { ...parent.entries[0], entry_id: randomUUID(), entry_version_id: randomUUID(), body: body.body, reply_to_entry_id: parent.entries[0].entry_id };
      parent.entries.push(entry); return route.fulfill({ json: entry });
    }
    return route.fulfill({ status: 404, json: { detail: 'Unmocked fixture endpoint' } });
  });
  // Wait for the development-only dynamic fixture import, not just its shell.
  await page.goto(`/work-feedback-floating-fixture?surface=${kind}`, { waitUntil: 'networkidle' });
  await expect(page.getByRole('heading', { name: 'Shared floating feedback fixture' })).toBeVisible();
  return { source, threads, saved, replies };
}
async function activate(page: Page) {
  await page.getByRole('button', { name: 'Activate selection intent', exact: true }).click();
  await expect(page.getByRole('status', { name: '' }).filter({ hasText: 'Binding ready' })).toBeVisible();
}
async function selectText(page: Page, selector = '[data-testid="floating-native-source"]', start = 0, end = 15) {
  await page.locator(selector).evaluate((root, offsets) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); const nodes: Text[] = []; let node;
    while ((node = walker.nextNode())) nodes.push(node as Text);
    const at = (offset: number) => { for (const node of nodes) { if (offset <= node.length) return { node, offset }; offset -= node.length; } throw new Error('Selection exceeds rendered text'); };
    const a = at(offsets[0]), b = at(offsets[1]), selection = getSelection()!;
    selection.setBaseAndExtent(a.node, a.offset, b.node, b.offset);
    document.dispatchEvent(new Event('selectionchange'));
  }, [start, end]);
}
async function sourceMetrics(page: Page, selector: string) {
  return page.locator(selector).evaluateAll(nodes => nodes.map(node => {
    const box = node.getBoundingClientRect(), style = getComputedStyle(node);
    return { x: box.x, y: box.y, width: box.width, height: box.height, font: style.font, lineHeight: style.lineHeight, text: node.textContent };
  }));
}
async function registry(page: Page) {
  return page.evaluate(() => [...CSS.highlights.keys()].filter(name => name.startsWith('cognis-feedback-'))
    .map(name => ({ name, size: CSS.highlights.get(name)!.size, connected: [...CSS.highlights.get(name)!].every(range => range.startContainer.isConnected) })));
}
test('activation, native target, composing and actual thread never reflow the source', async ({ page }, info) => {
  const fixture = await setup(page);
  const selector = '[data-testid="floating-native-source"]', before = await sourceMetrics(page, selector);
  await selectText(page);
  await activate(page);
  expect(await page.evaluate(() => getSelection()?.toString())).toBe('Readable native');
  expect(await sourceMetrics(page, selector)).toEqual(before);
  await page.evaluate(() => document.dispatchEvent(new Event('selectionchange')));
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toBeFocused();
  expect(await sourceMetrics(page, selector)).toEqual(before);
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Floating exact native selection');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => fixture.saved.length).toBe(1);
  expect(fixture.saved[0]).toMatchObject({ kind: 'text', range: { start: 0, end: 15, exact: 'Readable native' } });
  await expect(page.getByRole('heading', { name: 'Thread', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /^Comments/ }).first().click();
  await expect(page.getByRole('region', { name: 'Feedback panel' })).toBeVisible();
  const card = page.getByRole('region', { name: 'Feedback panel' });
  await card.getByRole('button', { name: 'Reply', exact: true }).last().click();
  await card.getByRole('textbox', { name: 'Reply' }).last().fill('Retained thread reply');
  await page.getByRole('button', { name: 'Close comments', exact: true }).click();
  await page.getByRole('button', { name: /^Comments/ }).first().click();
  await expect(card.getByRole('textbox', { name: 'Reply' }).last()).toHaveValue('Retained thread reply');
  await page.getByRole('button', { name: 'Close comments', exact: true }).click();
  await page.locator('[data-feedback-avatar]').first().click();
  await page.getByRole('button', { name: /Reviewer 1.*Saved comment 1$/ }).click();
  await expect(page.getByRole('heading', { name: 'Thread', exact: true })).toBeVisible();
  expect(await sourceMetrics(page, selector)).toEqual(before);
  await expect(page.locator('[data-feedback-floating]')).toHaveCount(1);
  await page.screenshot({ path: info.outputPath('source-thread.png') });
});
for (const theme of ['light', 'dark']) test(`twelve overlapping comments use one underlay in ${theme} and survive syntax rerender`, async ({ page }, info) => {
  await setup(page); await page.locator('html').evaluate((root, theme) => root.classList.toggle('dark', theme === 'dark'), theme);
  await activate(page);
  await expect.poll(() => registry(page)).toEqual([expect.objectContaining({ size: 1, connected: true })]);
  await page.getByRole('button', { name: 'Rerender syntax nodes' }).click();
  await expect.poll(() => registry(page)).toEqual([expect.objectContaining({ size: 1, connected: true })]);
  expect(await page.locator('article code').evaluate(node => getComputedStyle(node).color)).toBe('rgb(32, 120, 210)');
  await selectText(page); expect(await page.evaluate(() => getSelection()?.toString())).toBe('Readable native');
  await page.screenshot({ path: info.outputPath(`overlap-${theme}.png`) });
  await page.getByRole('button', { name: 'Unmount source', exact: true }).click();
  await expect.poll(() => registry(page)).toEqual([]);
  await expect(page.locator('style[data-feedback-highlight-style]')).toHaveCount(0);
});
test('unsupported Custom Highlights use non-filling unioned outlines', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(CSS, 'highlights', { value: undefined, configurable: true }));
  await setup(page); await activate(page);
  await expect(page.locator('[data-feedback-passive-highlight]')).toHaveCount(1);
  expect(await page.locator('[data-feedback-passive-highlight]').evaluate(node => getComputedStyle(node).backgroundColor)).toBe('rgba(0, 0, 0, 0)');
  await selectText(page); expect(await page.evaluate(() => getSelection()?.toString())).toBe('Readable native');
});
test('avatar dismissal returns focus without reopening its preview', async ({ page }) => {
  await setup(page, 'text', 1); await activate(page);
  const avatar = page.locator('[data-feedback-avatar]');
  await avatar.click();
  await expect(page.getByRole('heading', { name: 'Thread', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Close comments', exact: true }).click();
  await expect(avatar).toBeFocused();
  await expect(page.locator('[data-feedback-floating]')).toHaveCount(0);
});
test('avatars keep stable positions and leave the first annotated glyph available to native drag', async ({ page }) => {
  await setup(page); await activate(page);
  const avatars = page.locator('[data-feedback-avatar]');
  await expect(avatars).toHaveCount(1);
  const before = await sourceMetrics(page, '[data-feedback-avatar]');
  for (let frame = 0; frame < 4; frame++) {
    await page.evaluate(() => new Promise<void>(resolve => {
      dispatchEvent(new Event('scroll'));
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
    expect(await sourceMetrics(page, '[data-feedback-avatar]')).toEqual(before);
  }
  const points = await page.locator('[data-testid="floating-native-source"]').evaluate(root => {
    const node = document.createTreeWalker(root, NodeFilter.SHOW_TEXT).nextNode()!;
    const range = document.createRange(); range.setStart(node, 9); range.setEnd(node, 10);
    const first = range.getBoundingClientRect(); range.setStart(node, 14); range.setEnd(node, 15);
    const last = range.getBoundingClientRect();
    return { first: { x: first.left + .1, y: first.top + first.height / 2 }, last: { x: last.right - .1, y: last.top + last.height / 2 } };
  });
  await page.mouse.move(points.first.x, points.first.y); await page.mouse.down();
  await page.mouse.move(points.last.x, points.last.y, { steps: 16 }); await page.mouse.up();
  expect(await page.evaluate(() => getSelection()?.toString())).toBe('native');
  await expect(page.getByRole('button', { name: 'Add comment', exact: true })).toBeVisible();
});
test('diff activation, keyboard range, composing and thread preserve every row and old/new evidence', async ({ page }, info) => {
  const fixture = await setup(page, 'diff');
  const selector = '[data-feedback-file] tbody tr', before = await sourceMetrics(page, selector);
  expect(before.length).toBeGreaterThan(0);
  await activate(page); await expect(page.getByRole('button', { name: 'Select new line 2', exact: true })).toBeVisible();
  expect(await sourceMetrics(page, selector)).toEqual(before);
  await page.getByRole('button', { name: 'Select new line 2', exact: true }).click();
  await page.getByRole('button', { name: 'Select new line 2', exact: true }).press('Shift+ArrowDown');
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  expect(await sourceMetrics(page, selector)).toEqual(before);
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Exact new lines');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => fixture.saved.length).toBe(1);
  expect(fixture.saved[0]).toMatchObject({ kind: 'diff', side: 'new', start_line: 2, end_line: 3, quote: 'new value\nthird line' });
  await expect(page.getByRole('heading', { name: 'Thread', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /^Comments/ }).first().click();
  await expect(page.getByRole('region', { name: 'Feedback panel' }).locator('label').getByText('Exact new lines', { exact: true })).toBeVisible();
  expect(await sourceMetrics(page, selector)).toEqual(before);
  await page.screenshot({ path: info.outputPath('diff-density.png') });
});
test('PDF retained target stays outlined and visible at actual 76% without resizing source', async ({ page }, info) => {
  await setup(page, 'pdf');
  const zoom = page.getByRole('spinbutton', { name: 'Zoom percentage' });
  for (const value of ['50', '76']) { await zoom.fill(value); await zoom.press('Tab'); }
  const canvas = page.getByLabel('PDF page 1', { exact: true });
  await expect(canvas).toHaveAttribute('aria-busy', 'false');
  await expect.poll(async () => (await canvas.boundingBox())!.width).toBeCloseTo(595.28 * .76 * 96 / 72, 1);
  const before = await sourceMetrics(page, 'canvas[aria-label="PDF page 1"]');
  await activate(page); expect(await sourceMetrics(page, 'canvas[aria-label="PDF page 1"]')).toEqual(before);
  await selectText(page, '.textLayer');
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('PDF quote stays visible');
  expect(await sourceMetrics(page, 'canvas[aria-label="PDF page 1"]')).toEqual(before);
  await expect(page.locator('[data-feedback-target-highlight]')).toHaveCount(1);
  expect(await page.locator('[data-feedback-target-highlight]').evaluate(node => getComputedStyle(node).backgroundColor)).toBe('rgba(0, 0, 0, 0)');
  const visible = await page.locator('.textLayer').evaluate(root => {
    const node = root.querySelector('span')!.firstChild!, range = document.createRange();
    range.setStart(node, 14); range.setEnd(node, 15); const end = range.getBoundingClientRect();
    const card = document.querySelector('[data-feedback-floating]')!.getBoundingClientRect();
    const point = document.elementFromPoint(end.x + end.width / 2, end.y + end.height / 2);
    return { endVisible: !!point && root.contains(point),
      overlap: card.left < end.right && card.right > end.left && card.top < end.bottom && card.bottom > end.top };
  });
  expect(visible).toEqual({ endVisible: true, overlap: false });
  await page.screenshot({ path: info.outputPath('pdf-retained-outline.png') });
});
for (const pointer of ['mouse', 'touch'] as const) test(`continuous ${pointer} range adjustment keeps the dense diff and exact side`, async ({ page, context }) => {
  const fixture = await setup(page, 'diff');
  const selector = '[data-feedback-file] tbody tr', before = await sourceMetrics(page, selector), side = pointer === 'mouse' ? 'new' : 'old';
  await activate(page);
  await page.getByRole('button', { name: `Select ${side} line 2`, exact: true }).click();
  const handle = page.getByRole('button', { name: 'Adjust selected line range' });
  const box = (await handle.boundingBox())!;
  expect([box.width, box.height]).toEqual([44, 44]);
  const positions = await page.locator(`[data-feedback-${side}]`).evaluateAll((cells, side) => cells.filter(cell => ['3', '4'].includes(cell.getAttribute(`data-feedback-${side}`)!))
    .map(cell => { const box = cell.getBoundingClientRect(); return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; }), side);
  const client = pointer === 'touch' ? await context.newCDPSession(page) : null;
  if (client) await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x + 22, y: box.y + 22, id: 1 }] });
  else { await page.mouse.move(box.x + 22, box.y + 22); await page.mouse.down(); }
  for (const [index, point] of positions.entries()) {
    if (client) await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ ...point, id: 1 }] });
    else await page.mouse.move(point.x, point.y);
    await expect(page.locator('[data-feedback-target-highlight]')).toHaveCount(index + 2);
  }
  if (client) { await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }); await client.detach(); }
  else await page.mouse.up();
  expect(await sourceMetrics(page, selector)).toEqual(before);
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Adjusted continuous range');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => fixture.saved.length).toBe(1);
  expect(fixture.saved[0]).toMatchObject({ kind: 'diff', side, start_line: 2, end_line: 4,
    quote: `${side === 'new' ? 'new' : 'old'} value\nthird line\nfourth line` });
});
test('narrow keyboard-sized viewport exposes the target once, then docks without chasing user scroll', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page); await activate(page); await selectText(page);
  const before = await sourceMetrics(page, '[data-testid="floating-native-source"]');
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  const input = page.getByRole('textbox', { name: 'Comment', exact: true });
  await input.fill('Preserve my narrow draft');
  await page.setViewportSize({ width: 390, height: 480 });
  const visible = () => page.locator('[data-testid="floating-native-source"]').evaluate(root => {
    const range = document.createRange(); range.selectNodeContents(root);
    const text = range.getBoundingClientRect(), card = document.querySelector('[data-feedback-floating]')!.getBoundingClientRect();
    return { covered: card.left < text.right && card.right > text.left && card.top < text.bottom && card.bottom > text.top,
      bounded: card.left >= 0 && card.top >= 0 && card.right <= innerWidth && card.bottom <= innerHeight };
  });
  await expect.poll(visible).toEqual({ covered: false, bounded: true });
  await expect(input).toBeFocused();
  const after = await sourceMetrics(page, '[data-testid="floating-native-source"]');
  expect(after.map(({ width, height, font, lineHeight }) => ({ width, height, font, lineHeight })))
    .toEqual(before.map(({ width, height, font, lineHeight }) => ({ width, height, font, lineHeight })));
  await page.locator('main').evaluate(root => root.scrollTop = 600);
  await expect(page.getByRole('button', { name: 'Back to target', exact: true })).toBeVisible();
  expect(await page.locator('main').evaluate(root => root.scrollTop)).toBe(600);
  await page.getByRole('button', { name: 'Back to target', exact: true }).click();
  await page.getByRole('button', { name: 'Resume draft', exact: true }).click();
  await expect(input).toHaveValue('Preserve my narrow draft');
});
test('normal HTML frame source keeps its viewport and non-filling retained target', async ({ page }) => {
  await setup(page, 'html');
  const frame = page.frameLocator('iframe');
  await expect(frame.locator('p')).toHaveText(text);
  const before = await sourceMetrics(page, 'iframe');
  await activate(page);
  await frame.locator('p').evaluate(root => {
    const selection = getSelection()!; selection.setBaseAndExtent(root.firstChild!, 0, root.firstChild!, 15);
    document.dispatchEvent(new Event('selectionchange'));
  });
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toBeFocused();
  expect(await sourceMetrics(page, 'iframe')).toEqual(before);
  await expect(page.locator('[data-feedback-target-highlight]')).toHaveCount(1);
  expect(await page.locator('[data-feedback-target-highlight]').evaluate(node => getComputedStyle(node).backgroundColor)).toBe('rgba(0, 0, 0, 0)');
});
