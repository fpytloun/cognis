import { createHash, randomUUID } from 'node:crypto';
import { expect, test, type Locator, type Page } from '@playwright/test';
import type { FeedbackAnchor, FeedbackLocator, FeedbackThread, ResolvedFeedbackSource } from '../src/lib/feedback/types';

const text = 'Repeat this sentence. Repeat this sentence. Native selection remains precise.';
const descriptor = (value: string) => ({ representation: 'feedback-text-v1' as const, text_digest: createHash('sha256').update(value).digest('hex'), length: value.length, available_ranges: [[0, value.length] as [number, number]] });
async function fixture(page: Page, format = 'text', atEnd = false, shell = false, retained = false) {
  const sourceText = atEnd ? Array.from({ length: 35 }, (_, index) => `Source paragraph ${index + 1}.`).join('\n') + '\n' + text : text;
  page.on('pageerror', error => console.error(`Local inline fixture: ${error.message}`));
  page.on('console', message => { if (message.type() === 'error') console.error(`Local inline fixture console: ${message.text()}`); });
  const source: ResolvedFeedbackSource = {
    subject_id: randomUUID(), source_token: randomUUID(), source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
    content_digest: createHash('sha256').update(`${format}:${sourceText}`).digest('hex'), context: { kind: 'standalone' },
    availability: 'available', permitted_locator_kinds: ['whole', format === 'text' ? 'text' : format === 'rich' ? 'rich' : format === 'html' ? 'html' : 'image'],
    lineage_id: null, revision: 1, latest_content_ref: null, title: 'Synthetic local source', format,
    capabilities: { read: true, comment: true, dispatch: false, moderate: false, disclose: false }, canonical_text: format === 'text' ? descriptor(sourceText) : null,
  };
  const threads: FeedbackThread[] = [], saved: FeedbackLocator[] = [];
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') return route.fulfill({ json: { email: 'inline-review@example.test', display_name: 'Inline reviewer', is_admin: false, is_active: true } });
    if (path.endsWith('/sources/resolve')) return route.fulfill({ json: source });
    if (path.includes('/drafts/')) return route.fulfill(request.method() === 'PUT' ? { json: { version: 1 } } : { status: 404, json: { detail: 'No remote draft' } });
    if (path.endsWith('/sources/text')) {
      const body = request.postDataJSON(), value = body.scope.field_path?.includes('values') ? 'Alpha' : sourceText;
      const start = body.start ?? 0, end = Math.min(value.length, start + body.limit), proof = descriptor(value);
      return route.fulfill({ json: { scope: body.scope, content_digest: source.content_digest, descriptor: proof, text_page: { ...proof, start, end, total_length: value.length, text: value.slice(start, end) } } });
    }
    if (path.endsWith('/threads') && request.method() === 'POST') {
      const body = request.postDataJSON() as { anchor: FeedbackAnchor; body: string };
      saved.push(body.anchor.locator);
      const thread: FeedbackThread = { thread_id: randomUUID(), subject_id: source.subject_id, version: 1, state: 'open', hidden: false, audience: 'internal',
        anchor: { schema_version: 1, anchor_id: randomUUID(), source: source.source, locator: body.anchor.locator, content_digest: source.content_digest },
        entries: [] };
      thread.entries = [{ entry_id: randomUUID(), entry_version_id: randomUUID(), version: 1, thread_id: thread.thread_id, body: body.body,
        author: { kind: 'verified_user', label: 'Inline reviewer', is_self: true }, audience: 'internal', reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [] }];
      threads.push(thread); return route.fulfill({ json: thread });
    }
    if (path.endsWith('/threads')) return route.fulfill({ json: { items: threads, next_cursor: null } });
    return route.fulfill({ status: 404, json: { detail: 'Local fixture endpoint unavailable' } });
  });
  await page.goto(`/work-feedback-inline-fixture?surface=${format}${atEnd ? '&end=1' : ''}${shell ? '&shell=1' : ''}${retained ? '&retained=1' : ''}`);
  await page.getByRole('button', { name: 'Annotate', pressed: false }).first().click();
  await expect(page.getByRole('button', { name: 'Comment on source', exact: true })).toBeVisible();
  const coarse = await page.evaluate(() => matchMedia('(pointer: coarse)').matches);
  expect((await page.getByRole('button', { name: 'Annotate' }).first().boundingBox())!.height).toBeGreaterThanOrEqual(coarse ? 44 : 32);
  return { saved, sourceText, threads };
}
async function reachable(locator: Locator) {
  await expect.poll(() => locator.evaluate(element => {
    const box = element.getBoundingClientRect(), x = box.left + box.width / 2, y = box.top + box.height / 2;
    return [[x, y], [x, box.top + 2], [x, box.bottom - 2], [box.left + 2, y], [box.right - 2, y]]
      .every(([x, y]) => element.contains(document.elementFromPoint(x, y)));
  })).toBe(true);
}
async function nativeRange(page: Page, start: number, end: number) {
  await page.getByTestId('native-text').evaluate((node, offsets) => {
    const selection = document.getSelection()!;
    selection.setBaseAndExtent(node.firstChild!, offsets[0], node.firstChild!, offsets[1]);
    document.dispatchEvent(new Event('selectionchange'));
  }, [start, end]);
}
test('retained hidden parent suspends its toolbar and composer without losing the draft', async ({ page }) => {
  const { sourceText } = await fixture(page, 'text', true, false, true);
  const hide = () => page.getByRole('button', { name: 'Suspend retained source', exact: true }).click();
  const show = () => page.getByRole('button', { name: 'Restore retained source', exact: true }).click();
  const tools = page.getByRole('button', { name: 'Comment on source', exact: true });
  await hide(); await expect(tools).not.toBeVisible();
  await show(); await expect(tools).toBeVisible();
  await page.locator('main').evaluate(node => { node.scrollTop = node.scrollHeight; });
  await nativeRange(page, sourceText.lastIndexOf('Native'), sourceText.length);
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  const comment = page.getByRole('textbox', { name: 'Comment', exact: true });
  await comment.fill('Retained parent draft');
  await hide(); await expect(comment).not.toBeVisible();
  await expect(page.locator('[data-feedback-layout-reservation]')).toHaveCount(0);
  const child = page.getByRole('textbox', { name: 'Child workstream input', exact: true });
  await child.fill('Child stays active'); await page.keyboard.press('Escape');
  await expect(child).toBeFocused();
  await show();
  await expect(comment).toHaveValue('Retained parent draft');
  await expect(page.getByRole('region', { name: 'Add comment', exact: true })).toBeVisible();
  await reachable(page.getByRole('button', { name: 'Save comment', exact: true }));
  await page.getByRole('button', { name: 'Close and preserve draft' }).click();
  await page.getByRole('button', { name: 'Resume draft', exact: true }).click();
  await expect(comment).toHaveValue('Retained parent draft');
});
test('real native drag targets the second repeated sentence, saves once, and preserves drafts on close', async ({ page }, testInfo) => {
  const { saved } = await fixture(page);
  const node = page.getByTestId('native-text');
  await node.click(); await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: 'Add comment', exact: true })).toHaveCount(0);
  const points = await node.evaluate(element => {
    const at = (offset: number) => { const range = document.createRange(); range.setStart(element.firstChild!, offset); range.setEnd(element.firstChild!, offset); const box = range.getBoundingClientRect(); return { x: box.left, y: box.top + box.height / 2 }; };
    return [at(22), at(42)];
  });
  await page.mouse.move(points[0].x, points[0].y); await page.mouse.down();
  await page.mouse.move(points[1].x, points[1].y, { steps: 15 }); await page.mouse.up();
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Precise repeated sentence');
  await page.screenshot({ path: testInfo.outputPath('desktop-contextual-composer.png') });
  await page.getByRole('button', { name: 'Close and preserve draft' }).click();
  await page.getByRole('button', { name: 'Resume draft' }).click();
  await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toHaveValue('Precise repeated sentence');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toMatchObject({ kind: 'text', range: { start: 22, end: 42, exact: 'Repeat this sentence' } });
  await expect(page.getByRole('region', { name: 'Comment preview' })).toContainText('Precise repeated sentence');
});
test('sorted numeric Cell/Row/Table targets retain the authored row path and reopen it', async ({ page }) => {
  const { saved } = await fixture(page, 'rich');
  await page.getByRole('button', { name: 'Select element', exact: true }).click();
  await page.getByRole('button', { name: /Station/ }).click();
  await page.getByRole('gridcell', { name: '4', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Cell', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Table', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Row', exact: true }).click();
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Authored Alpha row');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toMatchObject({ kind: 'rich', block_id: 'station-status', item_key: null, field_path: ['rows', '1'], range: null });
  await page.getByRole('button', { name: /^Feedback/ }).click();
  await page.getByRole('button', { name: 'Show target', exact: true }).click();
  await page.getByRole('button', { name: 'Close comments', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Comment preview' })).toContainText('Authored Alpha row');
});
test('HTML text tool never creates an element from click; explicit element starts at cell', async ({ page }) => {
  const { saved } = await fixture(page, 'html');
  const frame = page.frameLocator('iframe');
  expect(await page.locator('iframe').getAttribute('sandbox')).not.toContain('allow-same-origin');
  await frame.getByRole('heading', { name: 'Observed source' }).click();
  await expect(page.getByRole('button', { name: 'Add comment', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Select element', exact: true }).click();
  await frame.getByRole('cell', { name: '4', exact: true }).click();
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Observed cell only');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toMatchObject({ kind: 'html', range: null });
  expect(saved[0].kind === 'html' && saved[0].selector_path).toContain('td:nth-of-type(2)');
  await page.getByRole('button', { name: 'Annotate', pressed: true }).first().click();
  await frame.getByRole('link', { name: 'Follow link' }).click();
  await expect(frame.getByText('Destination', { exact: true })).toBeInViewport();
});
test('a saved preview cannot compete with a new same-source HTML target after a tool switch', async ({ page }) => {
  const { saved, threads } = await fixture(page, 'html');
  const heading = page.frameLocator('iframe').getByRole('heading', { name: 'Observed source', exact: true });
  await heading.evaluate(element => {
    const selection = document.getSelection()!, range = document.createRange();
    range.selectNodeContents(element); selection.removeAllRanges(); selection.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
  });
  await page.getByRole('button', { name: 'Add comment', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Saved heading text');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Comment preview' })).toContainText('Saved heading text');
  await page.getByRole('button', { name: 'Close preview', exact: true }).click();
  await page.getByRole('button', { name: 'Annotate', pressed: true }).first().click();
  await page.getByRole('button', { name: 'Annotate', pressed: false }).first().click();
  await page.getByRole('button', { name: 'Select element', exact: true }).click();
  await page.getByRole('button', { name: /^Feedback/ }).click();
  await page.getByRole('button', { name: 'Show target', exact: true }).click();
  await page.getByRole('button', { name: 'Close comments', exact: true }).click();
  await heading.click({ position: { x: 20, y: 18 } });
  const add = page.getByRole('button', { name: 'Add comment', exact: true });
  await expect(add).toBeVisible();
  await expect(page.getByRole('region', { name: 'Comment preview' })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('html-new-target-no-preview.png') });
  await reachable(add); await add.click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('New heading element');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(2);
  expect(saved[0]).toMatchObject({ kind: 'html', range: { exact: 'Observed source' } });
  expect(saved[1]).toMatchObject({ kind: 'html', range: null });
  expect(threads[0].entries[0].body).toBe('Saved heading text');
});
for (const width of [390, 900]) test.describe(`real bottom app chrome at ${width}px`, () => {
  test.use({ viewport: { width, height: 844 }, hasTouch: true });
  test('app chrome leaves normal Resume draft and Save touch targets reachable', async ({ page }) => {
    const { saved, sourceText } = await fixture(page, 'text', true, true);
    const nav = page.getByRole('navigation', { name: 'Primary', exact: true });
    await expect(nav).toBeVisible();
    await page.locator('main').evaluate(element => { element.scrollTop = element.scrollHeight; });
    const start = sourceText.lastIndexOf('Native');
    await nativeRange(page, start, sourceText.length);
    await page.getByRole('button', { name: 'Add comment', exact: true }).tap();
    const input = page.getByRole('textbox', { name: 'Comment', exact: true });
    await input.fill('Draft above real bottom navigation');
    await page.getByRole('button', { name: 'Close and preserve draft' }).tap();
    const resume = page.getByRole('button', { name: 'Resume draft', exact: true });
    await reachable(resume);
    await page.screenshot({ path: test.info().outputPath('resume-above-navigation.png') });
    await resume.tap();
    await expect(input).toHaveValue('Draft above real bottom navigation');
    const save = page.getByRole('button', { name: 'Save comment', exact: true });
    await reachable(save);
    await page.screenshot({ path: test.info().outputPath('composer-above-navigation.png') });
    await page.evaluate(() => { Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 430 }); window.visualViewport!.dispatchEvent(new Event('resize')); });
    await reachable(save);
    await expect.poll(async () => {
      const targets = await page.locator('[data-feedback-target-highlight]').evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().toJSON()));
      const composer = await page.getByRole('region', { name: 'Add comment', exact: true }).boundingBox();
      return !!composer && targets.length > 0 && targets.every(target => target.top >= 0 && target.bottom < composer.y);
    }).toBe(true);
    await page.screenshot({ path: test.info().outputPath('composer-keyboard-source.png') });
    await page.locator('[data-feedback-source-viewport]').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await page.evaluate(() => { Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 844 }); window.visualViewport!.dispatchEvent(new Event('resize')); });
    await expect(nav).toBeVisible(); await reachable(save); await save.tap();
    await expect.poll(() => saved.length).toBe(1);
    await expect(page.locator('[data-feedback-layout-reservation]')).toHaveCount(0);
    if (width === 900) {
      await page.setViewportSize({ width: 1100, height: 844 });
      await expect(nav).not.toBeVisible();
      await reachable(page.getByRole('button', { name: 'Comment on source', exact: true }));
      await page.setViewportSize({ width, height: 844 });
      await expect(nav).toBeVisible();
    }
    await page.goto('/work-feedback-inline-fixture?surface=text&end=1');
    await expect(nav).toHaveCount(0);
    await page.getByRole('button', { name: 'Annotate', pressed: false }).first().click();
    const source = page.getByRole('button', { name: 'Comment on source', exact: true });
    await reachable(source);
    expect((await source.boundingBox())!.y + (await source.boundingBox())!.height).toBeGreaterThan(820);
  });
});
test('keyboard-only element targeting reaches numeric table cells and saves the authored cell', async ({ page }) => {
  const { saved } = await fixture(page, 'rich');
  expect(await page.getByRole('cell', { name: '4', exact: true }).getAttribute('tabindex')).toBeNull();
  await page.getByRole('button', { name: 'Select element', exact: true }).click();
  const cell = page.getByRole('gridcell', { name: '4', exact: true });
  await cell.focus(); await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: 'Cell', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Add comment', exact: true }).focus(); await page.keyboard.press('Enter');
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Keyboard-selected numeric cell');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toMatchObject({ kind: 'rich', block_id: 'station-status', field_path: ['rows', '1', 'values', '1'], range: null });
});
test.describe('emulated touch viewport (not native-device acceptance)', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });
  test('native range changes update the target; keyboard viewport leaves source visible and actions reachable', async ({ page }, testInfo) => {
    const { saved, sourceText } = await fixture(page, 'text', true);
    await page.locator('main').evaluate(element => { element.scrollTop = element.scrollHeight; });
    expect(await page.locator('main').evaluate(element => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThanOrEqual(1);
    const start = sourceText.lastIndexOf('Native');
    await nativeRange(page, sourceText.length - 1, start);
    await expect(page.getByRole('button', { name: 'Add comment', exact: true })).toBeVisible();
    await nativeRange(page, sourceText.length, start);
    await page.getByRole('button', { name: 'Add comment', exact: true }).tap();
    await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Touch range');
    await page.evaluate(() => {
      Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 430 });
      window.visualViewport!.dispatchEvent(new Event('resize'));
    });
    const composer = page.getByRole('region', { name: 'Add comment', exact: true });
    const box = await composer.boundingBox();
    expect(box!.height).toBeLessThanOrEqual(430 * 0.46 + 1);
    expect(box!.y).toBeGreaterThan(40);
    await expect.poll(async () => {
      const target = await page.locator('[data-feedback-target-highlight]').last().boundingBox();
      const composer = await page.getByRole('region', { name: 'Add comment', exact: true }).boundingBox();
      return !!target && !!composer && target.y + target.height <= composer.y - 10;
    }).toBe(true);
    await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toBeFocused();
    await expect(page.locator('[data-feedback-layout-reservation]')).toHaveCount(1);
    await expect.poll(async () => {
      const card = await composer.boundingBox(), save = await composer.getByRole('button', { name: 'Save comment', exact: true }).boundingBox();
      return !!card && !!save && save.height >= 44 && save.y >= card.y && save.y + save.height <= card.y + card.height;
    }).toBe(true);
    const sourceViewport = page.locator('[data-feedback-source-viewport]');
    await sourceViewport.evaluate(element => { element.scrollTop = element.scrollHeight; });
    const visibleTarget = async () => {
      const boxes = await page.locator('[data-feedback-target-highlight]').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().toJSON()));
      const box = await composer.boundingBox();
      return boxes.length > 0 && boxes.every(target => target.top >= 0 && target.bottom <= box!.y - 10);
    };
    await expect.poll(visibleTarget).toBe(true);
    await page.evaluate(() => { Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 844 }); window.visualViewport!.dispatchEvent(new Event('resize')); });
    await expect.poll(visibleTarget).toBe(true);
    await composer.getByRole('button', { name: 'Close and preserve draft' }).tap();
    await expect(page.locator('[data-feedback-layout-reservation]')).toHaveCount(0);
    expect(await sourceViewport.evaluate(element => element.style.height)).toBe('');
    await page.getByRole('button', { name: 'Resume draft' }).tap();
    await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toHaveValue('Touch range');
    await page.evaluate(() => { Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 430 }); window.visualViewport!.dispatchEvent(new Event('resize')); });
    await expect.poll(visibleTarget).toBe(true);
    await expect(page.getByRole('textbox', { name: 'Comment', exact: true })).toBeFocused();
    await page.screenshot({ path: testInfo.outputPath('touch-keyboard-composer.png') });
    await composer.getByRole('button', { name: 'Save comment', exact: true }).tap();
    await expect.poll(() => saved.length).toBe(1);
    expect(saved[0]).toMatchObject({ kind: 'text', range: { start, end: sourceText.length, exact: 'Native selection remains precise.' } });
    await expect(page.locator('[data-feedback-layout-reservation]')).toHaveCount(0);
  });
  test('the final table cell remains visible at the true end after keyboard occlusion', async ({ page }) => {
    await fixture(page, 'rich');
    await page.locator('main').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await page.getByRole('button', { name: 'Select element', exact: true }).tap();
    await page.getByRole('gridcell', { name: 'Count 5', exact: true }).tap();
    await page.getByRole('button', { name: 'Add comment', exact: true }).tap();
    const input = page.getByRole('textbox', { name: 'Comment', exact: true });
    await input.fill('Final authored cell');
    await page.evaluate(() => { Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 430 }); window.visualViewport!.dispatchEvent(new Event('resize')); });
    const source = page.locator('[data-feedback-source-viewport]');
    await expect.poll(async () => {
      const cell = await page.getByRole('gridcell', { name: 'Count 5', exact: true }).boundingBox();
      const composer = await page.getByRole('region', { name: 'Add comment', exact: true }).boundingBox();
      return !!cell && !!composer && cell.y >= 0 && cell.y + cell.height <= composer.y - 10;
    }).toBe(true);
    await source.evaluate(element => { element.scrollTop = element.scrollHeight; });
    expect(await source.evaluate(element => element.scrollHeight - element.clientHeight - element.scrollTop)).toBeLessThanOrEqual(1);
    await expect(input).toBeFocused();
    await page.getByRole('button', { name: 'Close and preserve draft' }).tap();
    await expect(page.locator('[data-feedback-layout-reservation]')).toHaveCount(0);
    await page.getByRole('button', { name: 'Resume draft' }).tap();
    await expect(input).toHaveValue('Final authored cell');
  });
});
