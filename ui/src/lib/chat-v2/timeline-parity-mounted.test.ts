import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { tick } from 'svelte';
import 'fake-indexeddb/auto';
import { ProfileIntentStore } from '$lib/profile-intent';

const fixtureProfileIntents = new Map<string, unknown>();

const fixture = vi.hoisted(() => ({
  owner: 'mounted-timeline-fixture' as string | null,
  realCache: false,
  rows: new Map<string, unknown>(),
  changed: () => {},
  conversationId: 'mounted-parity',
  navigate: (_id: string) => {},
  stores: 0,
  snapshotProjections: 0,
  stateRestores: 0,
}));
vi.mock('$lib/chat-v2/store.svelte', async (importOriginal) => {
  const original = await importOriginal<typeof import('./store.svelte')>();
  return { ...original, ChatV2Store: class extends original.ChatV2Store {
    constructor() { super(); fixture.stores += 1; }
    override replaceFromSnapshot(snapshot: ChatSnapshot): void {
      fixture.snapshotProjections += 1;
      super.replaceFromSnapshot(snapshot);
    }
    override restoreState(state: Parameters<InstanceType<typeof original.ChatV2Store>['restoreState']>[0]): void {
      fixture.stateRestores += 1;
      super.restoreState(state);
    }
    override replaceFromSnapshotForCanonicalBootstrap(
      snapshot: ChatSnapshot,
      watermark: ReturnType<InstanceType<typeof original.ChatV2Store>['refreshWatermark']>,
      bootstrapPending: boolean,
    ): boolean {
      if (bootstrapPending) fixture.snapshotProjections += 1;
      return super.replaceFromSnapshotForCanonicalBootstrap(snapshot, watermark, bootstrapPending);
    }
  } };
});
vi.mock('$app/state', async () => {
  const { writable, fromStore } = await import('svelte/store');
  const location = (id: string) => ({
    params: { conversationId: id },
    url: new URL(`http://localhost/chat/${id}`),
  });
  const route = writable(location(fixture.conversationId));
  const state = fromStore(route);
  fixture.navigate = id => route.set(location(id));
  return { page: {
    get params() { return state.current.params; },
    get url() { return state.current.url; },
    data: {},
  } };
});
vi.mock('$app/navigation', () => ({ goto: vi.fn(), afterNavigate: vi.fn(), beforeNavigate: vi.fn() }));
vi.mock('$lib/offline/cache', async (importOriginal) => {
  const original = await importOriginal<typeof import('$lib/offline/cache')>();
  return {
  ...original,
  offlineCache: {
  get currentOwner() { return fixture.realCache ? original.offlineCache.currentOwner : fixture.owner; },
  onOwnerChange: (listener: () => void) => {
    fixture.changed = listener;
    return original.offlineCache.onOwnerChange(() => { if (fixture.realCache) listener(); });
  },
  get: async (...args: Parameters<typeof original.offlineCache.get>) => fixture.realCache
    ? original.offlineCache.get(...args) : fixture.rows.get(`${args[0]}:${args[1]}`) ?? null,
  put: vi.fn(async (...args: Parameters<typeof original.offlineCache.put>) => {
    if (fixture.realCache) return original.offlineCache.put(...args);
    const [kind, key, value] = args;
    fixture.rows.set(`${kind}:${key}`, value); return true;
  }),
} };
});

import FullChat from '../../routes/(app)/chat/[conversationId]/+page.svelte';
import CompactConversationChat from '$lib/components/chat-v2/CompactConversationChat.svelte';
import { conversationComposer } from './conversation-composer';
import { conversationFeedback } from '$lib/feedback/staging';
import { feedbackApi } from '$lib/feedback/api';
import type { FeedbackEntryVersion, FeedbackPreflight, FeedbackStagingSnapshot } from '$lib/feedback/types';
import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
import { api } from '$lib/api/client';
import { documentCoordination } from '$lib/document-coordination';
import { DocumentDrafts } from '$lib/offline/document-drafts';
import { hydrateComposer } from './composer-hydration';
import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
import { ChatV2ApiClient, chatV2Api } from './api';
import { offlineCache } from '$lib/offline/cache';
import { wsClient } from '$lib/ws/client';
import { CoordinatedOutbox, IndexedDbChatV2Outbox, createIndexedDbChatV2Outbox } from './outbox';
import { queuedComposerRuntime } from '../../../e2e/queued-composer-runtime';
import * as outboxSending from './outbox-send';
import * as chatPage from '$lib/chat-page';
import { ChatV2Store } from './store.svelte';
import { acquireTimeline, clearTimelineMemory, peekTimeline, timelineResourceCounts, TimelineOwnershipError } from './timeline-controller.svelte';
import { conversationTimelineScope, type ChatSnapshot } from './types';

const scope = conversationTimelineScope(fixture.conversationId);
// These cases mount real full/scoped trees and complete several independently
// bounded hydration/history phases. Their aggregate scenario budget is not a
// UI latency assertion.
const MOUNTED_SCENARIO_TIMEOUT_MS = 20_000;
const outboxFailures: unknown[] = [];
const pendingOutboxWork = new Set<Promise<unknown>>();
const mountedDrafts = new Set<DocumentDrafts>();

function trackOutboxWork<T>(operation: Promise<T>): Promise<T> {
  pendingOutboxWork.add(operation);
  void operation.then(
    () => { pendingOutboxWork.delete(operation); },
    error => {
      pendingOutboxWork.delete(operation);
      outboxFailures.push(error);
    },
  );
  return operation;
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

async function settleFixtureOutboxWork(): Promise<void> {
  // Composer writes and outbox reads share document authority. Keep their owner
  // and mocks alive until both settle, before the next test's beginLogin.
  let drafts: DocumentDrafts[];
  let writes: Promise<void>[];
  do {
    drafts = [...mountedDrafts];
    writes = drafts.map(draft => draft.settled());
    await Promise.all(writes);
    await Promise.allSettled([...pendingOutboxWork]);
    await tick();
  } while (pendingOutboxWork.size > 0 || mountedDrafts.size !== drafts.length
    || drafts.some((draft, index) => draft.settled() !== writes[index]));
  expect(outboxFailures).toEqual([]);
}
const conversation = {
  conversation_id: fixture.conversationId, agent_id: 'fixture-agent', status: 'active',
  title: 'Synthetic parity', context: { type: 'web' }, has_message_history: true,
};
const agent = { agent_id: 'fixture-agent', name: 'Fixture agent', enabled: true };
function snapshot(): ChatSnapshot {
  return {
    schema_version: 2, projection_version: 'test', scope, conversation,
    cursor: 'mounted:initial', server_time: '2026-01-01T00:00:00Z',
    timeline: { items: [{
      id: 'mounted:message', kind: 'message', role: 'assistant', message_id: 'mounted:message',
      content: 'Canonical mounted parity row', sort_key: '0001', source_refs: [], stable: true,
      status: 'complete', attachments: [], partial: false,
    }], has_more_before: false, before_cursor: null },
    state: { state_version: 1, snapshot_generated_at: '', capabilities: [], active_turn: {}, pending: {}, active_session: {} },
    queue: { messages: [], queued_count: 0 }, runtime: null,
  } as unknown as ChatSnapshot;
}

function historyRow(i: number) {
  return {
    ...snapshot().timeline.items[0], id: `history-row-${i}`, message_id: `history-row-${i}`,
    sort_key: String(i + 1000).padStart(5, '0'), content: `Synthetic history row ${i}`,
  };
}

describe('mounted full and embedded timeline parity (synthetic local data only)', () => {
  it.each(['main', 'compact'] as const)(
    'binds reviewed feedback after delayed hydration and preserves full input across %s conversation switches',
    async surface => {
      vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
      // Later tests must not inherit a motion-enabled matchMedia that jsdom cannot animate.
      onTestFinished(() => { vi.unstubAllGlobals(); });
      const id = fixture.conversationId, otherId = 'mounted-other-feedback';
      const subject = crypto.randomUUID();
      const entries: FeedbackEntryVersion[] = Array.from({ length: 3 }, (_, index) => ({
        entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), version: 1,
        thread_id: crypto.randomUUID(), body: `Exact mounted feedback ${index}`,
        author: { kind: 'verified_user', label: 'Reviewer', is_self: true }, audience: 'private',
        reply_to_entry_id: null, created_at: '2026-01-01T00:00:00Z', delivery: [],
      }));
      const staging: FeedbackStagingSnapshot = { conversation_id: id, version: 1, receipts: [],
        items: entries.map(entry => ({ entry_id: entry.entry_id, entry_version_id: entry.entry_version_id,
          staging_version: 1, inclusion: 'prepared', subject_id: subject, anchor_id: crypto.randomUUID() })) };
      const packet: FeedbackPreflight = {
        preflight_id: crypto.randomUUID(), manifest_hash: 'c'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
        destination: { kind: 'origin_chat', conversation_id: id }, update_mode: 'revise_source',
        selection: [], source_summaries: [], warnings: [], task_consequences: null,
        context_items: [{ role: 'instruction', entry: entries[0], anchor: {
          schema_version: 1, anchor_id: staging.items[0].anchor_id,
          source: { kind: 'document', content_ref: 'art_mounted_feedback' },
          content_digest: 'd'.repeat(64), locator: { kind: 'whole' },
        } }],
      };
      vi.spyOn(feedbackApi, 'staging').mockImplementation(async asked => structuredClone(asked === id
        ? staging : { conversation_id: asked, version: 1, receipts: [], items: [] }));
      vi.spyOn(feedbackApi, 'threads').mockResolvedValue({
        items: entries.map(entry => ({
          thread_id: entry.thread_id, state: 'open', entries: [entry],
          anchor: packet.context_items[0].anchor,
        })), next_cursor: null,
      } as never);
      vi.spyOn(feedbackApi, 'stage').mockImplementation(async (_id, entryId, request) => {
        const item = staging.items.find(item => item.entry_id === entryId)!;
        expect(request.expected_version).toBe(item.staging_version);
        Object.assign(item, { staging_version: item.staging_version + 1, inclusion: request.inclusion });
        return structuredClone(item);
      });
      vi.spyOn(feedbackApi, 'preflight').mockImplementation(async request => ({
        ...packet, selection: structuredClone(request.selection),
      }));
      const composer = conversationComposer();
      const attachments = [{ artifact_id: 'art_mounted_attachment', filename: 'retained.png',
        mime_type: 'image/png', kind: 'image' as const, size_bytes: 1 }];
      composer.update(id, { text: 'Retained instruction', attachments, chat_mode: 'plan',
        input_revision: crypto.randomUUID() });
      composer.update(otherId, { text: 'Other conversation draft', attachments: [],
        chat_mode: 'build', input_revision: crypto.randomUUID() });
      const stop = vi.fn();
      const subscribe = composer.subscribe.bind(composer);
      vi.spyOn(composer, 'subscribe').mockImplementation((conversationId, listener) => {
        const unsubscribe = subscribe(conversationId, listener);
        return () => { stop(conversationId); unsubscribe(); };
      });
      const detail = deferred<typeof conversation>();
      let firstDetail = true;
      vi.mocked(api.conversations.detail).mockImplementation(async asked => {
        if (asked === id && firstDetail) { firstDetail = false; return detail.promise as never; }
        return { ...conversation, conversation_id: asked } as never;
      });
      vi.mocked(chatV2Api.snapshot).mockImplementation(async requested => {
        const target = typeof requested === 'string' ? conversationTimelineScope(requested) : requested;
        return { ...snapshot(), scope: target, conversation: { ...conversation, conversation_id: target.conversation_id! } };
      });
      const mainView = surface === 'main' ? render(FullChat) : null;
      const compactView = surface === 'compact' ? render(CompactConversationChat, { conversationId: id, agent: agent as never }) : null;
      const view = mainView ?? compactView!;
      await tick();
      detail.resolve(conversation);
      const label = surface === 'main' ? 'Chat message' : 'Conversation message';
      await waitFor(() => expect(screen.getByRole('textbox', { name: label })).toHaveValue('Retained instruction'));
      const tray = () => within(screen.getByRole('group', { name: 'Message feedback' }));
      await tray().findByRole('button', { name: 'Add 3 comments to message' });
      // The main chat chooses in its Comments panel; the compact chat opens a small list above the composer.
      await fireEvent.click(tray().getByRole('button', { name: 'Choose comments' }));
      // The list loads lazily (its module and every staged thread), which can exceed the default wait under load.
      await fireEvent.click(await screen.findByRole('checkbox', { name: /Exact mounted feedback 0/ }, { timeout: 5000 }));
      await tray().findByRole('button', { name: 'Comments in message: 1' }, { timeout: 5000 });
      expect(tray().getByRole('button', { name: 'Add 2 comments to message' })).toBeVisible();
      const included = composer.hydrate(id).input;
      expect(included).toMatchObject({ text: 'Retained instruction', attachments, chat_mode: 'plan',
        feedback: { manifest_hash: packet.manifest_hash, preflight_id: packet.preflight_id } });
      expect(conversationFeedback(id).review(included.feedback!)?.selection).toEqual([
        { entry_version_id: entries[0].entry_version_id, expected_entry_version: 1, staging_version: 2 },
      ]);
      await fireEvent.input(screen.getByRole('textbox', { name: label }), { target: { value: 'Later typed instruction' } });
      await waitFor(() => expect(hydrateComposer(new DocumentDrafts(), id).input).toMatchObject({
        text: 'Later typed instruction', attachments, chat_mode: 'plan', feedback: included.feedback,
      }));
      if (surface === 'main') fixture.navigate(otherId);
      else await compactView!.rerender({ conversationId: otherId, agent: agent as never });
      await waitFor(() => expect(screen.getByRole('textbox', { name: label })).toHaveValue('Other conversation draft'));
      expect(stop).toHaveBeenCalledWith(id);
      composer.update(id, { ...composer.hydrate(id).input, text: 'Late update to inactive conversation',
        input_revision: crypto.randomUUID() });
      await tick();
      expect(screen.getByRole('textbox', { name: label })).toHaveValue('Other conversation draft');
      // The other conversation never shows this conversation's attached feedback.
      expect(screen.queryByText(/attached to your next message/)).toBeNull();
      if (surface === 'main') fixture.navigate(id);
      else await compactView!.rerender({ conversationId: id, agent: agent as never });
      await waitFor(() => expect(screen.getByRole('textbox', { name: label })).toHaveValue('Late update to inactive conversation'));
      await tray().findByRole('button', { name: 'Comments in message: 1' });
      expect(composer.hydrate(id).input).toMatchObject({ attachments, chat_mode: 'plan', feedback: included.feedback });
      view.unmount();
      expect(stop).toHaveBeenCalledWith(otherId);
      await settleFixtureOutboxWork();
      await documentCoordination().beginLogin();
      expect(() => composer.update(id, included)).toThrow();
    },
    MOUNTED_SCENARIO_TIMEOUT_MS,
  );

  it('renders sessionless metadata before a runtime model selection is available', async () => {
    expect(conversation).not.toHaveProperty('active_session_id');
    expect(snapshot().runtime).toBeNull();
    expect(snapshot().state.active_session).toEqual({});

    const full = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    expect(full.container.querySelector('[data-testid="conversation-metadata"]')).not.toBeNull();
    expect(screen.getByRole('textbox', { name: 'Chat message' })).toBeEnabled();
    full.unmount();
  });

  it('keeps chat diagnostics inside the Session tab context and runtime disclosure', async () => {
    const activeConversation = { ...conversation, active_session_id: 'mounted-intaris-session' };
    vi.mocked(api.conversations.detail).mockResolvedValue(activeConversation as never);
    vi.mocked(chatV2Api.snapshot).mockImplementation(async () => ({
      ...snapshot(), conversation: activeConversation,
    }));
    vi.spyOn(api.sessions, 'intarisDetail').mockResolvedValue({
      intaris_session_id: 'mounted-intaris-session', intention: 'Synthetic session details',
      summary: null, status: 'running', total_calls: 12, approved_count: 12, denied_count: 0, escalated_count: 0,
    } as never);
    const full = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    await fireEvent.click(screen.getByTestId('chat-header-info'));
    expect(screen.queryByTestId('timeline-diagnostic-export')).toBeNull();
    await fireEvent.click(screen.getByRole('tab', { name: /^Session$/ }));
    const runtimeDetails = await screen.findByTestId('session-details-diagnostics');
    const diagnostics = await within(runtimeDetails).findByTestId('timeline-diagnostic-export');
    expect(runtimeDetails).not.toHaveAttribute('open');
    expect(runtimeDetails.lastElementChild).toBe(diagnostics);
    await fireEvent.click(runtimeDetails.querySelector('summary')!);
    await waitFor(() => expect(within(diagnostics).getByRole('button', { name: 'Download chat diagnostics' })).toBeEnabled());
    await fireEvent.click(screen.getByRole('tab', { name: /^Overview$/ }));
    expect(screen.queryByTestId('timeline-diagnostic-export')).toBeNull();
    full.unmount();
  });

  it.each(['command', 'clarification'])('consumes an autosaved %s without restoring it on reopen', async kind => {
    const value = kind === 'command' ? '/help' : 'The clarification answer';
    if (kind === 'clarification') {
      vi.mocked(chatV2Api.snapshot).mockImplementation(async () => {
        const base = snapshot();
        // Exercise the supported composer fallback when canonical tool state
        // precedes the separate notification mirror, not the question panel.
        return { ...base, timeline: { ...base.timeline, items: [...base.timeline.items, {
          id: 'mounted-question-tool', kind: 'tool_call', call_id: 'mounted-question-tool',
          tool_name: 'step_request_questions', sort_key: '0002', source_refs: [], stable: true,
          status: 'waiting', arguments: { notification_id: 'mounted-clarification' },
          is_error: false, attachments: [], file_diffs: [], truncated: false, has_full_output: false,
        }] } };
      });
    }
    const command = vi.spyOn(chatV2Api, 'executeCommand').mockResolvedValue({
      conversation_id: fixture.conversationId, client_txn_id: 'mounted-command',
      status: 'completed', result_type: 'text', text: 'Command completed', data: {}, server_time: '',
    });
    const resolve = vi.spyOn(api.notifications, 'resolve').mockImplementation(async () => {
      vi.mocked(api.notifications.list).mockResolvedValue([]);
      return {} as never;
    });
    const full = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    const composer = screen.getByRole('textbox', { name: 'Chat message' }) as HTMLTextAreaElement;
    await fireEvent.input(composer, { target: { value } });
    await waitFor(() => expect(hydrateComposer(new DocumentDrafts(), fixture.conversationId).input.text).toBe(value));
    await fireEvent.submit(composer.closest('form')!);
    await waitFor(() => expect(kind === 'command' ? command : resolve).toHaveBeenCalledOnce());
    await waitFor(() => expect(composer.value).toBe(''));
    await waitFor(() => expect(hydrateComposer(new DocumentDrafts(), fixture.conversationId).input.text).toBe(''));
    full.unmount();
    const reopened = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    expect(screen.getByRole('textbox', { name: 'Chat message' })).toHaveValue('');
    reopened.unmount();
  });

  it('preserves newer autosaved input while a command waits for owner validation', async () => {
    const command = vi.spyOn(chatV2Api, 'executeCommand').mockResolvedValue({
      conversation_id: fixture.conversationId, client_txn_id: 'mounted-delayed-command',
      status: 'completed', result_type: 'text', text: 'Command completed', data: {}, server_time: '',
    });
    const full = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    const composer = screen.getByRole('textbox', { name: 'Chat message' }) as HTMLTextAreaElement;
    await fireEvent.input(composer, { target: { value: '/help' } });
    await waitFor(() => expect(hydrateComposer(new DocumentDrafts(), fixture.conversationId).input.text).toBe('/help'));
    const owner = documentCoordination();
    const lease = owner.capture();
    let release!: () => void;
    const held = new Promise<void>(resolve => { release = resolve; });
    const validate = vi.fn(async () => { await held; await lease.validate(); });
    vi.spyOn(owner, 'capture').mockReturnValue({ ...lease, validate });
    await fireEvent.submit(composer.closest('form')!);
    await waitFor(() => expect(validate).toHaveBeenCalledOnce());
    await fireEvent.input(composer, { target: { value: 'Newer input' } });
    await waitFor(() => expect(hydrateComposer(new DocumentDrafts(), fixture.conversationId).input.text).toBe('Newer input'));
    release();
    await waitFor(() => expect(command).toHaveBeenCalledOnce());
    expect(composer.value).toBe('Newer input');
    full.unmount();
    const reopened = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    expect(screen.getByRole('textbox', { name: 'Chat message' })).toHaveValue('Newer input');
    reopened.unmount();
  });

  it.each([false, true])('retains a paused full window through two scoped backfills and live appends (hidden=%s)', async hidden => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(performance.now()), 16));
    vi.stubGlobal('cancelAnimationFrame', clearTimeout);
    const row = historyRow;
    vi.mocked(chatV2Api.snapshot).mockResolvedValue({
      ...snapshot(),
      timeline: { items: Array.from({ length: 200 }, (_, i) => hidden && i === 151 ? {
        ...row(i), kind: 'notice' as const, title: 'Synthetic child hook', level: 'info' as const,
        hook: { run_id: 'synthetic-hook-run', hook_id: 'synthetic-hook',
          status: 'completed', child_session_id: 'synthetic-child-session', trigger: 'conversation_idle' },
      } : row(i)),
        has_more_before: true, before_cursor: 'history:older' },
    });
    for (let page = 1; page <= 2; page++) {
      vi.mocked(chatV2Api.timeline).mockResolvedValueOnce({
        schema_version: 2, projection_version: 'test', scope, conversation_id: fixture.conversationId,
        items: Array.from({ length: 8 }, (_, i) => row(i - page * 8)),
        has_more_before: page === 1, before_cursor: page === 1 ? 'history:older:2' : null, server_time: '',
      });
    }
    const full = render(FullChat);
    const scoped = render(ScopedChatV2Timeline, { scope, compact: true, autoBackfill: false, hasEditableComposer: true });
    await waitFor(() => expect(full.container.textContent).toContain('Synthetic history row 199'));
    await waitFor(() => expect(scoped.container.textContent).toContain('Synthetic history row 199'));
    const node = full.container.querySelector<HTMLElement>('[data-timeline-row-key]')!;
    const viewport = node.closest<HTMLElement>('[class*="overflow-y-auto"]')!;
    expect(viewport).not.toBeNull();
    Object.defineProperty(viewport, 'scrollHeight', { configurable: true, value: 20_000 });
    Object.defineProperty(viewport, 'clientHeight', { configurable: true, value: 600 });
    await new Promise(resolve => setTimeout(resolve, 250));
    viewport.scrollTop = 800;
    await fireEvent.wheel(viewport, { deltaY: -100 });
    await tick();
    expect(full.container.querySelector('[aria-label="Scroll to bottom"]')).not.toBeNull();
    const keys = () => [...viewport.querySelectorAll<HTMLElement>('[data-timeline-row-key]')].map(e => e.dataset.timelineRowKey);
    const before = keys();
    if (hidden) {
      vi.spyOn(api.sessions, 'detail').mockResolvedValue({
        session_id: 'synthetic-child-session', conversation_id: fixture.conversationId,
        intaris_session_id: null, agent_id: agent.agent_id, status: 'completed',
        delegation_mode: 'agent', delegation_task: 'Synthetic child hook', activity_scope_id: null,
      } as never);
      const hook = full.container.querySelector<HTMLElement>('[aria-label="Background hook"]')!;
      await fireEvent.click(within(hook).getByRole('button', { name: 'Expand' }));
      await fireEvent.click(within(hook).getByRole('button', { name: 'Open session' }));
      await waitFor(() => expect(full.container.querySelector('[data-testid="child-header"]')).not.toBeNull());
    }
    const lease = acquireTimeline(scope);
    try {
      const scopedViewport = scoped.container.querySelector<HTMLElement>('[data-testid="scoped-timeline-viewport"]')!;
      await waitFor(() => expect(scopedViewport.classList.contains('invisible')).toBe(false));
      for (let page = 1; page <= 2; page++) {
        await fireEvent.wheel(scopedViewport, { deltaY: -100 });
        await waitFor(() => expect(chatV2Api.timeline).toHaveBeenCalledTimes(page));
        await waitFor(() => expect(scoped.container.textContent).toContain(`Synthetic history row ${-page * 8}`));
        expect(keys()).toEqual(before);
        const state = lease.controller.store.snapshot;
        lease.controller.store.applyRealtime({
          type: 'chat_v2_frame', schema_version: 2, projection_version: 'test', scope,
          conversation_id: fixture.conversationId, cursor_before: state.cursor, cursor_after: `history:live:${page}`,
          ops: [{ op: 'upsert_item', item: row(200 + page) },
            { op: 'upsert_item', item: { ...row(150), content: `Live update ${page}` } }],
          runtime: null, server_time: '',
        } as never);
        await tick();
        expect(keys()).toEqual(before);
        expect(full.container.textContent).toContain(`Live update ${page}`);
      }
      expect(lease.controller.store.snapshot.hasMoreBefore).toBe(false);
      if (hidden) {
        const header = full.container.querySelector<HTMLElement>('[data-testid="child-header"]');
        expect(header).not.toBeNull();
        expect(header!.isConnected).toBe(true);
        await fireEvent.click(within(header!).getByRole('link', { name: 'Back to parent conversation' }));
        await waitFor(() => expect(full.container.querySelector('[data-testid="child-header"]')).toBeNull());
        expect(keys()).toEqual(before);
      }
      expect(full.container.textContent).toContain('Synthetic history row 199');
      expect(vi.mocked(chatV2Api.snapshot).mock.calls.filter(([asked]) =>
        typeof asked !== 'string' && asked.key === scope.key)).toHaveLength(1);
    } finally {
      lease.release();
      full.unmount();
      scoped.unmount();
      await new Promise(resolve => setTimeout(resolve, 60));
    }
  }, MOUNTED_SCENARIO_TIMEOUT_MS);

  it('propagates mounted live replacements alternating older pages and survives the first view teardown', async () => {
    const listeners = new Set<Parameters<typeof wsClient.subscribe>[0]>();
    vi.spyOn(wsClient, 'subscribe').mockImplementation(listener => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    });
    vi.mocked(chatV2Api.snapshot).mockResolvedValue({
      ...snapshot(), timeline: { ...snapshot().timeline, has_more_before: true, before_cursor: 'older:0' },
    });
    vi.mocked(chatV2Api.timeline).mockImplementation(async (_scope, options) => {
      const page = Number(options?.before?.split(':')[1] ?? 0) + 1;
      return {
        schema_version: 2, projection_version: 'test', scope, conversation_id: fixture.conversationId,
        server_time: '', before_cursor: `older:${page}`, has_more_before: true,
        items: [{ ...snapshot().timeline.items[0], id: `older-row:${page}`, message_id: `older-row:${page}`,
          content: `Older synthetic page ${page}`, sort_key: `-${page}` }],
      };
    });
    const full = render(FullChat);
    const scoped = render(ScopedChatV2Timeline, { scope, compact: true, autoBackfill: false });
    await waitFor(() => expect(screen.getAllByText('Canonical mounted parity row')).toHaveLength(2));
    const lease = acquireTimeline(scope);
    let fullAlive = true;
    try {
      for (let i = 1; i <= 3; i++) {
        const store = lease.controller.store;
        const state = store.snapshot;
        const item = { ...snapshot().timeline.items[0], content: `Synthetic live revision ${i}` };
        const frame = {
          type: 'chat_v2_frame', schema_version: 2, projection_version: 'test', scope,
          conversation_id: fixture.conversationId, cursor_before: state.cursor,
          cursor_after: `mounted:live:${i}`, ops: [{ op: 'upsert_item', item }],
          runtime: null, server_time: '2026-01-01T00:00:00Z',
        };
        for (const listener of listeners) listener(frame as never);
        expect(store.snapshot.cursor).toBe(frame.cursor_after);
        expect(store.visibleItems.find(row => row.id === item.id)).toMatchObject({ content: item.content });
        await tick();
        expect(full.container.textContent).toContain(item.content);
        expect(scoped.container.textContent).toContain(item.content);
        await lease.controller.older(`older:${i - 1}`);
        await tick();
        expect(full.container.textContent).toContain(`Older synthetic page ${i}`);
        expect(scoped.container.textContent).toContain(`Older synthetic page ${i}`);
      }
      full.unmount();
      fullAlive = false;
      const item = { ...snapshot().timeline.items[0], content: 'Live after first view destroyed' };
      for (const listener of listeners) listener({
        type: 'chat_v2_frame', schema_version: 2, projection_version: 'test', scope,
        conversation_id: fixture.conversationId, cursor_before: lease.controller.store.snapshot.cursor,
        cursor_after: 'mounted:after-first-destroy', ops: [{ op: 'upsert_item', item }], runtime: null,
      } as never);
      await tick();
      expect(scoped.container.textContent).toContain(item.content);
    } finally {
      lease.release();
      if (fullAlive) full.unmount();
      scoped.unmount();
    }
  });

  it('completes window-only upward loading with RAF suspended and live append pending', async () => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => setTimeout(() => callback(performance.now()), 16));
    vi.stubGlobal('cancelAnimationFrame', clearTimeout);
    vi.mocked(chatV2Api.snapshot).mockResolvedValue({
      ...snapshot(), timeline: { items: Array.from({ length: 200 }, (_, i) => historyRow(i)),
        has_more_before: true, before_cursor: 'history:older' },
    });
    vi.mocked(chatV2Api.timeline).mockResolvedValue({
      schema_version: 2, projection_version: 'test', scope, conversation_id: fixture.conversationId,
      items: [historyRow(-1)], has_more_before: false, before_cursor: null, server_time: '',
    });
    const full = render(FullChat);
    await waitFor(() => expect(full.container.textContent).toContain('Synthetic history row 199'));
    const viewport = full.container.querySelector<HTMLElement>('[data-testid="timeline-viewport"]')!;
    Object.defineProperty(viewport, 'scrollHeight', { configurable: true, value: 20_000 });
    Object.defineProperty(viewport, 'clientHeight', { configurable: true, value: 600 });
    await settleFixtureOutboxWork();
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
    viewport.scrollTop = 0;
    await fireEvent.wheel(viewport, { deltaY: -100 });
    await fireEvent.scroll(viewport);
    await waitFor(() => expect(full.container.textContent).toContain('Synthetic history row 50'));
    const lease = acquireTimeline(scope);
    try {
      lease.controller.store.applyRealtime({
        type: 'chat_v2_frame', schema_version: 2, projection_version: 'test', scope,
        conversation_id: fixture.conversationId, cursor_before: lease.controller.store.snapshot.cursor,
        cursor_after: 'history:live:raf-suspended', ops: [{ op: 'upsert_item', item: historyRow(201) }],
        runtime: null, server_time: '',
      } as never);
      await tick();
      expect(full.container.querySelector('[role="status"]')?.textContent ?? '').not.toContain('Loading earlier');
      await fireEvent.wheel(viewport, { deltaY: -100 });
      await fireEvent.scroll(viewport);
      await waitFor(() => expect(full.container.textContent).toContain('Synthetic history row 0'));
      await fireEvent.wheel(viewport, { deltaY: -100 });
      await fireEvent.scroll(viewport);
      await waitFor(() => expect(chatV2Api.timeline).toHaveBeenCalledOnce());
      await waitFor(() => expect(full.container.textContent).toContain('Synthetic history row -1'));
    } finally {
      try {
        await settleFixtureOutboxWork();
      } finally {
        lease.release();
        full.unmount();
      }
    }
  }, MOUNTED_SCENARIO_TIMEOUT_MS);

  beforeEach(async () => {
    expect(pendingOutboxWork.size).toBe(0);
    expect(mountedDrafts.size).toBe(0);
    // Keep the coordinator's file-lifetime metadata without JSDOM storage deadlines.
    // Real coordination, epoch validation and outbox persistence remain in use.
    vi.spyOn(ProfileIntentStore.prototype, 'transact').mockImplementation(async (profile, change) => {
      const raw = structuredClone(fixtureProfileIntents.get(profile));
      const result = change(raw);
      if (result !== raw) fixtureProfileIntents.set(profile, structuredClone(result));
      return result;
    });
    clearTimelineMemory();
    fixture.realCache = false;
    fixture.navigate(fixture.conversationId);
    outboxFailures.length = 0;
    fixture.rows.clear();
    fixture.stores = 0;
    fixture.snapshotProjections = 0;
    fixture.stateRestores = 0;
    localStorage.clear();
    // In-memory coordination fixture only: no auth endpoint, browser session
    // state, credentials or external service is used by this mounted test.
    await documentCoordination().beginLogin();
    await documentCoordination().verified('mounted-fixture@cognis.test');
    fixture.owner = documentCoordination().storageName('offline-cache');
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('Unexpected network access in local fixture'); }));
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
    vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1; });
    vi.stubGlobal('cancelAnimationFrame', vi.fn());
    const readComposite = DocumentDrafts.prototype.readComposite;
    vi.spyOn(DocumentDrafts.prototype, 'readComposite').mockImplementation(function (
      this: DocumentDrafts, ...args: Parameters<typeof readComposite>
    ) {
      mountedDrafts.add(this);
      return readComposite.apply(this, args);
    });
    vi.spyOn(api.conversations, 'detail').mockResolvedValue(conversation as never);
    vi.spyOn(api.conversations, 'sessions').mockResolvedValue([]);
    vi.spyOn(api.conversations, 'sidebar').mockResolvedValue({
      conversations: [conversation], items: [conversation], agents: [agent], context_types: ['web'],
      next_cursor: null, has_more: false, server_time: '2026-01-01T00:00:00Z', background_work: { items: [] },
    } as never);
    vi.spyOn(api.conversations, 'contextTypes').mockResolvedValue(['web']);
    vi.spyOn(api.conversations, 'waiting').mockResolvedValue([]);
    vi.spyOn(api.conversations, 'markRead').mockResolvedValue(undefined as never);
    vi.spyOn(api.conversations, 'rememberOpened').mockResolvedValue(undefined as never);
    vi.spyOn(api.agents, 'list').mockResolvedValue([agent] as never);
    vi.spyOn(api.projects, 'list').mockResolvedValue([]);
    vi.spyOn(api.notifications, 'list').mockResolvedValue([]);
    vi.spyOn(api.search, 'health').mockResolvedValue({ enabled: false } as never);
    vi.spyOn(api.userPreferences, 'get').mockResolvedValue(DEFAULT_USER_PREFERENCES);
    vi.spyOn(chatV2Api, 'snapshot').mockImplementation(async () => snapshot());
    vi.spyOn(chatV2Api, 'sync').mockImplementation(async (_scope, cursor) => ({
      schema_version: 2, projection_version: 'test', conversation_id: fixture.conversationId, scope,
      cursor_before: cursor, cursor_after: cursor, ops: [], has_more: false, runtime: null,
      reset_required: false, server_time: '2026-01-01T00:00:00Z',
    }));
    vi.spyOn(chatV2Api, 'timeline').mockRejectedValue(new Error('No history page expected'));
    // Synthetic rows have no durable receipts. Keep this new optional REST
    // operation local, like the fixture's snapshot/sync/backfill operations.
    vi.spyOn(chatV2Api, 'messageAdmission').mockRejectedValue(new Error('No synthetic admission receipt'));
    const read = CoordinatedOutbox.prototype.list;
    vi.spyOn(CoordinatedOutbox.prototype, 'list').mockImplementation(function (this: CoordinatedOutbox, id?: string) {
      return trackOutboxWork(read.call(this, id));
    });
    const drain = outboxSending.drainConversationOutbox;
    vi.spyOn(outboxSending, 'drainConversationOutbox').mockImplementation(options => trackOutboxWork(drain(options)));
  });
  it.each(['failed', 'sending', 'route-before', 'account-before', 'route-after', 'account-after',
    'overlap-after', 'overlap-interrupted-after', 'overlap-account-after', 'overlap-stale-after', 'overlap-destroy-after'] as const)(
    'retries durable %s admission on real cached reopen with original identity', async scenario => {
    const status = scenario === 'sending' ? 'sending' : 'failed';
    const { offlineCache: realCache } = await vi.importActual<typeof import('$lib/offline/cache')>('$lib/offline/cache');
    fixture.realCache = true;
    const owner = documentCoordination();
    await owner.beginLogin();
    await owner.verified(`cached-${scenario}@cognis.test`);
    const lease = owner.capture();
    await realCache.setOwner(owner.storageName('offline-cache', lease));
    const fields = queuedComposerRuntime(fixture.conversationId);
    vi.mocked(chatV2Api.snapshot).mockResolvedValue({ ...snapshot(), ...fields });
    const recoveryGate = deferred<void>();
    const admissionGate = deferred<void>();
    const otherDetailGate = deferred<void>();
    let otherDetailStarted = false;
    const completedAuthorities: chatPage.CanonicalTimelineAuthority[] = [];
    const transition = chatPage.transitionCanonicalTimelineAuthority;
    vi.spyOn(chatPage, 'transitionCanonicalTimelineAuthority').mockImplementation((...args) => {
      const authority = transition(...args);
      if (authority.status === 'ready' && authority.baseAccepted) completedAuthorities.push(authority);
      return authority;
    });
    const backfill = vi.spyOn(chatPage, 'backfillInitialVisibleHistory');
    const projectedAdmissions = vi.spyOn(ChatV2Store.prototype, 'applySend');
    let settledRefreshes = 0;
    const refresh = chatPage.startCachedTimelineRefresh;
    vi.spyOn(chatPage, 'startCachedTimelineRefresh').mockImplementation((operation, settled, isCurrent) =>
      refresh(operation, () => {
        settled();
        settledRefreshes += 1;
      }, isCurrent));
    const completedFor = (id: string) => completedAuthorities.filter(authority => authority.conversationId === id);
    const expectCompletion = async (id: string, previous: number) => {
      await waitFor(() => expect(completedFor(id).length).toBeGreaterThan(previous));
      expect(completedFor(id).at(-1)).toMatchObject({ status: 'ready', baseAccepted: true });
      expect(backfill).toHaveBeenCalled();
      await expect(backfill.mock.results.at(-1)!.value).resolves.toMatch(/visible|exhausted|budget_exhausted/);
    };
    let pendingReads = 0;
    const list = vi.mocked(CoordinatedOutbox.prototype.list).getMockImplementation()!;
    vi.spyOn(CoordinatedOutbox.prototype, 'list').mockImplementation(function (this: CoordinatedOutbox, id?: string) {
      pendingReads += 1;
      return list.call(this, id).finally(() => { pendingReads -= 1; });
    });
    vi.mocked(api.conversations.detail).mockImplementation(async id => {
      if (id === 'mounted-other') {
        otherDetailStarted = true;
        if (scenario === 'overlap-interrupted-after') await otherDetailGate.promise;
      }
      return { ...conversation, conversation_id: id } as never;
    });
    vi.mocked(chatV2Api.snapshot).mockImplementation(async requestedScope => {
      const targetScope = typeof requestedScope === 'string' ? conversationTimelineScope(requestedScope) : requestedScope;
      const id = targetScope.conversation_id!;
      return {
        ...snapshot(), scope: targetScope,
        conversation: { ...conversation, conversation_id: id },
        ...queuedComposerRuntime(id),
      };
    });
    let recoveryCompleted = false;
    let holdRecovery = false;
    vi.mocked(chatV2Api.sync).mockImplementation(async (requestedScope, cursor) => {
      const targetScope = typeof requestedScope === 'string' ? conversationTimelineScope(requestedScope) : requestedScope;
      if (holdRecovery && scenario.endsWith('-before')) await recoveryGate.promise;
      recoveryCompleted = true;
      return {
      schema_version: 2, projection_version: 'test', conversation_id: targetScope.conversation_id!, scope: targetScope,
      cursor_before: cursor, cursor_after: cursor, ops: [], has_more: false,
      runtime: fields.runtime, reset_required: false, server_time: fields.runtime.generated_at,
    }; });
    const warm = acquireTimeline(scope);
    await warm.controller.load();
    await waitFor(async () => expect(await realCache.get('timeline', scope.key)).not.toBeNull());
    warm.release();
    clearTimelineMemory();
    await realCache.put('conversation-view', fixture.conversationId, {
      conversation, sessions: [], queuedCount: 0, queuedMessages: [],
      contextUsage: null, sessionInfo: null, visibleStartIndex: 0, scrollTop: 0,
      userScrolledUp: false, viewportAnchor: null, turnInProgress: false,
      awaitingAssistantStart: false, activeSessionLastSeq: 0,
      olderMessagesCursor: null, hasOlderMessages: false,
      activeTurnChatMode: 'default', fetchedAt: Date.now(),
    }, realCache.currentOwner);
    expect(await realCache.get('conversation-view', fixture.conversationId)).toMatchObject({
      conversation: { conversation_id: fixture.conversationId }, turnInProgress: false,
    });
    expect(await realCache.get('timeline', scope.key)).not.toBeNull();
    holdRecovery = true;
    recoveryCompleted = false;
    vi.mocked(chatV2Api.sync).mockClear();
    vi.mocked(chatV2Api.snapshot).mockClear();
    const outbox = createIndexedDbChatV2Outbox();
    const id = crypto.randomUUID(), messageId = crypto.randomUUID();
    const attachment = { artifact_id: 'synthetic-upload-receipt', filename: 'synthetic.png',
      mime_type: 'image/png', kind: 'image', size_bytes: 1 };
    await outbox.put({
      client_txn_id: id, client_message_id: messageId, conversation_id: fixture.conversationId,
      content: 'Cached retry', attachments: [attachment], status, retryable: true,
      created_at: '2030-04-16T12:00:00Z', updated_at: '2030-04-16T12:00:00Z',
    });
    if (scenario.endsWith('-after')) {
      await outbox.put({
        client_txn_id: `${id}-next`, client_message_id: `${messageId}-next`,
        conversation_id: fixture.conversationId, content: 'Next retained retry',
        attachments: [], status: 'failed', retryable: true,
        created_at: '2030-04-16T12:00:01Z', updated_at: '2030-04-16T12:00:01Z',
      });
    }
    const receipt = { status: 'accepted' as const, client_txn_id: id, client_message_id: messageId,
      conversation_id: fixture.conversationId, server_time: '2030-04-16T12:00:00Z' };
    const send = vi.spyOn(chatV2Api, 'sendMessage').mockImplementation(async (conversationId, transactionId, request) => {
      if (scenario.endsWith('-after')) await admissionGate.promise;
      return { ...receipt, conversation_id: conversationId, client_txn_id: transactionId,
        client_message_id: request.client_message_id };
    });
    const full = render(FullChat);
    let fullAlive = true;
    try {
      await screen.findByText('Canonical mounted parity row');
      await waitFor(() => expect(chatV2Api.sync).toHaveBeenCalled());
      expect(chatV2Api.snapshot).not.toHaveBeenCalled();
      if (scenario.startsWith('overlap-')) {
        await waitFor(() => expect(send).toHaveBeenCalledOnce());
        await expectCompletion(fixture.conversationId, 0);
        await waitFor(() => expect(settledRefreshes).toBeGreaterThan(0));
        const originalId = fixture.conversationId;
        // Each revisit completes canonical recovery while the original send
        // remains blocked; only the newest eligible request may survive.
        for (let revisit = 0; revisit < 2; revisit++) {
          const previousA = completedFor(originalId).length;
          const previousB = completedFor('mounted-other').length;
          otherDetailStarted = false;
          fixture.navigate('mounted-other');
          await tick();
          await waitFor(() => expect(otherDetailStarted).toBe(true));
          if (scenario !== 'overlap-interrupted-after') {
            await expectCompletion('mounted-other', previousB);
          }
          fixture.navigate(originalId);
          await tick();
          await expectCompletion(originalId, previousA);
          await tick();
          await waitFor(() => expect(pendingReads).toBe(0));
        }
        expect(send).toHaveBeenCalledOnce();
        if (scenario === 'overlap-interrupted-after') {
          const lastA = completedFor(originalId).at(-1);
          otherDetailGate.resolve();
          await tick();
          await waitFor(() => expect(pendingReads).toBe(0));
          expect(completedAuthorities.at(-1)).toEqual(lastA);
          expect(peekTimeline(scope)?.conversationId).toBe(originalId);
        }
        if (scenario === 'overlap-account-after') {
          await owner.beginLogin();
          await realCache.setOwner(null);
          await tick();
        } else if (scenario === 'overlap-stale-after') {
          fixture.navigate('mounted-other');
          await tick();
        } else if (scenario === 'overlap-destroy-after') {
          full.unmount();
          fullAlive = false;
        }
        admissionGate.resolve();
        await settleFixtureOutboxWork();
        if (scenario === 'overlap-after' || scenario === 'overlap-interrupted-after') {
          await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
          expect(send.mock.calls.map(call => call.slice(0, 2))).toEqual([
            [originalId, id], [originalId, `${id}-next`],
          ]);
          expect(send.mock.calls[1][2].client_message_id).toBe(`${messageId}-next`);
          await waitFor(async () => expect(await outbox.get(`${id}-next`)).toMatchObject({
            status: 'acked', admitted: { client_txn_id: `${id}-next`, client_message_id: `${messageId}-next` },
          }));
        } else {
          expect(send).toHaveBeenCalledOnce();
          expect(projectedAdmissions).not.toHaveBeenCalled();
        }
        return;
      }
      if (scenario.includes('-')) {
        if (scenario.endsWith('-after')) await waitFor(() => expect(send).toHaveBeenCalledOnce());
        else {
          expect(send).not.toHaveBeenCalled();
          await settleFixtureOutboxWork();
        }
        if (scenario.startsWith('route-')) {
          fixture.navigate('mounted-other');
          await tick();
        } else {
          await owner.beginLogin();
          await realCache.setOwner(null);
          await tick();
        }
        recoveryGate.resolve();
        admissionGate.resolve();
        await waitFor(() => expect(recoveryCompleted).toBe(true));
        await settleFixtureOutboxWork();
        expect(send).toHaveBeenCalledTimes(scenario.endsWith('-after') ? 1 : 0);
        if (send.mock.calls.length) {
          expect(send.mock.calls[0].slice(0, 2)).toEqual([conversation.conversation_id, id]);
          expect(send.mock.calls[0][2]).toMatchObject({ client_message_id: messageId });
        }
        if (scenario.startsWith('route-')) {
          expect((await outbox.get(id))?.client_message_id).toBe(messageId);
          if (scenario.endsWith('-after')) expect((await outbox.get(`${id}-next`))?.status).toBe('failed');
        } else {
          await owner.verified('other-mounted-account@cognis.test');
          expect(await createIndexedDbChatV2Outbox().list()).toEqual([]);
        }
        return;
      }
      await waitFor(() => expect(send).toHaveBeenCalledOnce());
      expect(send.mock.calls[0].slice(0, 2)).toEqual([fixture.conversationId, id]);
      expect(send.mock.calls[0][2]).toMatchObject({ client_message_id: messageId, attachments: [attachment] });
      await waitFor(async () => expect(await outbox.get(id)).toMatchObject({
        status: 'acked', admitted: receipt, client_message_id: messageId, attachments: [attachment],
      }));
    } finally {
      recoveryGate.resolve(); admissionGate.resolve(); otherDetailGate.resolve();
      if (fullAlive) full.unmount();
    }
  });
  afterEach(async () => {
    try {
      try {
        await settleFixtureOutboxWork();
      } finally {
        cleanup();
        await settleFixtureOutboxWork();
      }
    } finally {
      mountedDrafts.clear();
      clearTimelineMemory();
      vi.restoreAllMocks();
      vi.unstubAllGlobals();
    }
  });

  it.each(['before', 'during'])('full-page recovery follows navigation %s verification without reviving the old route', async timing => {
    render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    fixture.changed();
    await screen.findByTestId('timeline-identity-required');
    const nextId = 'mounted-parity-next';
    const nextScope = conversationTimelineScope(nextId);
    vi.mocked(api.conversations.detail).mockImplementation(async id => ({ ...conversation, conversation_id: id }) as never);
    vi.mocked(chatV2Api.snapshot).mockImplementation(async target => ({
      ...snapshot(), scope: typeof target === 'string' ? conversationTimelineScope(target) : target,
      conversation: { ...conversation, conversation_id: typeof target === 'string' ? target : target.conversation_id },
      timeline: { ...snapshot().timeline, items: [{
        ...snapshot().timeline.items[0], content: 'New route canonical row',
      }] },
    }) as ChatSnapshot);
    vi.mocked(chatV2Api.snapshot).mockClear();
    if (timing === 'during') {
      let finish!: () => void;
      const coordination = documentCoordination();
      const original = coordination.synchronize.bind(coordination);
      const verification = vi.spyOn(coordination, 'synchronize').mockImplementationOnce(() => new Promise(resolve => {
        finish = () => { void original().then(resolve); };
      }));
      await fireEvent.click(screen.getByRole('button', { name: 'Verify session and retry' }));
      await waitFor(() => expect(verification).toHaveBeenCalled());
      fixture.navigate(nextId);
      await tick();
      finish();
      await waitFor(() => expect(screen.getByRole('button', { name: 'Verify session and retry' })).toBeEnabled());
      expect(chatV2Api.snapshot).not.toHaveBeenCalled();
      expect(screen.getByTestId('timeline-identity-required')).toBeInTheDocument();
    } else {
      fixture.navigate(nextId);
      await tick();
    }
    await fireEvent.click(screen.getByRole('button', { name: 'Verify session and retry' }));
    await screen.findByText('New route canonical row');
    expect(chatV2Api.snapshot).toHaveBeenCalledWith(nextScope, expect.anything());
    expect(vi.mocked(chatV2Api.snapshot).mock.calls.every(([target]) => (
      typeof target === 'string' ? target : target.conversation_id
    ) === nextId)).toBe(true);
    expect(screen.queryByText('Canonical mounted parity row')).toBeNull();
  });

  it('full cached chat -> embedded cold mount -> full again uses the identical timeline store', async () => {
    const full = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    await waitFor(() => expect(fixture.rows.has(`conversation-view:${fixture.conversationId}`)).toBe(true));
    const canonical = peekTimeline(scope);
    const canonicalView = acquireTimeline(scope);
    const store = canonicalView.controller.store;
    canonicalView.release();
    expect(canonical?.cursor).toBe('mounted:initial');
    full.unmount();

    const embedded = render(ScopedChatV2Timeline, { scope, compact: true });
    await screen.findByText('Canonical mounted parity row');
    const embeddedOwner = acquireTimeline(scope);
    expect(embeddedOwner.controller.store).toBe(store);
    embeddedOwner.release();
    await waitFor(() => expect(chatV2Api.sync).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(embeddedOwner.controller.phase).toBe('ready'));
    embedded.unmount();

    const fullAgain = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    await waitFor(() => expect(chatV2Api.sync).toHaveBeenCalled());
    await waitFor(() => expect(chatV2Api.sync).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(canonicalView.controller.phase).toBe('ready'));
    expect(chatV2Api.snapshot).toHaveBeenCalledTimes(1);
    expect(chatV2Api.timeline).not.toHaveBeenCalled();
    expect(timelineResourceCounts()).toEqual({ entries: 1, retained: 1 });
    expect(fixture.snapshotProjections).toBe(1);
    expect(fixture.stateRestores).toBe(0);
    expect([...fixture.rows.keys()].filter(key => key.startsWith('timeline:'))).toHaveLength(1);
    console.info('timeline-resource-evidence', JSON.stringify({
      snapshots: vi.mocked(chatV2Api.snapshot).mock.calls.length,
      syncs: vi.mocked(chatV2Api.sync).mock.calls.length,
      snapshotProjections: fixture.snapshotProjections,
      stateRestores: fixture.stateRestores,
      storeConstructions: fixture.stores,
      cache: timelineResourceCounts(),
      deviceTimelineCopies: [...fixture.rows.keys()].filter(key => key.startsWith('timeline:')).length,
    }));
    fullAgain.unmount();
  });

  it('boots full/scoped consumers after the layout binds its initial null owner to a verified namespace', async () => {
    // offline/session gates child mounting on deviceDataReady. A first bind
    // clears resident memory before either consumer captures the new owner.
    const owner = fixture.owner;
    fixture.owner = null;
    fixture.changed();
    fixture.owner = owner;
    fixture.changed();
    const full = render(FullChat);
    const scoped = render(ScopedChatV2Timeline, { scope, compact: true });
    await waitFor(() => expect(screen.getAllByText('Canonical mounted parity row')).toHaveLength(2));
    expect(screen.queryByTestId('timeline-identity-required')).toBeNull();
    expect(chatV2Api.snapshot).toHaveBeenCalledOnce();
    expect(timelineResourceCounts()).toEqual({ entries: 1, retained: 1 });
    full.unmount();
    scoped.unmount();
  });

  it('coalesces two mounted windows and preserves the remaining consumer when one unmounts', async () => {
    let resolve!: (snapshot: ChatSnapshot) => void;
    vi.mocked(chatV2Api.snapshot).mockReturnValue(new Promise(done => { resolve = done; }));
    const first = render(ScopedChatV2Timeline, { scope, compact: true });
    await waitFor(() => expect(chatV2Api.snapshot).toHaveBeenCalledTimes(1));
    const second = render(ScopedChatV2Timeline, { scope, compact: true });
    await waitFor(() => expect(second.container.textContent).toContain('Loading timeline…'));
    first.unmount();
    resolve(snapshot());
    expect(await screen.findByText('Canonical mounted parity row')).toBeInTheDocument();
    expect(chatV2Api.snapshot).toHaveBeenCalledTimes(1);
    expect(fixture.snapshotProjections).toBe(1);
    expect(timelineResourceCounts()).toEqual({ entries: 1, retained: 1 });
    second.unmount();
  });

  it('settles delayed real outbox work after unmount before rotating the fixture account', async () => {
    const read = IndexedDbChatV2Outbox.prototype.list;
    let release!: () => void;
    const pending = new Promise<void>(done => { release = done; });
    const backend = vi.spyOn(IndexedDbChatV2Outbox.prototype, 'list').mockImplementation(async function (this: IndexedDbChatV2Outbox, id?: string) {
      await pending;
      return read.call(this, id);
    });
    const view = render(FullChat);
    await screen.findByText('Canonical mounted parity row');
    await waitFor(() => expect(backend).toHaveBeenCalled());
    view.unmount();
    let settled = false;
    const closing = settleFixtureOutboxWork().then(() => { settled = true; });
    await tick();
    expect(settled).toBe(false);
    expect(pendingOutboxWork.size).toBeGreaterThan(0);
    release();
    await closing;
    expect(settled).toBe(true);
    expect(pendingOutboxWork.size).toBe(0);
    // This is the same generation change the next beforeEach performs. All
    // real IDB work and chained drains must finish before revoking their owner.
    await documentCoordination().beginLogin();
    expect(outboxFailures).toEqual([]);
  });

  it.each(['cache', 'legacy', 'network', 'body', 'ready'])(
    'settles simultaneous full/scoped views and fences late work when owner changes during %s',
    async phase => {
      const read = offlineCache.get.bind(offlineCache);
      let finishCache!: (value: unknown) => void;
      const cache = new Promise<unknown>(resolve => { finishCache = resolve; });
      const get = vi.spyOn(offlineCache, 'get').mockImplementation((kind, ...args) => {
        if ((phase === 'cache' && kind === 'timeline')
          || (phase === 'legacy' && (kind === 'scoped-view' || kind === 'conversation-view'))) {
          return cache as never;
        }
        return read(kind, ...args);
      });
      let finishSnapshot!: (value: ChatSnapshot) => void;
      const pendingSnapshot = new Promise<ChatSnapshot>(resolve => { finishSnapshot = resolve; });
      const cancelBody = vi.fn();
      let bodyReader: ReturnType<typeof vi.spyOn> | undefined;
      if (phase === 'network') vi.mocked(chatV2Api.snapshot).mockReturnValue(pendingSnapshot);
      if (phase === 'body') {
        const response = new Response(new ReadableStream({ cancel: cancelBody }), { headers: { 'Content-Type': 'application/json' } });
        bodyReader = vi.spyOn(response.body!, 'getReader');
        const client = new ChatV2ApiClient({ fetch: vi.fn().mockResolvedValue(
          response,
        ) });
        vi.mocked(chatV2Api.snapshot).mockImplementation(client.snapshot.bind(client));
      }
      const acquire = vi.spyOn(wsClient, 'acquireChatV2');
      const release = vi.spyOn(wsClient, 'releaseChatV2');
      const full = render(FullChat);
      const scoped = render(ScopedChatV2Timeline, { scope, compact: true });
      if (phase === 'cache' || phase === 'legacy') {
        await waitFor(() => expect(get.mock.calls.some(([kind]) => kind === (phase === 'cache' ? 'timeline' : 'scoped-view'))).toBe(true));
      } else {
        await waitFor(() => expect(chatV2Api.snapshot).toHaveBeenCalledOnce());
        if (phase === 'body') await waitFor(() => expect(bodyReader).toHaveBeenCalledOnce());
        if (phase === 'ready') {
          await waitFor(() => expect(screen.getAllByText('Canonical mounted parity row')).toHaveLength(2));
          await waitFor(() => expect(acquire).toHaveBeenCalledTimes(2));
          await settleFixtureOutboxWork();
        }
      }
      const requests = vi.mocked(chatV2Api.snapshot).mock.calls.length;
      fixture.owner = `${fixture.owner}:changed`;
      fixture.changed();
      await waitFor(() => expect(screen.getAllByText(/Your account or server context changed/)).toHaveLength(2));
      expect(screen.queryByText('Loading timeline…')).toBeNull();
      expect(screen.queryByText('Canonical mounted parity row')).toBeNull();
      expect(timelineResourceCounts()).toEqual({ entries: 0, retained: 0 });
      if (phase === 'body') await waitFor(() => expect(cancelBody).toHaveBeenCalledOnce());
      if (phase === 'ready') expect(release).toHaveBeenCalledTimes(2);
      finishCache({ state: { cursor: 'late-owner' } });
      finishSnapshot(snapshot());
      await tick();
      expect(chatV2Api.snapshot).toHaveBeenCalledTimes(requests);
      expect(peekTimeline(scope)).toBeNull();
      full.unmount();
      scoped.unmount();
    },
  );

  it.each(['timeline', 'conversation-view'] as const)(
    'fails both foreground surfaces closed on a real account lease transition during %s hydration',
    async kind => {
      const { offlineCache: realCache } = await vi.importActual<typeof import('$lib/offline/cache')>('$lib/offline/cache');
      fixture.realCache = true;
      const coordination = documentCoordination();
      const lease = coordination.capture();
      await lease.validate();
      await realCache.setOwner(coordination.storageName('offline-cache', lease));
      await lease.validate();
      expect(coordination.capture().signal).toBe(lease.signal);
      const read = realCache.get.bind(realCache);
      let finish!: () => void;
      const held = new Promise<void>(resolve => { finish = resolve; });
      const reads = vi.spyOn(realCache, 'get').mockImplementation(async (entryKind, ...args) => {
        const value = await read(entryKind, ...args);
        if (entryKind === kind) await held;
        return value;
      });
      const full = render(FullChat);
      const scoped = render(ScopedChatV2Timeline, { scope, compact: true });
      await waitFor(() => expect(reads.mock.calls.some(([entryKind]) => entryKind === kind)).toBe(true));
      await settleFixtureOutboxWork();
      const requests = vi.mocked(chatV2Api.snapshot).mock.calls.length;
      await coordination.beginLogin();
      expect(lease.signal.aborted).toBe(true);
      await expect(lease.validate()).rejects.toThrow();
      await coordination.verified('replacement-mounted-fixture@cognis.test');
      const replacement = coordination.capture();
      await replacement.validate();
      await realCache.setOwner(coordination.storageName('offline-cache', replacement));
      await replacement.validate();
      expect(replacement.signal).not.toBe(lease.signal);
      await waitFor(() => expect(screen.getAllByText(/Your account or server context changed/)).toHaveLength(2));
      expect(screen.queryByRole('textbox', { name: 'Chat message' })).toBeNull();
      expect(screen.queryByText('Canonical mounted parity row')).toBeNull();
      finish();
      await tick();
      expect(chatV2Api.snapshot).toHaveBeenCalledTimes(requests);
      expect(timelineResourceCounts()).toEqual({ entries: 0, retained: 0 });
      full.unmount();
      scoped.unmount();
    },
  );

  it.each(['abort', 'failure', 'obsolete-owner'] as const)(
    'observes %s foreground hydration rejection without invalidating a newer route',
    async outcome => {
      const original = offlineCache.get.bind(offlineCache);
      let reject!: (reason: unknown) => void;
      const held = new Promise<never>((_, fail) => { reject = fail; });
      const reads = vi.spyOn(offlineCache, 'get').mockImplementation((kind, key, ...args) =>
        kind === 'conversation-view' && key === fixture.conversationId ? held : original(kind, key, ...args));
      const full = render(FullChat);
      await waitFor(() => expect(reads.mock.calls.some(([kind]) => kind === 'conversation-view')).toBe(true));
      if (outcome === 'obsolete-owner') {
        fixture.navigate('newer-owner-route');
        await waitFor(() => expect(chatV2Api.snapshot).toHaveBeenCalledWith(
          expect.objectContaining({ conversation_id: 'newer-owner-route' }), expect.anything()));
      }
      reject(outcome === 'abort' ? new DOMException('Navigation superseded', 'AbortError')
        : outcome === 'obsolete-owner' ? new TimelineOwnershipError() : new Error('Foreground cache unavailable'));
      if (outcome === 'failure') await screen.findByText('Foreground cache unavailable');
      else await tick();
      expect(screen.queryByTestId('timeline-identity-required')).toBeNull();
      full.unmount();
    },
  );

  it('explicitly reboots a verified same-account context once without rebinding the other mounted surface', async () => {
    const full = render(FullChat);
    const scoped = render(ScopedChatV2Timeline, { scope, compact: true });
    await waitFor(() => expect(screen.getAllByText('Canonical mounted parity row')).toHaveLength(2));
    await settleFixtureOutboxWork();
    await documentCoordination().beginLogin();
    await waitFor(() => expect(screen.getAllByText(/Your account or server context changed/)).toHaveLength(2));
    await documentCoordination().verified('mounted-fixture@cognis.test');
    fixture.rows.clear(); // A fresh canonical base is required after invalidation.
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getAllByText('Canonical mounted parity row')).toHaveLength(1));
    expect(screen.getByTestId('timeline-identity-required')).toBeInTheDocument();
    expect(chatV2Api.snapshot).toHaveBeenCalledTimes(2);
    await fireEvent.click(screen.getByRole('button', { name: 'Verify session and retry' }));
    await waitFor(() => expect(screen.getAllByText('Canonical mounted parity row')).toHaveLength(2));
    expect(chatV2Api.snapshot).toHaveBeenCalledTimes(2);
    expect(timelineResourceCounts()).toEqual({ entries: 1, retained: 1 });
    full.unmount();
    scoped.unmount();
  });

  it('does not rebind a surface unmounted during bounded owner verification', async () => {
    const scoped = render(ScopedChatV2Timeline, { scope, compact: true });
    await screen.findByText('Canonical mounted parity row');
    await documentCoordination().beginLogin();
    await documentCoordination().verified('mounted-fixture@cognis.test');
    await screen.findByText(/Your account or server context changed/);
    const lease = documentCoordination().capture();
    let grant!: () => void;
    const validate = vi.fn(() => new Promise<void>(resolve => { grant = resolve; }));
    vi.spyOn(documentCoordination(), 'capture').mockReturnValue({ ...lease, validate });
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(validate).toHaveBeenCalledOnce());
    scoped.unmount();
    grant();
    await tick();
    await tick();
    expect(chatV2Api.snapshot).toHaveBeenCalledOnce();
    expect(timelineResourceCounts()).toEqual({ entries: 0, retained: 0 });
  });

  it('does not rebind either mounted surface across accounts even after a verified account switch', async () => {
    const full = render(FullChat);
    const scoped = render(ScopedChatV2Timeline, { scope, compact: true });
    await waitFor(() => expect(screen.getAllByText('Canonical mounted parity row')).toHaveLength(2));
    await settleFixtureOutboxWork();
    await documentCoordination().beginLogin();
    await documentCoordination().verified('other-mounted-fixture@cognis.test');
    fixture.owner = documentCoordination().storageName('offline-cache');
    fixture.changed();
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Verify session and retry' }));
    await waitFor(() => expect(screen.getAllByText(/Reopen this view after verifying your session/)).toHaveLength(2));
    expect(chatV2Api.snapshot).toHaveBeenCalledOnce();
    expect(chatV2Api.sync).not.toHaveBeenCalled();
    expect(screen.queryByText('Canonical mounted parity row')).toBeNull();
    expect(timelineResourceCounts()).toEqual({ entries: 0, retained: 0 });
    full.unmount();
    scoped.unmount();
  });
});
