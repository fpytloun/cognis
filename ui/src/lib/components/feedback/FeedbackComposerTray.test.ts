import 'fake-indexeddb/auto';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { tick } from 'svelte';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { documentCoordination } from '$lib/document-coordination';
import { DocumentDrafts } from '$lib/offline/document-drafts';
import { conversationFeedback } from '$lib/feedback/staging';
import { FeedbackDeliveryModel } from '$lib/feedback/delivery';
import { createIndexedDbChatV2Outbox, type OutboxEntry } from '$lib/chat-v2/outbox';
import { conversationComposer } from '$lib/chat-v2/conversation-composer';
import type { FeedbackEntryVersion, FeedbackPreflight, FeedbackStagingSnapshot, IncludedFeedback, FeedbackThread } from '$lib/feedback/types';
import FeedbackComposerTray from './FeedbackComposerTray.svelte';
import ChatV2Composer from '../chat-v2/ChatV2Composer.svelte';

const calls = vi.hoisted(() => ({ staging: vi.fn(), stage: vi.fn(), threads: vi.fn(), preflight: vi.fn(), load: vi.fn() }));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: { staging: calls.staging, stage: calls.stage, threads: calls.threads, preflight: calls.preflight } }));
vi.mock('./composer-presentation-loader', () => ({ loadComposerPresentation: calls.load }));
beforeAll(async () => { await import('./composer-presentation'); });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fixture() {
  const conversationId = crypto.randomUUID();
  const subject = crypto.randomUUID();
  const entry: FeedbackEntryVersion = {
    entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), thread_id: crypto.randomUUID(),
    version: 3, body: 'Preserve the selected version.', audience: 'private',
    author: { kind: 'verified_user', label: 'Reviewer', is_self: true },
    reply_to_entry_id: null, created_at: '2026-10-27T00:00:00Z', delivery: [],
  };
  const snapshot: FeedbackStagingSnapshot = { conversation_id: conversationId, version: 1, receipts: [],
    items: [{ entry_id: entry.entry_id, entry_version_id: entry.entry_version_id,
      staging_version: 2, inclusion: 'included', subject_id: subject, anchor_id: crypto.randomUUID() }] };
  const packet: FeedbackPreflight = {
    preflight_id: crypto.randomUUID(), manifest_hash: 'a'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
    selection: [{ entry_version_id: entry.entry_version_id, expected_entry_version: entry.version, staging_version: 2 }],
    destination: { kind: 'origin_chat', conversation_id: conversationId }, update_mode: 'revise_source',
    context_items: [{ role: 'instruction', entry, anchor: { schema_version: 1,
      anchor_id: snapshot.items[0].anchor_id, content_digest: 'b'.repeat(64),
      source: { kind: 'document', content_ref: `art_${crypto.randomUUID().replaceAll('-', '')}` }, locator: { kind: 'whole' } } }],
    source_summaries: [], warnings: [], task_consequences: null,
  };
  const included: IncludedFeedback = { creation_key: crypto.randomUUID(),
    preflight_id: packet.preflight_id, manifest_hash: packet.manifest_hash };
  const thread: FeedbackThread = { thread_id: entry.thread_id, subject_id: subject, version: 1, state: 'open', hidden: false, audience: 'private',
    anchor: packet.context_items[0].anchor, entries: [entry] };
  return { conversationId, entry, snapshot, packet, included, thread };
}
async function restore(f: ReturnType<typeof fixture>) {
  await new DocumentDrafts().write(`feedback-review:${f.included.creation_key}`, JSON.stringify(f.packet));
}
const empty = (conversation_id: string): FeedbackStagingSnapshot => ({ conversation_id, version: 0, items: [], receipts: [] });

beforeEach(async () => {
  vi.resetAllMocks();
  localStorage.clear();
  await loginFixtureAccount();
  calls.staging.mockImplementation(async (id: string) => empty(id));
  calls.load.mockImplementation(() => import('./composer-presentation'));
});
afterEach(async () => { cleanup(); await auth.clear(); vi.restoreAllMocks(); });

it('sends an ordinary message without loading presentation or including staged-only feedback', async () => {
  const f = fixture(), sent = vi.fn();
  calls.staging.mockResolvedValue({ ...f.snapshot, items: [{ ...f.snapshot.items[0], inclusion: 'prepared' }] });
  render(ChatV2Composer, { conversationId: f.conversationId, feedback: null, value: 'Keep my instruction.',
    showMic: false, onSend: sent, onStop: vi.fn(), onFiles: vi.fn() });
  await screen.findByRole('button', { name: 'Add 1 comment to message' });
  expect(screen.queryByText(/attached to your next message/)).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  expect(sent).toHaveBeenCalledOnce();
  expect(calls.load).not.toHaveBeenCalled();
});

it('includes every ready comment, guest comments too, with one click', async () => {
  const f = fixture(), composer = conversationComposer();
  const guest: FeedbackEntryVersion = { ...f.entry, entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(),
    version: 1, body: 'Guest suggestion from a shared link.', author: { kind: 'guest_unverified', label: 'Guest', is_self: false } };
  let rows = [f.entry, guest].map(entry => ({ entry_id: entry.entry_id, entry_version_id: entry.entry_version_id,
    staging_version: 1, inclusion: 'prepared' as 'prepared' | 'included', subject_id: f.thread.subject_id, anchor_id: f.thread.anchor.anchor_id }));
  calls.staging.mockImplementation(async () => ({ ...f.snapshot, items: rows.map(row => ({ ...row })) }));
  calls.stage.mockImplementation(async (_conversation: string, entryId: string) => {
    rows = rows.map(row => row.entry_id === entryId ? { ...row, staging_version: 2, inclusion: 'included' } : row);
    return rows.find(row => row.entry_id === entryId);
  });
  calls.threads.mockResolvedValue({ items: [{ ...f.thread, entries: [f.entry, guest] }], next_cursor: null });
  calls.preflight.mockImplementation(async request => ({ ...f.packet, selection: request.selection }));
  render(FeedbackComposerTray, { conversationId: f.conversationId, included: null });
  await fireEvent.click(await screen.findByRole('button', { name: 'Add 2 comments to message' }));
  await waitFor(() => expect(composer.hydrate(f.conversationId).input.feedback).toMatchObject({ preflight_id: f.packet.preflight_id }));
  expect(calls.preflight).toHaveBeenCalledOnce();
  expect(calls.preflight.mock.calls[0][0].selection.map((item: { entry_version_id: string }) => item.entry_version_id))
    .toEqual([f.entry.entry_version_id, guest.entry_version_id]);
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  expect(screen.queryByText(/reviewed this guest comment/)).not.toBeInTheDocument();
});

it('is ready with a fresh restored packet and shows its count in the composer row', async () => {
  const f = fixture(), ready = vi.fn();
  await restore(f);
  calls.staging.mockResolvedValue(f.snapshot);
  render(FeedbackComposerTray, { conversationId: f.conversationId, included: f.included, onReadyChange: ready });
  expect(await screen.findByRole('button', { name: 'Comments in message: 1' })).toHaveTextContent('1 comment');
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  expect(calls.preflight).not.toHaveBeenCalled();
  expect(calls.load).not.toHaveBeenCalled();
});

it.each(['missing', 'expired', 'other conversation'] as const)('silently re-reviews a %s packet instead of warning', async problem => {
  const f = fixture(), composer = conversationComposer(), ready = vi.fn();
  if (problem === 'expired') f.packet.expires_at = '2020-01-01T00:00:00Z';
  if (problem === 'other conversation') f.packet.destination = { kind: 'origin_chat', conversation_id: crypto.randomUUID() };
  if (problem !== 'missing') await restore(f);
  composer.update(f.conversationId, { ...composer.hydrate(f.conversationId).input, feedback: f.included });
  let row = { ...f.snapshot.items[0] };
  calls.staging.mockImplementation(async () => ({ ...f.snapshot, items: [{ ...row }] }));
  calls.stage.mockImplementation(async (_id: string, _entry: string, body: { inclusion: 'prepared' | 'included' }) => {
    row = { ...row, staging_version: row.staging_version + 1, inclusion: body.inclusion };
    return { ...row };
  });
  calls.threads.mockResolvedValue({ items: [f.thread], next_cursor: null });
  const fresh = { ...f.packet, preflight_id: crypto.randomUUID(), expires_at: '2099-01-01T00:00:00Z',
    destination: { kind: 'origin_chat' as const, conversation_id: f.conversationId } };
  calls.preflight.mockImplementation(async request => ({ ...fresh, selection: request.selection }));
  const view = render(FeedbackComposerTray, { conversationId: f.conversationId, included: f.included, onReadyChange: ready });
  await waitFor(() => expect(composer.hydrate(f.conversationId).input.feedback?.preflight_id).toBe(fresh.preflight_id));
  await view.rerender({ conversationId: f.conversationId, included: composer.hydrate(f.conversationId).input.feedback, onReadyChange: ready });
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  expect(calls.preflight).toHaveBeenCalledOnce();
  expect(screen.queryByText(/Review again|changed or expired|could not be verified/)).toBeNull();
  expect(screen.getByRole('button', { name: 'Comments in message: 1' })).toBeVisible();
});

it('keeps the stale binding and blocks sending when the silent re-review fails', async () => {
  const f = fixture(), composer = conversationComposer(), ready = vi.fn();
  f.packet.expires_at = '2020-01-01T00:00:00Z';
  await restore(f);
  composer.update(f.conversationId, { ...composer.hydrate(f.conversationId).input, feedback: f.included });
  let row = { ...f.snapshot.items[0] };
  calls.staging.mockImplementation(async () => ({ ...f.snapshot, items: [{ ...row }] }));
  calls.stage.mockImplementation(async (_id: string, _entry: string, body: { inclusion: 'prepared' | 'included' }) => {
    row = { ...row, staging_version: row.staging_version + 1, inclusion: body.inclusion };
    return { ...row };
  });
  calls.threads.mockResolvedValue({ items: [f.thread], next_cursor: null });
  // The first re-review clears the row back to ready, then fails while including it again.
  let failInclude = true;
  const stage = calls.stage.getMockImplementation()!;
  calls.stage.mockImplementation(async (id: string, entry: string, body: { inclusion: 'prepared' | 'included' }) => {
    if (body.inclusion === 'included' && failInclude) throw new Error('Staging unavailable');
    return stage(id, entry, body);
  });
  const fresh = { ...f.packet, preflight_id: crypto.randomUUID(), expires_at: '2099-01-01T00:00:00Z' };
  calls.preflight.mockImplementation(async request => ({ ...fresh, selection: request.selection }));
  const view = render(FeedbackComposerTray, { conversationId: f.conversationId, included: f.included, onReadyChange: ready });
  const retry = await screen.findByRole('button', { name: 'Comments failed · Retry' });
  // The message still carries its comments and cannot be sent without them.
  expect(row.inclusion).toBe('prepared');
  expect(composer.hydrate(f.conversationId).input.feedback).toEqual(f.included);
  expect(ready).toHaveBeenLastCalledWith(false);
  // Retry restores the same comment even though staging no longer marks it included.
  failInclude = false;
  await fireEvent.click(retry);
  await waitFor(() => expect(composer.hydrate(f.conversationId).input.feedback?.preflight_id).toBe(fresh.preflight_id));
  expect(row.inclusion).toBe('included');
  expect(calls.preflight.mock.lastCall?.[0].selection.map((item: { entry_version_id: string }) => item.entry_version_id))
    .toEqual([f.entry.entry_version_id]);
  await view.rerender({ conversationId: f.conversationId, included: composer.hydrate(f.conversationId).input.feedback, onReadyChange: ready });
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
});

it('re-reviews an included comment after it was edited, keeping it in the message', async () => {
  const f = fixture(), composer = conversationComposer(), ready = vi.fn();
  // Include the comment through the normal path, so its reviewed entry is recorded.
  let row = { ...f.snapshot.items[0], inclusion: 'prepared' as 'prepared' | 'included' };
  calls.staging.mockImplementation(async () => ({ ...f.snapshot, items: [{ ...row }] }));
  calls.stage.mockImplementation(async (_id: string, _entry: string, body: { entry_version_id: string; inclusion: 'prepared' | 'included' }) => {
    row = { ...row, staging_version: row.staging_version + 1, entry_version_id: body.entry_version_id, inclusion: body.inclusion };
    return { ...row };
  });
  calls.threads.mockResolvedValue({ items: [f.thread], next_cursor: null });
  calls.preflight.mockImplementation(async request => ({ ...f.packet, preflight_id: crypto.randomUUID(), selection: request.selection }));
  await conversationFeedback(f.conversationId).addAll();
  const first = composer.hydrate(f.conversationId).input.feedback!;
  // The comment is edited: staging and the thread now carry only the new version.
  const edited = { ...f.entry, entry_version_id: crypto.randomUUID(), version: 2, body: 'Edited instruction.' };
  row = { ...row, entry_version_id: edited.entry_version_id, staging_version: row.staging_version + 1 };
  calls.threads.mockResolvedValue({ items: [{ ...f.thread, entries: [edited] }], next_cursor: null });
  render(FeedbackComposerTray, { conversationId: f.conversationId, included: first, onReadyChange: ready });
  await waitFor(() => expect(composer.hydrate(f.conversationId).input.feedback?.creation_key).not.toBe(first.creation_key));
  expect(calls.preflight.mock.lastCall?.[0].selection.map((item: { entry_version_id: string }) => item.entry_version_id))
    .toEqual([edited.entry_version_id]);
  expect(screen.queryByRole('button', { name: 'Comments failed · Retry' })).toBeNull();
});

it('keeps included readiness closed while a refresh fails', async () => {
  const f = fixture(), pending = deferred<FeedbackStagingSnapshot>(), ready = vi.fn();
  await restore(f);
  calls.staging.mockResolvedValueOnce(f.snapshot).mockReturnValueOnce(pending.promise);
  render(FeedbackComposerTray, { conversationId: f.conversationId, included: f.included, onReadyChange: ready });
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(true));
  window.dispatchEvent(new Event('feedback-changed'));
  await waitFor(() => expect(ready).toHaveBeenLastCalledWith(false));
  pending.reject(new Error('Staging unavailable'));
  expect(await screen.findByRole('button', { name: 'Comments failed · Retry' })).toHaveAttribute('title', 'Staging unavailable');
  expect(ready).toHaveBeenLastCalledWith(false);
});

it('opens and closes the comment list from the chip without a Comments panel', async () => {
  const f = fixture();
  await restore(f);
  calls.staging.mockResolvedValue(f.snapshot);
  calls.threads.mockResolvedValue({ items: [f.thread], next_cursor: null });
  render(FeedbackComposerTray, { conversationId: f.conversationId, included: f.included });
  const chip = await screen.findByRole('button', { name: 'Comments in message: 1' });
  await fireEvent.click(chip);
  expect(await screen.findByRole('checkbox', { name: /Preserve the selected version/ })).toBeChecked();
  expect(chip).toHaveAttribute('aria-expanded', 'true');
  await fireEvent.click(chip);
  await waitFor(() => expect(screen.queryByRole('region', { name: 'Comments in this message' })).toBeNull());
  expect(chip).toHaveAttribute('aria-expanded', 'false');
});

it('discovers retained feedback before any presentation loads and passes the one live model through', async () => {
  const f = fixture(), chunk = deferred<typeof import('./composer-presentation')>();
  const retained: OutboxEntry = { client_txn_id: crypto.randomUUID(), client_message_id: crypto.randomUUID(),
    conversation_id: f.conversationId, content: 'Retain this text.', attachments: [], chat_mode: 'build',
    feedback: f.included, status: 'failed', created_at: '2026-10-27T00:00:00Z', updated_at: '2026-10-27T00:00:00Z' };
  await createIndexedDbChatV2Outbox().put(retained);
  const stored = await createIndexedDbChatV2Outbox().get(retained.client_txn_id);
  const start = vi.spyOn(FeedbackDeliveryModel.prototype, 'start');
  calls.load.mockReturnValue(chunk.promise);
  render(FeedbackComposerTray, { conversationId: f.conversationId, included: null });
  await waitFor(() => expect(calls.load).toHaveBeenCalledOnce());
  expect(start).toHaveBeenCalledOnce();
  const model = start.mock.contexts[0] as FeedbackDeliveryModel;
  expect(get(model.state).pending).toEqual([stored]);
  expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument();
  chunk.resolve(await import('./composer-presentation'));
  await screen.findByRole('button', { name: 'Check delivery' });
  expect(start).toHaveBeenCalledOnce();
  expect(await createIndexedDbChatV2Outbox().get(retained.client_txn_id)).toEqual(stored);
});

it.each(['conversation', 'account'] as const)('ignores late presentation after %s invalidation', async transition => {
  const f = fixture(), chunk = deferred<typeof import('./composer-presentation')>(), ready = vi.fn();
  await restore(f);
  calls.staging.mockResolvedValue(f.snapshot);
  calls.load.mockReturnValueOnce(chunk.promise).mockImplementation(() => new Promise(() => {}));
  const view = render(FeedbackComposerTray, { conversationId: f.conversationId, included: f.included, onReadyChange: ready });
  await fireEvent.click(await screen.findByRole('button', { name: 'Comments in message: 1' }));
  await waitFor(() => expect(calls.load).toHaveBeenCalledOnce());
  if (transition === 'conversation') await view.rerender({ conversationId: crypto.randomUUID(), included: null, onReadyChange: ready });
  else await documentCoordination().invalidate();
  ready.mockClear();
  chunk.resolve(await import('./composer-presentation'));
  await chunk.promise;
  await tick();
  expect(screen.queryByText(/Preserve the selected version.*v3/)).not.toBeInTheDocument();
  if (transition === 'account') expect(ready).not.toHaveBeenCalledWith(true);
});

it.each(['conversation', 'account'] as const)('disposes old owners once and ignores prepared-page completion after %s change', async transition => {
  const f = fixture(), page = deferred<{ items: { entries: FeedbackEntryVersion[] }[]; next_cursor: null }>();
  calls.staging.mockResolvedValue(f.snapshot);
  calls.threads.mockReturnValue(page.promise);
  const channel = conversationFeedback(f.conversationId);
  const original = channel.snapshot.subscribe, stops: ReturnType<typeof vi.fn>[] = [];
  vi.spyOn(channel.snapshot, 'subscribe').mockImplementation((...args) => {
    const stop = vi.fn(original(...args));
    stops.push(stop);
    return stop;
  });
  const dispose = vi.spyOn(FeedbackDeliveryModel.prototype, 'dispose');
  const start = vi.spyOn(FeedbackDeliveryModel.prototype, 'start');
  const view = render(FeedbackComposerTray, { conversationId: f.conversationId, included: null });
  await fireEvent.click(await screen.findByRole('button', { name: 'Choose comments' }));
  await waitFor(() => expect(calls.threads).toHaveBeenCalledOnce());
  if (transition === 'account') await documentCoordination().invalidate();
  else await view.rerender({ conversationId: crypto.randomUUID(), included: null });
  view.unmount();
  page.resolve({ items: [{ entries: [f.entry] }], next_cursor: null });
  await page.promise;
  await tick();
  expect(stops.length).toBeGreaterThan(0);
  for (const stop of stops) expect(stop).toHaveBeenCalledOnce();
  expect(dispose.mock.contexts).toHaveLength(start.mock.contexts.length);
  for (const model of start.mock.contexts) expect(dispose.mock.contexts.filter(context => context === model)).toHaveLength(1);
  expect(screen.queryByText(f.entry.body)).not.toBeInTheDocument();
});

it('removes every included comment with ×, even ones the reviewed packet does not list', async () => {
  const f = fixture(), composer = conversationComposer();
  await restore(f);
  const attachments = [{ artifact_id: `art_${crypto.randomUUID().replaceAll('-', '')}`,
    filename: 'review.md', mime_type: 'text/markdown', kind: 'file' as const, size_bytes: 40 }];
  composer.update(f.conversationId, { ...composer.hydrate(f.conversationId).input,
    text: 'Preserve the retained instruction.', attachments, feedback: f.included });
  const extra = { ...f.snapshot.items[0], entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), staging_version: 5 };
  let rows = [{ ...f.snapshot.items[0] }, extra];
  calls.staging.mockImplementation(async () => ({ ...f.snapshot, items: rows.map(row => ({ ...row })) }));
  calls.stage.mockImplementation(async (_id: string, entryId: string) => {
    rows = rows.map(row => row.entry_id === entryId ? { ...row, staging_version: row.staging_version + 1, inclusion: 'prepared' as const } : row);
    return rows.find(row => row.entry_id === entryId);
  });
  // The chip's × calls this; an outdated packet must not leave the extra row included.
  await conversationFeedback(f.conversationId).exclude();
  expect(composer.hydrate(f.conversationId).input).toMatchObject({
    text: 'Preserve the retained instruction.', attachments, feedback: null,
  });
  expect(calls.stage).toHaveBeenCalledWith(f.conversationId, f.entry.entry_id, {
    expected_version: 2, entry_version_id: f.entry.entry_version_id, inclusion: 'prepared' });
  expect(calls.stage).toHaveBeenCalledWith(f.conversationId, extra.entry_id, {
    expected_version: 5, entry_version_id: extra.entry_version_id, inclusion: 'prepared' });
  expect(rows.every(row => row.inclusion === 'prepared')).toBe(true);
});

it('does not publish a stale staging response or clear a newer refresh error', async () => {
  const f = fixture(), older = deferred<FeedbackStagingSnapshot>(), newer = deferred<FeedbackStagingSnapshot>();
  calls.staging.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
  const channel = conversationFeedback(f.conversationId);
  const first = channel.refresh(), second = channel.refresh();
  expect(get(channel.loading)).toBe(true);
  newer.reject(new Error('Current staging unavailable'));
  await second;
  expect(get(channel.loading)).toBe(false);
  expect(get(channel.error)).toBe('Current staging unavailable');
  older.resolve(f.snapshot);
  await first;
  expect(get(channel.snapshot)).toBeNull();
  expect(get(channel.error)).toBe('Current staging unavailable');
});

it('cannot continue excluding versions after account invalidation during staging lookup', async () => {
  const f = fixture(), pending = deferred<FeedbackStagingSnapshot>(), composer = conversationComposer();
  await restore(f);
  composer.update(f.conversationId, { ...composer.hydrate(f.conversationId).input, feedback: f.included });
  calls.staging.mockReturnValue(pending.promise);
  const exclusion = conversationFeedback(f.conversationId).exclude();
  await documentCoordination().invalidate();
  pending.resolve(f.snapshot);
  await expect(exclusion).rejects.toThrow();
  expect(calls.stage).not.toHaveBeenCalled();
});
