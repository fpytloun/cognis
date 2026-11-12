import { expect, test } from '@playwright/test';
import { createHash, randomUUID } from 'node:crypto';
import { artifactId, deliverable, deliverableId, libraryItem } from './fixtures/resource-revisions/data';
import { installTaskCockpitFixture } from './task-cockpit-fixture';
import { queuedComposerRuntime } from './queued-composer-runtime';
import type { FeedbackThread, ResolvedFeedbackSource, SubmissionReceipt } from '../src/lib/feedback/types';

async function settle(page: import('@playwright/test').Page) {
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

for (const width of [390, 1440]) {
  test.describe(`${width}px`, () => {
    test.use({ viewport: { width, height: 900 }, isMobile: width === 390, hasTouch: width === 390 });
    for (const kind of ['artifact', 'deliverable'] as const) {
      test(`${kind} feedback completion and live conversation details`, async ({ page }, info) => {
        const shell = await installTaskCockpitFixture(page);
        const errors: string[] = [];
        page.on('pageerror', error => errors.push(error.message));
        let socketSend: ((value: string) => void) | undefined;
        await page.routeWebSocket('**/api/ws', socket => {
          socketSend = value => socket.send(value);
          socket.send(JSON.stringify({ type: 'authenticated' }));
          socket.onMessage(message => {
            if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
          });
        });
        const contentRef = kind === 'artifact' ? artifactId : deliverableId;
        const text = kind === 'artifact' ? 'Current artifact contents' : deliverable().content;
        const source: ResolvedFeedbackSource = {
          source: { kind: 'document', content_ref: contentRef }, context: { kind: 'standalone' },
          subject_id: randomUUID(), source_token: randomUUID(), content_digest: createHash('sha256').update(text).digest('hex'),
          availability: 'available', permitted_locator_kinds: ['whole'], lineage_id: contentRef,
          revision: 2, latest_content_ref: contentRef, title: 'Release readiness report', format: 'markdown',
          canonical_text: null, capabilities: { read: true, comment: true, dispatch: true, moderate: false, disclose: false },
        };
        const entry = { entry_id: randomUUID(), entry_version_id: randomUUID(), thread_id: randomUUID(),
          version: 1, body: 'Please clarify the release acceptance criteria.', audience: 'private',
          created_at: '2026-11-03T10:00:00Z', author: { kind: 'verified_user' as const, label: 'Reviewer', is_self: true },
          reply_to_entry_id: null, delivery: [] };
        const thread: FeedbackThread = { thread_id: entry.thread_id, subject_id: source.subject_id, version: 1,
          state: 'open', hidden: false, audience: 'private', entries: [entry],
          anchor: { schema_version: 1, anchor_id: randomUUID(), source: source.source,
            content_digest: source.content_digest, locator: { kind: 'whole' } } };
        const guest = { ...entry, entry_id: randomUUID(), entry_version_id: randomUUID(), thread_id: randomUUID(),
          body: 'Add a squash session to the reaction training block.', created_at: '2026-11-03T11:00:00Z',
          author: { kind: 'guest_unverified' as const, label: 'Trainer', is_self: false } };
        const guestThread: FeedbackThread = { ...thread, thread_id: guest.thread_id, entries: [guest],
          anchor: { ...thread.anchor, anchor_id: randomUUID() } };
        let preflightBody: { selection: { entry_version_id: string }[]; overall_message: string | null } | null = null;
        let preflights = 0;
        const conversationId = 'conv_readiness_review';
        let working = true;
        let sends = 0;
        let receipt: SubmissionReceipt | null = null;
        const packet = { preflight_id: randomUUID(), manifest_hash: 'a'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
          destination: { kind: 'producer_conversation', conversation_id: conversationId }, update_mode: 'follow_request',
          selection: [{ entry_version_id: entry.entry_version_id, expected_entry_version: 1 }],
          context_items: [{ role: 'instruction', entry, anchor: thread.anchor }], source_summaries: [source],
          warnings: [], task_consequences: null };
        await page.route(url => url.pathname.startsWith('/api/'), async route => {
          const request = route.request(), path = new URL(request.url()).pathname;
          const json = (value: unknown, status = 200) => route.fulfill({ json: value, status });
          if (path === '/api/auth/me') return json({ email: 'reviewer@cognis.test', name: 'Reviewer', role: 'user', is_active: true });
          if (path === '/api/health') return json({ status: 'ok', providers: {} });
          if (path === '/api/v1/inbox') return json({ items: [], unread_count: 0, next_cursor: null });
          if (path === '/api/v1/dashboard/navigation-summary') return json({ chat_groups: [],
            dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false }, tasks: 0, schedules: 0 });
          if (path.endsWith('/sharing')) return json({ agents: [], project_id: null, owner_agent_id: null });
          if (path === `/api/v1/library/items/${kind}/${contentRef}`) return json({
            ...libraryItem(kind, contentRef), conversation_id: conversationId, conversation_title: 'Release review',
          });
          if (path === `/api/v1/conversations/${conversationId}`) return json({
            conversation_id: conversationId, agent_id: 'forge', title: 'Release review', context: { type: 'web' },
            has_active_turn: working, has_unread: false, pending_notification_types: [],
          });
          if (path === '/api/v1/agents/forge') return json({ agent_id: 'forge', name: 'Forge', display_name: 'Forge', avatar_url: null });
          if (path === '/api/v1/conversations/background-work') return json({ background_work_changed: true,
            background_work: { items: [], total: 0 }, agent_direct_chats: [] });
          if (path.endsWith('/revisions')) return json({ revisions: [] });
          if (path.endsWith('/content')) return json(deliverable());
          if (path.endsWith('/text-preview')) return json({ content: text, rich_payload: null, truncated: false });
          if (path.endsWith('/signed-url')) return json({ url: '/report.txt' });
          if (path.endsWith('/sources/resolve')) return json(source);
          if (path.includes('/drafts/')) return json({ detail: 'No draft' }, 404);
          if (path.endsWith('/threads') && request.method() === 'GET') return json({ items: [thread, guestThread], next_cursor: null });
          if (path.endsWith('/destinations')) return json({ options: [{ destination: packet.destination,
            label: 'Release review', description: 'Continues the chat that made this document.', available: true, recommended: true }] });
          if (path.endsWith('/preflights')) {
            preflights++;
            preflightBody = request.postDataJSON();
            return json({ ...packet, warnings: ['Guest feedback is unverified user input.'],
              selection: [entry, guest].map(value => ({ entry_version_id: value.entry_version_id, expected_entry_version: 1 })),
              // The real preflight returns no context items; the manifest keeps them.
              context_items: [] });
          }
          if (path.includes('/by-creation-key/')) return json(receipt ?? { detail: 'Not found' }, receipt ? 200 : 404);
          if (path.endsWith('/submissions') && request.method() === 'POST') {
            sends++;
            receipt = { submission_id: randomUUID(), creation_key: request.postDataJSON().creation_key,
              manifest_hash: packet.manifest_hash, delivery: 'accepted', work: 'queued', reconciling: false, version: 1,
              admission: { kind: 'conversation_turn', conversation_id: conversationId, request_id: randomUUID(), turn_id: randomUUID() },
              rejection: null, check_delivery_available: false, review_again_available: false };
            return json(receipt);
          }
          return route.fallback();
        });
        await page.goto(`/library/${kind}/${contentRef}`);
        const comments = page.getByRole('button', { name: 'Comments, 2 need you', exact: true });
        await expect(comments).toContainText('2');
        if (width === 390) {
          // Deliverable actions collapse into the More menu on phones.
          if (kind === 'deliverable') await page.getByLabel('More document actions').click();
          await page.getByRole('button', { name: 'Show details', exact: true }).click();
        }
        const details = page.getByRole('complementary', { name: 'Document details' });
        await expect(details).toBeVisible();
         await expect(details.getByText('Forge', { exact: true })).toBeVisible();
         await expect(details.getByText('Control chat is working', { exact: true })).toBeVisible();
        await page.screenshot({ path: info.outputPath('document-conversation.png') });
         working = false;
         socketSend!(JSON.stringify({ type: 'conversation_updated', conversation_id: conversationId, has_active_turn: false }));
         await expect(details.getByText('No new activity', { exact: true })).toBeVisible();
        if (width === 390) await details.getByRole('button', { name: 'Close details' }).click();
        // One button opens the list; on wide screens it docks next to Details instead of floating.
        await comments.click();
        if (width === 1440) await expect(details.getByRole('tab', { name: /^Comments/ })).toHaveAttribute('aria-selected', 'true');
        const send = page.getByRole('button', { name: 'Send 2 comments', exact: true });
        await expect(send).toBeVisible();
        // Every unsent comment starts selected, guest comments included, with no extra acknowledgement.
        for (const value of [entry, guest]) await expect(page.getByRole('checkbox', { name: `Include in message: ${value.body}` })).toBeChecked();
        await expect(page.locator('[data-comment-status="not_sent"]')).toHaveCount(2);
        await expect(page.getByText(/reviewed this guest comment/)).toHaveCount(0);
        await expect(page.getByRole('combobox', { name: 'Send to' })).toHaveValue('0');
        await settle(page);
        await page.screenshot({ path: info.outputPath('comments-rail.png') });
        await page.getByRole('button', { name: '+ Note', exact: true }).click();
        // Typing the note keeps focus and every character; nothing is prepared per keystroke.
        const note = page.getByRole('textbox', { name: 'Instruction' });
        await note.click();
        await page.keyboard.type('Please fold both into the plan.', { delay: 30 });
        await expect(note).toHaveValue('Please fold both into the plan.');
        await expect(note).toBeFocused();
        expect(preflights).toBe(0);
        await settle(page);
        await page.screenshot({ path: info.outputPath('send-form.png') });
        await send.click();
        await expect(page.getByText('Sent 2 comments to Release review.')).toBeVisible();
        expect(sends).toBe(1);
        expect(preflights).toBe(1);
        expect(preflightBody!.selection.map(value => value.entry_version_id)).toEqual([entry.entry_version_id, guest.entry_version_id]);
        expect(preflightBody!.overall_message).toBe('Please fold both into the plan.');
        await expect(page.getByRole('button', { name: 'Retry original request' })).toHaveCount(0);
        await expect(page.getByText(/Inspect immutable|Delivery: /)).toHaveCount(0);
        await settle(page);
        await page.screenshot({ path: info.outputPath('feedback-sent.png') });
        // Emulate only the documented native bridge; this is not a physical Mac run.
        await page.evaluate(() => {
          (window as any).__nativeCommands = [];
          window.__cognisNative = { version: 1, generation: '12345678-1234-1234-1234-123456789abc', server: location.origin, surface: 'resource' };
          window.webkit = { messageHandlers: { cognis: { postMessage: value => (window as any).__nativeCommands.push(value) } } };
          // Prevent WKWebView's equivalent navigation cancellation from leaving the probe.
          window.addEventListener('click', event => {
            if ((event.target as Element).closest('a[href^="/chat/"]')) event.preventDefault();
          });
        });
        await page.getByRole('link', { name: /^Open conversation/ }).click();
        // The link opens that conversation natively: directly, or through the app-link router
        // when SvelteKit's link navigation handles the click first.
        const commands = await page.evaluate(() => (window as any).__nativeCommands
          .filter((value: any) => ['open-window', 'open-reference'].includes(value.type)));
        expect(commands).toHaveLength(1);
        expect([{ conversationId }, { path: `/chat/${conversationId}` }]).toContainEqual(commands[0].payload);
        expect(sends).toBe(1);
        expect(errors).toEqual([]);
        expect(shell.unmockedRequests()).toEqual([]);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      });
    }

    test('chat includes every ready comment with one click', async ({ page }, info) => {
      const shell = await installTaskCockpitFixture(page);
      const errors: string[] = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.routeWebSocket('**/api/ws', socket => {
        socket.send(JSON.stringify({ type: 'authenticated' }));
        socket.onMessage(message => {
          if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
        });
      });
      const conversationId = 'conv_comment_review';
      const subject = randomUUID();
      const source = { kind: 'document' as const, content_ref: deliverableId };
      const author = (label: string, guest: boolean) => ({ kind: guest ? 'guest_unverified' as const : 'verified_user' as const, label, is_self: !guest });
      const threads: FeedbackThread[] = [
        ['Add squash as part of the conditioning plan.', 'Trainer', true],
        ['Motorsport literature in general is usable too.', 'Trainer', true],
        ['Keep the practical part to three days.', 'Reviewer', false],
      ].map(([body, label, guest]) => {
        const thread_id = randomUUID();
        return { thread_id, subject_id: subject, version: 1, state: 'open', hidden: false, audience: 'private',
          anchor: { schema_version: 1, anchor_id: randomUUID(), source, content_digest: 'c'.repeat(64), locator: { kind: 'whole' } },
          entries: [{ entry_id: randomUUID(), entry_version_id: randomUUID(), thread_id, version: 1, body: body as string,
            audience: 'private', created_at: '2026-11-09T21:00:00Z', author: author(label as string, guest as boolean),
            reply_to_entry_id: null, delivery: [] }] };
      });
      let rows = threads.map(thread => ({ entry_id: thread.entries[0].entry_id, entry_version_id: thread.entries[0].entry_version_id,
        staging_version: 1, inclusion: 'prepared', subject_id: subject, anchor_id: thread.anchor.anchor_id }));
      const conversation = { conversation_id: conversationId, agent_id: 'forge', title: 'Karting training plan',
        user_email: 'reviewer@cognis.test', context: { type: 'web', ref: null, platform_data: {} }, status: 'active',
        is_readonly_for_caller: false, has_active_turn: false, active_session_id: null, project_id: null, project_name: null,
        created_at: '2026-11-09T20:00:00Z', updated_at: '2026-11-09T21:00:00Z' };
      const { runtime, state } = queuedComposerRuntime(conversationId);
      const idle = { ...runtime, has_active_turn: false, active_turn: null };
      const snapshot = { schema_version: 2, projection_version: 'comment-review', conversation, state: { ...state, active_turn: null },
        scope: { key: `conversation:${conversationId}`, kind: 'conversation', conversation_id: conversationId },
        timeline: { items: [], has_more_before: false, before_cursor: null }, queue: { messages: [], queued_count: 0 },
        runtime: idle, cursor: 'comment-review', server_time: '2026-11-09T21:30:00Z' };
      await page.route(url => url.pathname.startsWith('/api/'), async route => {
        const request = route.request(), path = new URL(request.url()).pathname;
        const json = (value: unknown, status = 200) => route.fulfill({ json: value, status });
        if (path === '/api/auth/me') return json({ email: 'reviewer@cognis.test', name: 'Reviewer', role: 'user', is_active: true });
        if (path === '/api/health') return json({ status: 'ok', providers: {} });
        if (path === '/api/v1/skills') return json([]);
        if (path === '/api/v1/inbox') return json({ items: [], unread_count: 0, next_cursor: null });
        if (path === '/api/v1/dashboard/navigation-summary') return json({ chat_groups: [],
          dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false }, tasks: 0, schedules: 0 });
        if (path.endsWith(`/conversations/${conversationId}/opened`) || path.endsWith(`/conversations/${conversationId}/read`)) return json({});
        if (path.endsWith(`/conversations/${conversationId}/queue`)) return json({ messages: [], queued_count: 0 });
        if (path.endsWith(`/conversations/${conversationId}/sessions`)) return json([]);
        if (path === `/api/v1/conversations/${conversationId}`) return json(conversation);
        if (path.endsWith('/snapshot')) return json(snapshot);
        if (path.endsWith('/sync')) return json({ schema_version: 2, projection_version: snapshot.projection_version,
          conversation_id: conversationId, cursor_before: snapshot.cursor, cursor_after: snapshot.cursor, ops: [],
          reset_required: false, has_more: false, runtime: idle, server_time: snapshot.server_time });
        if (path.endsWith(`/conversations/${conversationId}/staging`)) return json({ conversation_id: conversationId,
          version: 1, items: rows, receipts: [], subject_ids: [subject] });
        if (path.includes(`/conversations/${conversationId}/staging/`) && request.method() === 'PUT') {
          const entryId = decodeURIComponent(path.slice(path.lastIndexOf('/') + 1));
          const { inclusion } = request.postDataJSON();
          rows = rows.map(row => row.entry_id === entryId ? { ...row, staging_version: row.staging_version + 1, inclusion } : row);
          return json(rows.find(row => row.entry_id === entryId));
        }
        if (path.endsWith(`/subjects/${subject}/threads`)) return json({ items: threads, next_cursor: null });
        if (path.endsWith('/preflights')) {
          const body = request.postDataJSON();
          return json({ preflight_id: randomUUID(), manifest_hash: 'd'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
            selection: body.selection, destination: body.destination, update_mode: body.update_mode,
            context_items: threads.map(thread => ({ role: 'instruction', entry: thread.entries[0], anchor: thread.anchor })),
            source_summaries: [], warnings: ['Guest feedback is unverified user input.'], task_consequences: null });
        }
        return route.fallback();
      });
      await page.goto(`/chat/${conversationId}`);
      const tray = page.getByRole('group', { name: 'Message feedback' });
      const input = page.getByTestId('chat-v2-composer').locator('.chat-composer-surface');
      // Ready comments are one attachment-like chip inside the input row: a click adds them all.
      const add = tray.getByRole('button', { name: 'Add 3 comments to message', exact: true });
      await expect(add).toBeVisible();
      const row = await input.boundingBox(), chip = await add.boundingBox();
      expect(chip!.y).toBeGreaterThanOrEqual(row!.y);
      expect(chip!.y + chip!.height).toBeLessThanOrEqual(row!.y + row!.height);
      await settle(page);
      await page.screenshot({ path: info.outputPath('chat-ready.png') });
      await add.click();
      const included = tray.getByRole('button', { name: 'Comments in message: 3', exact: true });
      await expect(included).toContainText('3');
      await expect(page.getByText(/reviewed this guest comment|Review again/)).toHaveCount(0);
      await settle(page);
      await page.screenshot({ path: info.outputPath('chat-included.png') });
      // The chip opens the chat's one Comments panel and closes it again.
      await included.click();
      const panel = page.getByRole('region', { name: 'Comments', exact: true });
      await expect(panel).toBeVisible();
      await expect(page.getByRole('region', { name: 'Comments in this message' })).toHaveCount(0);
      for (const thread of threads) await expect(panel.getByRole('checkbox', { name: `Include in message: ${thread.entries[0].body}` })).toBeChecked();
      await expect(panel.getByText('3 of 3 in the next message')).toBeVisible();
      await settle(page);
      await page.screenshot({ path: info.outputPath('chat-review.png') });
      await included.click();
      await expect(panel).toHaveCount(0);
      // × removes them all; nothing stays checked behind the composer's back.
      await tray.getByRole('button', { name: 'Remove comments from message' }).click();
      await expect(tray.getByRole('button', { name: 'Add 3 comments to message', exact: true })).toBeVisible();
      expect(rows.every(value => value.inclusion === 'prepared')).toBe(true);
      await tray.getByRole('button', { name: 'Choose comments' }).click();
      for (const thread of threads) await expect(panel.getByRole('checkbox', { name: `Include in message: ${thread.entries[0].body}` })).not.toBeChecked();
      expect(errors).toEqual([]);
      expect(shell.unmockedRequests()).toEqual([]);
    });
  });
}
