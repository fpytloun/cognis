import { expect, test, type Locator, type Page } from '@playwright/test';
import { createHash, randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { history, resolved as workResolved } from '../src/lib/components/work/work-event-feedback.fixture';
import type { FeedbackAnchor, FeedbackContext, FeedbackSourceRef, FeedbackThread, ResolvedFeedbackSource } from '../src/lib/feedback/types';

const quote = 'Prepare the staging deployment plan.';
const text = `Release review — synthetic fixture\n\n🚀 ${quote}\nKeep the current production deployment unchanged.\nConfirm backup evidence and document the rollback command.\n`;
const digest = createHash('sha256').update(text).digest('hex');
async function setup(page: Page, mode = 'reader', count = 3, shell = true) {
  page.on('pageerror', error => console.error(`Native acceptance fixture: ${error.message}`));
  const ref: FeedbackSourceRef = { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' };
  const saved: { anchor: FeedbackAnchor; body: string }[] = [], threads: FeedbackThread[] = [], resolutions: FeedbackSourceRef[] = [];
  const resolvedSources = new Map<string, FeedbackSourceRef>();
  const authRequests: string[] = [];
  const user = { email: 'inline-review@example.test', display_name: 'Inline reviewer', is_admin: false, is_active: true };
  const source = (ref: FeedbackSourceRef, context: FeedbackContext | null): ResolvedFeedbackSource => ref.kind === 'work_diff' ? workResolved(ref) : ({
    source: ref, context: context ?? { kind: 'standalone' }, subject_id: `native-${ref.kind === 'transcript' ? ref.source_item_id : 'document'}`, source_token: 'native-source-token',
    content_digest: digest, availability: 'available', format: 'text', lineage_id: null, revision: 1,
    latest_content_ref: null, title: 'release-review.txt', permitted_locator_kinds: ['whole', 'text'],
    capabilities: { read: true, comment: true, moderate: false, dispatch: true, disclose: false },
    canonical_text: { representation: 'feedback-text-v1', text_digest: digest, length: text.length, available_ranges: [[0, text.length]] },
  });
  function thread(payload: { anchor: FeedbackAnchor; body: string }, ref: FeedbackSourceRef): FeedbackThread {
    const thread_id = randomUUID();
    return { thread_id, subject_id: source(ref, null).subject_id, version: 1, state: 'open', hidden: false, audience: 'internal',
      anchor: { schema_version: 1, anchor_id: randomUUID(), source: ref, locator: payload.anchor.locator, content_digest: payload.anchor.content_digest },
      entries: [{ entry_id: randomUUID(), entry_version_id: randomUUID(), thread_id, version: 1, body: payload.body,
        audience: 'internal', created_at: '2026-01-12T12:00:00Z',
        author: { kind: 'verified_user', label: 'Reviewer', is_self: true }, reply_to_entry_id: null, delivery: [] }] };
  }
  const start = text.indexOf(quote);
  for (let index = 1; index <= count; index++) threads.push(thread({
    body: `Existing comment ${index}. ${'Review this release instruction. '.repeat(12)}`,
    anchor: { schema_version: 1, source_token: 'native-source-token', content_digest: digest, locator: { kind: 'text',
      range: { representation: 'feedback-text-v1', text_digest: digest, start, end: start + quote.length, exact: quote, prefix: '', suffix: '' } } },
  }, ref));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname, body = route.request().postDataJSON();
    if (path.includes('/auth/')) authRequests.push(`${route.request().method()} ${path}`);
    if (!path.startsWith('/api/')) return route.continue();
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (path.endsWith('/auth/me')) return json(user);
    if (path.includes('/artifacts/') && path.endsWith('/text-preview')) return json({ content: text, truncated: false });
    if (path.includes('/artifacts/') && path.endsWith('/revisions')) return json({ revisions: [] });
    if (path.includes('/artifacts/') && path.endsWith('/signed-url')) return json({ url: `data:text/plain,${encodeURIComponent(text)}` });
    if (path.endsWith('/work/file-history')) return json(history());
    if (path.endsWith('/sources/resolve')) {
      resolutions.push(body.source); const resolved = source(body.source, body.context);
      resolvedSources.set(resolved.source_token, resolved.source); return json(resolved);
    }
    if (path.includes('/drafts/')) return json(route.request().method() === 'PUT' ? { version: 1 } : { detail: 'No remote draft' }, route.request().method() === 'PUT' ? 200 : 404);
    if (path.endsWith('/threads') && route.request().method() === 'GET') return json({ items: mode === 'embedded' ? [] : threads, next_cursor: null });
    if (path.endsWith('/sources/text')) return json({ scope: body.scope, content_digest: digest,
      descriptor: { representation: 'feedback-text-v1', text_digest: digest, length: text.length, available_ranges: [[0, text.length]] },
      text_page: { representation: 'feedback-text-v1', text_digest: digest, start: 0, end: text.length, total_length: text.length, text } });
    if (path.endsWith('/feedback/threads') && route.request().method() === 'POST') {
      const ref = resolvedSources.get(body.anchor.source_token);
      if (!ref) throw new Error('Save did not use a resolved source token');
      saved.push(body); const value = thread(body, ref); threads.push(value); return json(value, 201);
    }
    return json({ detail: 'Not found' }, 404);
  });
  await page.goto(`/work-feedback-inline-fixture?native-layout=${mode}${shell ? '&shell=1' : ''}`);
  await expect(page.getByText('Library · native acceptance fixture', { exact: true })).toBeVisible();
  const authority = await page.evaluate(async () => {
    const coordinationPath = '/src/lib/document-coordination.ts', authPath = '/src/lib/stores/auth.ts';
    const { documentCoordination } = await import(coordinationPath) as typeof import('../src/lib/document-coordination');
    const { auth } = await import(authPath) as typeof import('../src/lib/stores/auth');
    let status = '', failure: string | null = null;
    const unsubscribe = auth.subscribe(value => { status = value.status; failure = value.error; }); unsubscribe();
    const owner = documentCoordination();
    try { owner.capture().assertCurrent(); return { current: true, status, supported: owner.supported }; }
    catch (error) { return { current: false, status, failure, supported: owner.supported, error: error instanceof Error ? error.message : String(error) }; }
  });
  expect(authority.current, `Fixture AUTH PRECONDITION BLOCKED: ${JSON.stringify({ ...authority, authRequests })}`).toBe(true);
  if (mode === 'reader') await expect(page.locator('code').first()).toContainText(quote);
  return { saved, threads, resolutions };
}
async function reachable(locator: Locator) {
  await expect.poll(() => locator.evaluate(node => {
    const box = node.getBoundingClientRect();
    return box.width >= 44 && box.height >= 44 && [[.5, .5], [.05, .5], [.95, .5], [.5, .05], [.5, .95]].every(([x, y]) => {
      const hit = document.elementFromPoint(box.left + box.width * x, box.top + box.height * y);
      return hit === node || !!hit && node.contains(hit);
    });
  })).toBe(true);
}
test('native forward and backward drags cross three existing endpoint annotations', async ({ page }) => {
  await setup(page);
  await page.getByRole('button', { name: 'Annotate', pressed: false }).first().click();
  const pin = page.getByRole('button', { name: /^Comment 3:/ });
  await reachable(pin);
  const points = await page.locator('code').first().evaluate((node, quote) => {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT), nodes: Text[] = [];
    while (walker.nextNode()) nodes.push(walker.currentNode as Text);
    const content = nodes.map(node => node.data).join(''), start = content.indexOf(quote);
    function glyph(index: number) {
      let offset = index;
      for (const node of nodes) {
        if (offset < node.length) { const range = document.createRange(); range.setStart(node, offset); range.setEnd(node, offset + 1); return range.getBoundingClientRect().toJSON(); }
        offset -= node.length;
      }
      throw new Error('Missing source glyph');
    }
    return { first: glyph(start), middle: glyph(start + 17), last: glyph(start + quote.length - 1) };
  }, quote);
  for (const box of Object.values(points)) {
    const tag = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('button')?.tagName,
      { x: box.x + box.width / 2, y: box.y + box.height / 2 });
    expect(tag).toBeUndefined();
  }
  for (const backwards of [false, true]) {
    const first = { x: points.first.x + .2, y: points.first.y + points.first.height / 2 };
    const last = { x: points.last.x + points.last.width - .2, y: points.last.y + points.last.height / 2 };
    const start = backwards ? last : first, end = backwards ? first : last;
    await page.mouse.move(start.x, start.y); await page.mouse.down();
    await page.mouse.move(end.x, end.y, { steps: 32 }); await page.mouse.up();
    await expect.poll(() => page.evaluate(() => window.getSelection()?.toString())).toBe(quote);
  }
  for (let number = 1; number <= 3; number++) {
    const button = page.getByRole('button', { name: new RegExp(`^Comment ${number}:`) });
    await reachable(button); await button.click();
    await expect(page.getByRole('region', { name: 'Comment preview' })).toBeVisible();
    await page.getByRole('button', { name: 'Close preview', exact: true }).click();
  }
});
test.describe('mobile reader comments', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
  test('long docked comments converge across frames and close/reopen normally', async ({ page }, info) => {
    await setup(page, 'reader', 25);
    await page.getByRole('button', { name: 'Annotate', pressed: false }).first().click();
    await page.getByRole('button', { name: 'Comment on source', exact: true }).tap();
    await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('New reader comment');
    const save = page.getByRole('button', { name: 'Save comment', exact: true });
    await info.attach('save-hit-geometry', { contentType: 'application/json', body: JSON.stringify(await save.evaluate(node => {
      const box = node.getBoundingClientRect();
      return { box: box.toJSON(), hit: document.elementsFromPoint(box.x + box.width / 2, box.y + box.height / 2).map(value => `${value.tagName}:${value.getAttribute('aria-label') ?? ''}`) };
    })) });
    await reachable(save);
    await page.getByRole('button', { name: 'Save comment', exact: true }).tap();
    await page.getByRole('button', { name: 'Close preview', exact: true }).tap();
    for (let repeat = 0; repeat < 2; repeat++) {
      if (!repeat) await page.getByRole('button', { name: /more source comments$/ }).tap();
      else await page.getByRole('button', { name: /^Feedback/ }).tap();
      const close = page.getByRole('button', { name: 'Close comments', exact: true });
      await expect(close).toBeVisible();
      const frames = await close.evaluate(async node => {
        const panel = node.closest('[aria-label="Feedback panel"]')!;
        const measure = (element: Element) => {
          const { x, y, width, height } = element.getBoundingClientRect();
          return { x, y, width, height };
        };
        const samples: Record<'header' | 'close' | 'review', ReturnType<typeof measure>>[] = [];
        for (let frame = 0; frame < 25; frame++) {
          await new Promise(requestAnimationFrame);
          samples.push({ header: measure(panel.querySelector('header')!), close: measure(node), review: measure(panel.querySelector('footer button')!) });
        }
        return samples;
      });
      const trace = info.outputPath(`comments-frames-${repeat}.json`);
      await writeFile(trace, JSON.stringify(frames, null, 2));
      await info.attach(`comments-frames-${repeat}`, { path: trace, contentType: 'application/json' });
      for (const target of ['header', 'close', 'review'] as const) {
        for (const key of ['x', 'y', 'width', 'height'] as const) expect(Math.max(...frames.slice(4).map(box => box[target][key])) - Math.min(...frames.slice(4).map(box => box[target][key]))).toBeLessThan(.5);
      }
      await reachable(close); await reachable(page.getByRole('button', { name: /^Review selected/ }));
      await page.screenshot({ path: info.outputPath(`reader-comments-${repeat}.png`) });
      await close.tap(); await expect(close).toHaveCount(0);
    }
    await page.goto('/work-feedback-inline-fixture?native-layout=reader');
    await expect(page.getByRole('navigation', { name: 'Primary' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Annotate', pressed: false }).first().click();
    await reachable(page.getByRole('button', { name: 'Comment on source', exact: true }));
  });
});
test('compact transcript ownership preserves separate message sources', async ({ page }) => {
  const { resolutions } = await setup(page, 'embedded', 0);
  const controls = page.getByRole('button', { name: 'Comment on this message', exact: true });
  await expect(controls).toHaveCount(2); await expect(page.getByRole('button', { name: 'Annotate' })).toHaveCount(0);
  await controls.nth(1).click();
  await expect.poll(() => resolutions.length).toBe(1);
  expect(resolutions[0]).toMatchObject({ kind: 'transcript', source_item_id: 'native-message-2', source_event: { seq: 2 }, field_path: ['content'] });
  await reachable(page.getByRole('button', { name: 'Comment on source', exact: true }));
});
test('Work comment entry expands the existing inspector without replacing the source or draft', async ({ page }) => {
  const { saved, resolutions } = await setup(page, 'work', 0);
  await page.getByRole('treeitem', { name: /rollback.md/ }).click();
  await page.getByRole('button', { name: 'Feedback on rollback.md change 1', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Exit expanded inspector', exact: true })).toBeVisible();
  const tools = page.getByRole('button', { name: 'Comment on source', exact: true });
  await reachable(tools);
  const pane = page.getByTestId('conversation-info-drawer');
  const within = await tools.evaluate((node, box) => { const rect = node.getBoundingClientRect(); return rect.left >= box.x && rect.right <= box.x + box.width; }, (await pane.boundingBox())!);
  expect(within).toBe(true);
  await page.getByRole('button', { name: 'Select lines', exact: true }).click();
  await page.getByRole('button', { name: 'Comment on file', exact: true }).click();
  const input = page.getByRole('textbox', { name: 'Comment', exact: true });
  await input.fill('Retain exact Work intent');
  await page.getByRole('button', { name: 'Exit expanded inspector', exact: true }).click();
  await expect(input).toHaveValue('Retain exact Work intent');
  await expect(page.getByRole('button', { name: 'Expand inspector', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Expand inspector', exact: true }).click();
  await expect(input).toHaveValue('Retain exact Work intent');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(resolutions[0]).toMatchObject({ kind: 'work_diff', file_ordinal: 1, source_event: { seq: 20 } });
  expect(saved[0].anchor.source_token).toBe('token-20-1');
});
