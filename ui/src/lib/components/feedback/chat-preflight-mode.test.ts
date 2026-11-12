import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { FeedbackController } from '$lib/feedback/state';
import type {
  FeedbackEntryVersion, FeedbackPreflightRequest, FeedbackSourceRef, FeedbackStagedItem, FeedbackThread, ResolvedFeedbackSource,
} from '$lib/feedback/types';
import FeedbackPanel from './FeedbackPanel.svelte';
import FeedbackReview from './FeedbackReview.svelte';
import type { FeedbackReviewSource } from '$lib/feedback/review-selection';

const api = vi.hoisted(() => ({ staging: vi.fn(), stage: vi.fn(), preflight: vi.fn() }));
vi.mock('$lib/feedback/api', async importOriginal => {
  const original = await importOriginal<typeof import('$lib/feedback/api')>();
  return { ...original, feedbackApi: { ...original.feedbackApi, ...api } };
});
let controller: FeedbackController | undefined;
beforeEach(async () => {
  vi.resetAllMocks(); localStorage.clear(); await loginFixtureAccount();
});
afterEach(async () => { cleanup(); controller?.dispose(); controller = undefined; await auth.clear(); });

function documentSource(): FeedbackSourceRef {
  return { kind: 'document', content_ref: `art_${crypto.randomUUID().replace(/-/g, '')}` };
}
function transcript(conversation: string): FeedbackSourceRef {
  return { kind: 'transcript', conversation_id: conversation, representation: 'chat-item-as-of-event-v1',
    source_event: { store: 'intaris', session_id: crypto.randomUUID(), seq: 9, event_type: 'assistant_message' },
    source_item_id: crypto.randomUUID(), field_path: ['content'] };
}
function workDiff(): FeedbackSourceRef {
  return { kind: 'work_diff', activity_scope_id: crypto.randomUUID(), source_item_id: crypto.randomUUID(),
    source_event: { store: 'intaris', session_id: crypto.randomUUID(), seq: 12, event_type: 'file_change' },
    file_ordinal: 0, root_id: null, path: 'src/source.ts', old_path: null };
}
function thread(source: FeedbackSourceRef, index: number): FeedbackThread {
  const id = crypto.randomUUID();
  const entry: FeedbackEntryVersion = { entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(),
    version: index + 1, thread_id: id, body: `Exact selected correction ${index + 1}`, audience: 'internal',
    author: { kind: 'verified_user', label: 'Reviewer', is_self: true }, reply_to_entry_id: null,
    created_at: '2026-10-28T00:00:00Z', delivery: [] };
  return { thread_id: id, subject_id: crypto.randomUUID(), version: 1, state: 'open', hidden: false,
    audience: 'internal', entries: [entry], anchor: { schema_version: 1, anchor_id: crypto.randomUUID(),
      source, locator: { kind: 'whole' }, content_digest: 'a'.repeat(64) } };
}

it.each([
  ['native transcript', 'answer_only'],
  ['work diff', 'answer_only'],
  ['mixed document/transcript', 'answer_only'],
  ['multiple documents', 'answer_only'],
  ['one exact document', 'revise_source'],
  ['repeated exact document', 'revise_source'],
  ['same-thread repeated entries', 'revise_source'],
] as const)('previews request-driven %s with exact selected versions', async (variant, previousMode) => {
  const conversation = crypto.randomUUID(), document = documentSource();
  const selectedSources = variant === 'native transcript' ? [transcript(conversation)]
    : variant === 'work diff' ? [workDiff()]
    : variant === 'mixed document/transcript' ? [document, transcript(conversation)]
    : variant === 'multiple documents' ? [document, documentSource()]
    : variant === 'repeated exact document' ? [document, document] : [document];
  const selectedThreads = selectedSources.map(thread);
  if (variant === 'same-thread repeated entries') {
    const original = selectedThreads[0].entries[0];
    selectedThreads[0].entries.push({ ...original, entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(),
      body: 'Exact selected correction 2', version: 2, reply_to_entry_id: original.entry_id });
  }
  // An unrelated source must neither grant nor veto revision authority.
  const unselected = thread(previousMode === 'answer_only' ? document : transcript(conversation), 8);
  const threads = [...selectedThreads, unselected];
  const entries = selectedThreads.flatMap(value => value.entries);
  const context = { kind: 'chat' as const, conversation_id: conversation };
  const source: ResolvedFeedbackSource = { subject_id: crypto.randomUUID(), source_token: crypto.randomUUID(),
    source: document, context, content_digest: 'a'.repeat(64), availability: 'available',
    permitted_locator_kinds: ['whole'], lineage_id: crypto.randomUUID(), revision: 1, latest_content_ref: null,
    capabilities: { read: true, comment: true, moderate: true, dispatch: true, disclose: false },
    title: 'Current document is not selected-source proof', format: 'plain', canonical_text: null };
  const rows: FeedbackStagedItem[] = threads.flatMap(value => value.entries.map(entry => ({ entry_id: entry.entry_id,
    entry_version_id: entry.entry_version_id, staging_version: 3, inclusion: 'prepared',
    subject_id: value.subject_id, anchor_id: value.anchor.anchor_id })));
  api.staging.mockImplementation(async () => ({ conversation_id: conversation, version: 1,
    items: rows.map(value => ({ ...value })), receipts: [] }));
  api.stage.mockImplementation(async (_conversation, entryId, request) => {
    const row = rows.find(value => value.entry_id === entryId)!;
    expect(request).toEqual({ expected_version: 3, entry_version_id: row.entry_version_id, inclusion: 'included' });
    return { ...row, staging_version: 4, inclusion: 'included' };
  });
  api.preflight.mockImplementation(async (request: FeedbackPreflightRequest) => {
    const documents = new Set(selectedSources.filter(value => value.kind === 'document').map(value => value.content_ref));
    // Faithfully reproduce the backend's non-document revision rejection.
    if (request.update_mode === 'revise_source' && (documents.size !== 1 || selectedSources.some(value => value.kind !== 'document'))) {
      throw Object.assign(new Error('Only one exact document can be revised'), { status: 409, code: 'invalid_update_mode' });
    }
    return { preflight_id: crypto.randomUUID(), manifest_hash: 'b'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
      selection: request.selection, context_items: selectedThreads.flatMap(value => value.entries.map(entry => ({ role: 'instruction',
        entry, anchor: value.anchor }))), source_summaries: [], destination: request.destination,
      update_mode: request.update_mode, warnings: [], task_consequences: null };
  });
  controller = new FeedbackController(source.source, context);
  controller.patch({ source, threads });
  render(FeedbackPanel, { controller });
  // The panel lists the comments with the include bar at the bottom; every unsent comment starts selected.
  await fireEvent.click(screen.getByRole('checkbox', { name: `Include in message: ${unselected.entries[0].body}` }));
  await fireEvent.click(screen.getByRole('button', { name: `Include ${entries.length} ${entries.length === 1 ? 'comment' : 'comments'} in message` }));
  await waitFor(() => expect(api.preflight).toHaveBeenCalledOnce());
  expect(api.preflight.mock.calls[0][0]).toMatchObject({
    context, destination: { kind: 'origin_chat', conversation_id: conversation }, update_mode: 'follow_request',
    selection: entries.map(entry => ({ entry_version_id: entry.entry_version_id,
      expected_entry_version: entry.version, staging_version: 4 })),
  });
  expect(api.stage).toHaveBeenCalledTimes(entries.length);
  expect(api.stage.mock.calls.some(([, entryId]) => entryId === unselected.entries[0].entry_id)).toBe(false);
  expect(screen.queryByText('Preview packet')).not.toBeInTheDocument();
});

it('leaves source authority to canonical backend anchors when the review has no local source proof', async () => {
  const conversation = crypto.randomUUID(), selected = thread(documentSource(), 0), entry = selected.entries[0];
  const context = { kind: 'chat' as const, conversation_id: conversation };
  api.staging.mockResolvedValue({ conversation_id: conversation, items: [], receipts: [] });
  api.stage.mockResolvedValue({ entry_id: entry.entry_id, entry_version_id: entry.entry_version_id,
    staging_version: 1, inclusion: 'included' });
  api.preflight.mockImplementation(async (request: FeedbackPreflightRequest) => ({
    preflight_id: crypto.randomUUID(), manifest_hash: 'b'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
    selection: request.selection, context_items: [{ role: 'instruction', entry, anchor: selected.anchor }],
    source_summaries: [], destination: request.destination, update_mode: request.update_mode, warnings: [], task_consequences: null,
  }));
  render(FeedbackReview, { props: { entries: [entry], subjectIds: [selected.subject_id], context, onClose: vi.fn() } });
  await fireEvent.click(screen.getByRole('button', { name: 'Include 1 comment in message' }));
  await waitFor(() => expect(api.preflight).toHaveBeenCalledOnce());
  expect(api.preflight.mock.calls[0][0].update_mode).toBe('follow_request');
  expect(screen.queryByText('Preview packet')).not.toBeInTheDocument();
});

it('discards a pending preview when selected canonical source proof changes without changing entry IDs', async () => {
  const conversation = crypto.randomUUID(), selected = thread(documentSource(), 0), entry = selected.entries[0];
  const context = { kind: 'chat' as const, conversation_id: conversation };
  const proof: FeedbackReviewSource = { entry_version_id: entry.entry_version_id, version: entry.version,
    thread_id: entry.thread_id, anchor_id: selected.anchor.anchor_id, source: selected.anchor.source };
  api.staging.mockResolvedValue({ conversation_id: conversation, items: [], receipts: [] });
  api.stage.mockResolvedValue({ entry_id: entry.entry_id, entry_version_id: entry.entry_version_id,
    staging_version: 1, inclusion: 'included' });
  let finish!: (value: unknown) => void;
  api.preflight.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const props = { entries: [entry], sources: [proof], subjectIds: [selected.subject_id], context, onClose: vi.fn() };
  const view = render(FeedbackReview, { props });
  await fireEvent.click(screen.getByRole('button', { name: 'Include 1 comment in message' }));
  await waitFor(() => expect(api.preflight).toHaveBeenCalledOnce());
  expect(api.preflight.mock.calls[0][0].update_mode).toBe('follow_request');
  await view.rerender({ ...props, sources: [{ ...proof, source: transcript(conversation) }] });
  finish({ preflight_id: crypto.randomUUID(), manifest_hash: 'b'.repeat(64), expires_at: '2099-01-01T00:00:00Z',
    selection: api.preflight.mock.calls[0][0].selection, context_items: [{ role: 'instruction', entry, anchor: selected.anchor }],
    source_summaries: [], destination: { kind: 'origin_chat', conversation_id: conversation },
    update_mode: 'follow_request', warnings: [], task_consequences: null });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Include 1 comment in message' })).not.toBeDisabled());
  expect(props.onClose).not.toHaveBeenCalled();
  expect(screen.queryByText('Packet preview')).not.toBeInTheDocument();
});
