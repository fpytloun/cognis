import { expect, test } from '@playwright/test';
import { createHash, randomUUID } from 'node:crypto';
import { artifactId, deliverable, deliverableId, libraryItem } from './fixtures/resource-revisions/data';
import type { FeedbackAnchor, FeedbackThread, ResolvedFeedbackSource } from '../src/lib/feedback/types';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ serviceWorkers: 'block' });

for (const width of [390, 1440]) {
  test.describe(`${width}px`, () => {
    test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390 });
    for (const kind of ['artifact', 'deliverable'] as const) {
      test(`${kind} whole-document comment opens and saves from viewing mode`, async ({ page }, info) => {
        const shell = await installTaskCockpitFixture(page);
        await page.routeWebSocket('**/api/ws', socket => {
          socket.send(JSON.stringify({ type: 'authenticated' }));
          socket.onMessage(message => {
            if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
          });
        });
        const contentRef = kind === 'artifact' ? artifactId : deliverableId;
        const text = kind === 'artifact' ? 'Current artifact contents' : deliverable().content;
        const digest = createHash('sha256').update(text).digest('hex');
        const source: ResolvedFeedbackSource = {
          source: { kind: 'document', content_ref: contentRef }, context: { kind: 'standalone' },
          subject_id: randomUUID(), source_token: randomUUID(), content_digest: digest,
          availability: 'available', permitted_locator_kinds: ['whole', 'text'],
          lineage_id: contentRef, revision: 2, latest_content_ref: contentRef,
          title: 'Release readiness report', format: kind === 'artifact' ? 'text' : 'markdown',
          canonical_text: { representation: 'feedback-text-v1', text_digest: digest, length: text.length, available_ranges: [[0, text.length]] },
          capabilities: { read: true, comment: true, dispatch: true, moderate: false, disclose: false },
        };
        function thread(locator: FeedbackAnchor['locator'], body: string): FeedbackThread {
          const id = randomUUID();
          return { thread_id: id, subject_id: source.subject_id, version: 1, state: 'open', hidden: false, audience: 'internal',
            anchor: { schema_version: 1, anchor_id: randomUUID(), source: source.source, content_digest: digest, locator },
            entries: [{ entry_id: randomUUID(), entry_version_id: randomUUID(), thread_id: id, version: 1, body,
              audience: 'internal', created_at: '2026-11-04T12:00:00Z', author: { kind: 'verified_user', label: 'Reviewer', is_self: true },
              reply_to_entry_id: null, delivery: [] }] };
        }
        const threads = [thread({ kind: 'text', range: { representation: 'feedback-text-v1', text_digest: digest,
          start: 0, end: text.length, exact: text, prefix: '', suffix: '' } }, 'Check the inline findings.')];
        const saved: { anchor: FeedbackAnchor; body: string }[] = [];
        const errors: string[] = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route(url => url.pathname.startsWith('/api/'), async route => {
          const request = route.request(), path = new URL(request.url()).pathname;
          const json = (value: unknown, status = 200) => route.fulfill({ json: value, status });
          if (path === '/api/auth/me') return json({ email: 'reviewer@cognis.test', name: 'Reviewer', role: 'user', is_active: true });
          if (path === '/api/health') return json({ status: 'ok', providers: {} });
          if (path === '/api/v1/inbox') return json({ items: [], unread_count: 0, next_cursor: null });
          if (path === '/api/v1/dashboard/navigation-summary') return json({
            chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
            tasks: 0, schedules: 0,
          });
          if (path.endsWith('/sharing')) return json({ agents: [], project_id: null, owner_agent_id: null });
          if (path === `/api/v1/library/items/${kind}/${contentRef}`) return json(libraryItem(kind, contentRef));
          if (path.endsWith('/revisions')) return json({ revisions: [] });
          if (path.endsWith('/content')) return json(deliverable());
          if (path.endsWith('/text-preview')) return json({ content: text, rich_payload: null, truncated: false });
          if (path.endsWith('/signed-url')) return json({ url: '/report.txt' });
          if (path.endsWith('/sources/resolve')) return json(source);
          if (path.endsWith('/sources/text')) {
            const body = request.postDataJSON();
            return json({ scope: body.scope, content_digest: digest, descriptor: source.canonical_text,
              text_page: { representation: 'feedback-text-v1', text_digest: digest, start: 0, end: text.length, total_length: text.length, text } });
          }
          if (path.includes('/drafts/')) return json(request.method() === 'PUT' ? { version: 1 } : { detail: 'No draft' }, request.method() === 'PUT' ? 200 : 404);
          if (path.endsWith('/threads') && request.method() === 'GET') return json({ items: threads, next_cursor: null });
          // The owner's comment list carries the send bar, which lists destinations.
          if (path.endsWith('/destinations')) return json({ options: [] });
          if (path === '/api/v1/feedback/threads' && request.method() === 'POST') {
            const body = request.postDataJSON(); saved.push(body);
            const created = thread(body.anchor.locator, body.body); threads.push(created);
            return json(created, 201);
          }
          return route.fallback();
        });
        await page.goto(`/library/${kind}/${contentRef}`);
        const toggle = page.locator('.comment-toggle');
        await expect(toggle).toContainText('1');
        await toggle.click();
        await page.getByRole('button', { name: 'Comment on document' }).click();
        const editor = page.getByRole('textbox', { name: 'Comment', exact: true });
        // Keep the rendered pre-fix failure, rather than taking a screenshot only after success.
        await page.screenshot({ path: info.outputPath('after-whole-comment-action.png') });
        await expect(editor).toBeVisible();
        await expect(page.getByRole('region', { name: 'Add comment' }).getByText('Whole document', { exact: true })).toBeVisible();
        await editor.fill('Please review the entire readiness report.');
        await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
        await expect(page.getByText(/^(Loading|Saving)[.…]*$/)).toHaveCount(0);
        await page.screenshot({ path: info.outputPath('whole-document-composer.png') });
        await page.getByRole('button', { name: 'Send', exact: true }).click();
        await expect(editor).toHaveCount(0);
        expect(saved).toHaveLength(1);
        expect(saved[0].body).toBe('Please review the entire readiness report.');
        expect(saved[0].anchor.locator).toEqual({ kind: 'whole' });
        expect(saved[0].anchor.source_token).toBe(source.source_token);
        expect(saved[0].anchor.content_digest).toBe(digest);
        await expect(toggle).toContainText('2');
        await expect(toggle).toHaveAttribute('aria-pressed', 'true');
        // The list is back after saving, ready for the next comment.
        await page.getByRole('button', { name: 'Comment on document' }).click();
        await expect(editor).toBeVisible();
        await page.getByRole('button', { name: 'Cancel', exact: true }).click();
        await expect(editor).toHaveCount(0);
        await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
        expect(saved).toHaveLength(1);
        expect(errors).toEqual([]);
        expect(shell.unmockedRequests()).toEqual([]);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      });
    }
  });
}
