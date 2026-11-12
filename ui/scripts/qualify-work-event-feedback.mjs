import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';

// Component-boundary Chromium qualification against synthetic wire-contract HTTP
// responses. This does not execute an agent or mutate a running Cognis stack.
const origin = process.argv[2];
if (!origin) throw new Error('Pass the URL of this worktree’s loopback Vite server.');
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
  const failures = [];
  page.on('pageerror', error => { failures.push(error.message); console.error(error); });
  page.on('requestfailed', request => console.error('Request failed', request.url(), request.failure()));
  page.on('console', message => { if (message.type() === 'error') console.error(message.text()); });
  const requests = [];
  const discussions = new Map();
  let fixtures;
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    const body = request.postDataJSON();
    let result = {};
    let status = 200;
    if (path.endsWith('/work/file-history')) {
      const ordinal = body.path_generation_id === 'generation-0' ? 0 : 1;
      result = { ...fixtures.history, path_generation_id: body.path_generation_id,
        items: fixtures.events.filter(event => event.feedback_source.file_ordinal === ordinal) };
    } else if (path.endsWith('/sources/resolve')) {
      const source = body.source;
      result = { ...fixtures.resolved, source, context: body.context,
        subject_id: `subject-${source.source_event.seq}-${source.file_ordinal}`,
        source_token: `token-${source.source_event.seq}-${source.file_ordinal}` };
    } else if (path.includes('/drafts/')) {
      status = request.method() === 'GET' ? 404 : 200;
      result = request.method() === 'GET' ? { detail: 'Not found' } : { version: 1 };
    } else if (path.endsWith('/feedback/threads') && request.method() === 'POST') {
      requests.push(body);
      const subject = body.anchor.source_token.replace('token-', 'subject-');
      const source = fixtures.events.find(event =>
        `token-${event.feedback_source.source_event.seq}-${event.feedback_source.file_ordinal}` === body.anchor.source_token).feedback_source;
      const thread = { thread_id: `thread-${requests.length}`, subject_id: subject, version: 1,
        state: 'open', hidden: false, audience: 'internal',
        anchor: { schema_version: 1, anchor_id: `anchor-${requests.length}`, source,
          locator: body.anchor.locator, content_digest: body.anchor.content_digest },
        entries: [{ entry_id: `entry-${requests.length}`, entry_version_id: `version-${requests.length}`,
          version: 1, thread_id: `thread-${requests.length}`, body: body.body,
          author: { kind: 'verified_user', label: 'Component reviewer', is_self: true }, audience: 'internal',
          reply_to_entry_id: null, created_at: '2026-10-27T00:00:00Z', delivery: [] }] };
      discussions.set(subject, [...(discussions.get(subject) ?? []), thread]);
      result = thread;
    } else if (path.endsWith('/threads')) {
      const subject = decodeURIComponent(path.split('/').at(-2));
      result = { items: discussions.get(subject) ?? [], next_cursor: null };
    } else if (path.endsWith('/staging')) {
      result = { conversation_id: 'work-activation-test', version: 0, items: [], receipts: [] };
    }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(result) });
  });
  await page.route('**/work-event-feedback-harness', route => route.fulfill({
    contentType: 'text/html',
    body: `<html><head><meta charset="utf-8"></head><body><div id="target"></div>
      <script>globalThis.__sveltekit_dev = { env: {} };</script>
      <script type="module">
        import { fixture, start } from '/src/lib/components/work/work-event-feedback.browser.ts';
        window.fixture = fixture;
        window.start = start;
      </script></body></html>`,
  }));
  await page.goto(`${origin}/work-event-feedback-harness`);
  await page.waitForFunction(() => window.start);
  fixtures = await page.evaluate(() => ({
    history: window.fixture.history(),
    events: [window.fixture.eventDiff(20, 0), window.fixture.eventDiff(10, 0),
      window.fixture.eventDiff(20, 1), window.fixture.eventDiff(10, 1)],
    resolved: window.fixture.resolved(window.fixture.eventDiff(10).feedback_source),
  }));
  await page.evaluate(() => window.start());
  await page.getByTestId('work-file-path-1').click();
  const event = sequence => page.locator(`[data-testid="work-event-diff"][data-source-item-id="tool-result-${sequence}"]`);
  await event(10).getByTestId('work-event-feedback').waitFor();
  assert.equal(await page.getByRole('button', { name: 'Comment on file', exact: true }).count(), 0);
  for (const [sequence, side] of [[10, 'old'], [20, 'new']]) {
    await event(sequence).getByTestId('work-event-feedback').click();
    await event(sequence).getByRole('button', { name: `Select ${side} line 1`, exact: true }).click();
    await event(sequence).getByRole('button', { name: 'Comment on selected lines', exact: true }).click();
    await page.getByText('Comment on diff selection', { exact: true }).waitFor();
    await page.getByRole('textbox', { name: 'Comment', exact: true }).fill(`Chromium ${side} event ${sequence}`);
    await page.getByRole('button', { name: 'Save comment', exact: true }).click();
    await page.getByRole('button', { name: 'Saving…', exact: true }).waitFor({ state: 'hidden' });
    await page.getByRole('region', { name: 'Feedback panel' }).getByText(`Chromium ${side} event ${sequence}`, { exact: true }).waitFor();
    assert.equal(requests.at(-1).anchor.source_token, `token-${sequence}-1`);
    assert.match(requests.at(-1).anchor.locator.hunk_digest, /^[a-f0-9]{64}$/);
    assert.deepEqual({ ...requests.at(-1).anchor.locator, hunk_digest: null }, {
      kind: 'diff', scope: 'lines', side, start_line: 1, end_line: 1,
      quote: `${side === 'old' ? 'before' : 'after'} ${sequence}`, hunk_digest: null,
    });
  }
  await event(10).getByTestId('work-event-feedback').click();
  await event(10).getByRole('button', { name: 'Comment on file', exact: true }).click();
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Chromium whole-event discussion');
  await page.getByRole('button', { name: 'Save comment', exact: true }).click();
  await page.getByRole('button', { name: 'Saving…', exact: true }).waitFor({ state: 'hidden' });
  await page.getByRole('region', { name: 'Feedback panel' }).getByText('Chromium whole-event discussion', { exact: true }).waitFor();
  assert.equal(requests.at(-1).anchor.source_token, 'token-10-1');
  assert.equal(requests.at(-1).anchor.locator.scope, 'file');
  await page.screenshot({ path: '/tmp/work-event-feedback-chromium.png', fullPage: true });
  assert.deepEqual(failures, []);
  console.log(JSON.stringify({ result: 'PASS', saved: requests.length, anchors: requests.map(request => request.anchor),
    screenshot: '/tmp/work-event-feedback-chromium.png', pageErrors: failures }, null, 2));
} finally {
  await browser.close();
}
