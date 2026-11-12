import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import 'fake-indexeddb/auto';
import { get } from 'svelte/store';
import { documentCoordination } from '$lib/document-coordination';
import { FeedbackController } from './state';
import { CommentWorkspace } from './comment-workspace';
import type { FeedbackEntryVersion, FeedbackThread, ResolvedFeedbackSource } from './types';

const calls = vi.hoisted(() => ({ resolve: vi.fn(), threads: vi.fn(), draft: vi.fn(), create: vi.fn(), reply: vi.fn(), edit: vi.fn(), saveDraft: vi.fn(), text: vi.fn(), owner: vi.fn() }));
vi.mock('./api', () => ({ feedbackApi: { resolve: calls.resolve, threads: calls.threads, draft: calls.draft,
  replyTo: (thread: { thread_id: string }, body: string, context: unknown) => calls.reply(thread.thread_id, { body, context }),
  createThread: calls.create, reply: calls.reply, edit: calls.edit, saveDraft: calls.saveDraft, text: calls.text, owner: calls.owner } }));
const source: ResolvedFeedbackSource = {
  subject_id: 'document-feedback-subject', source_token: 'account-bound-source-proof',
  source: { kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' },
  context: { kind: 'chat', conversation_id: 'originating-conversation' },
  content_digest: 'a'.repeat(64), lineage_id: null, revision: 1, latest_content_ref: null,
  title: 'A document produced elsewhere', format: 'markdown', availability: 'available',
  capabilities: { read: true, comment: true, dispatch: true, moderate: true, disclose: true },
  canonical_text: null, permitted_locator_kinds: ['whole', 'text'],
};
const controllers: FeedbackController[] = [];
function controller(standalone = false): FeedbackController {
  const value = new FeedbackController(source.source, standalone ? { kind: 'standalone' } : source.context, standalone);
  controllers.push(value); return value;
}
beforeEach(async () => {
  localStorage.clear();
  await documentCoordination().initialize();
  await documentCoordination().beginLogin();
  await documentCoordination().verified('feedback-state@cognis.test');
  calls.resolve.mockReset().mockResolvedValue(source);
  calls.threads.mockReset().mockResolvedValue({ items: [], next_cursor: null });
  calls.draft.mockReset().mockRejectedValue(Object.assign(new Error('Draft not found'), { status: 404 }));
  calls.create.mockReset().mockResolvedValue({});
  calls.reply.mockReset().mockResolvedValue({});
  calls.edit.mockReset().mockResolvedValue({});
  calls.saveDraft.mockReset().mockImplementation(async () => ({ version: 1 }));
  calls.text.mockReset();
  calls.owner.mockReset().mockResolvedValue({ email: 'feedback-state@cognis.test' });
});
afterEach(() => { for (const value of controllers.splice(0)) value.dispose(); });
describe('source feedback lifecycle', () => {
  it('locates through the mounted source adapter, clears prior errors and refuses changed source content', async () => {
    const value = controller(); await value.load();
    const workspace = new CommentWorkspace(), unregister = workspace.registerDocumentLocator(source.source, thread => value.locate(thread));
    const thread: FeedbackThread = { thread_id: crypto.randomUUID(), subject_id: source.subject_id, version: 1,
      state: 'open', hidden: false, audience: 'internal', entries: [],
      anchor: { schema_version: 1, anchor_id: crypto.randomUUID(), source: source.source,
        content_digest: source.content_digest, locator: { kind: 'whole' } } };
    value.binding(get(value.state))!.registerAdapter(crypto.randomUUID(), { clearSelection() {}, locate: () => ({ status: 'exact' }) });
    value.patch({ error: 'Earlier target was unavailable.' });
    expect(await workspace.locate(thread)).toBe(true);
    expect(get(value.state)).toMatchObject({ selected: thread.thread_id, error: '' });
    expect(get(value.presenter.state).surface).toBe('thread');
    value.patch({ selected: null, source: { ...source, content_digest: 'b'.repeat(64) } });
    expect(await workspace.locate(thread)).toBe(false);
    expect(get(value.state).selected).toBeNull();
    expect(get(value.state).error).toContain('Open the original revision');
    unregister(); workspace.dispose();
  });
  it('does not republish unchanged interaction state into reactive renderers', async () => {
    const value = controller(); await value.load();
    const changed = vi.fn(), stop = value.state.subscribe(changed);
    changed.mockClear();
    value.patch({ interacting: false });
    expect(changed).not.toHaveBeenCalled();
    value.patch({ interacting: true });
    expect(changed).toHaveBeenCalledOnce();
    value.patch({ interacting: true });
    expect(changed).toHaveBeenCalledOnce();
    stop();
  });
  it('requires comment mode before accepting a document-viewer selection', async () => {
    const value = controller(); await value.load();
    const binding = value.binding(get(value.state))!;
    binding.onAnchorCandidate({ locator: { kind: 'whole' } });
    expect(get(value.state).candidate).toBeNull();
    value.patch({ mode: 'comment' });
    binding.onAnchorCandidate({ locator: { kind: 'whole' } });
    expect(get(value.state).candidate?.locator.kind).toBe('whole');
  });
  it('allows renderer interaction cleanup after its controller has been disposed', async () => {
    const value = controller(); await value.load();
    const binding = value.binding(get(value.state))!;
    value.dispose();
    expect(() => binding.onInteractionChange(false)).not.toThrow();
  });
  it('withdraws an earlier source capability if explicit source revalidation fails', async () => {
    const value = controller(); await value.load(); value.patch({ mode: 'comment' }); value.setDraft('Retain local work');
    calls.resolve.mockRejectedValueOnce(new Error('Source access expired'));
    await value.load();
    expect(value.binding(get(value.state))).toBeNull();
    expect(get(value.state)).toMatchObject({ source: null, draft: 'Retain local work', error: 'Source access expired' });
    await value.discardDraft();
  });
  it('retains one presentation port and session when the owning store republishes its controller', async () => {
    const value = controller(); await value.load(); value.patch({ mode: 'comment' });
    const port = value.presentation();
    value.presenter.compose(); value.setDraft('Stable presenter draft');
    expect(value.presentation()).toBe(port);
    expect(get(port.session.state).composer).toBe(true);
    await value.discardDraft();
  });
  it('keeps temporary targeting separate from durable acceptance and rail presentation', async () => {
    const value = controller(); await value.load(); value.patch({ mode: 'comment' });
    const binding = value.binding(get(value.state))!;
    binding.onTargetChange?.({ candidate: { locator: { kind: 'whole' } }, label: 'Source', surface_id: 'mounted-source', instance_id: 'mounted-adapter', geometry: () => null });
    expect(get(value.state)).toMatchObject({ candidate: null, open: false, draft: '' });
    binding.onAnchorCandidate({ locator: { kind: 'whole' } });
    value.setDraft('Keep this draft');
    value.presenter.close(); value.presentation().setMode('view');
    expect(get(value.state)).toMatchObject({ candidate: { locator: { kind: 'whole' } }, draft: 'Keep this draft', open: false });
    await value.discardDraft();
    expect(get(value.state)).toMatchObject({ candidate: null, draft: '' });
  });
  it('does not discard or retarget the immutable uncertain save', async () => {
    const value = controller(); await value.load(); value.setDraft('Original');
    calls.create.mockRejectedValueOnce(new TypeError('Response lost')); await value.save();
    await value.discardDraft();
    value.binding(get(value.state))!.onAnchorCandidate({ locator: { kind: 'whole' } });
    expect(get(value.state)).toMatchObject({ draft: 'Original', pending_body: 'Original', candidate: null });
  });
  it.each(['reply', 'edit'] as const)('notifies the composer after successful %s, never a failed mutation', async (operation) => {
    const value = controller();
    await value.load();
    const changed = vi.fn();
    window.addEventListener('feedback-changed', changed);
    const mutate = () => operation === 'reply'
      ? value.reply({ thread_id: 'authored-thread', version: 1, entries: [] } as unknown as FeedbackThread, 'Reply body')
      : value.edit({ entry_id: 'authored-entry', version: 1 } as FeedbackEntryVersion, 'Edited body');
    try {
      await mutate();
      expect(changed).toHaveBeenCalledOnce();
      calls[operation].mockRejectedValueOnce(new Error('Mutation rejected'));
      await expect(mutate()).rejects.toThrow('Mutation rejected');
      expect(changed).toHaveBeenCalledOnce();
    } finally { window.removeEventListener('feedback-changed', changed); }
  });
  it.each([false, true])('passes the lexical edit context, standalone=%s', async (standalone) => {
    const value = controller(standalone);
    await value.load();
    await value.edit({ entry_id: 'authored-entry', version: 1 } as FeedbackEntryVersion, 'Edited body');
    expect(calls.edit).toHaveBeenCalledWith('authored-entry', 1, 'Edited body',
      standalone ? { kind: 'standalone' } : source.context);
  });
  it('continues remote autosave after a transient failed draft operation', async () => {
    calls.saveDraft.mockRejectedValueOnce(new Error('Draft service unavailable'));
    const value = controller();
    await value.load();
    value.setDraft('First local edit');
    await vi.waitFor(() => expect(calls.saveDraft).toHaveBeenCalledTimes(1), { timeout: 2000 });
    value.setDraft('Second local edit');
    await vi.waitFor(() => expect(calls.saveDraft).toHaveBeenCalledTimes(2), { timeout: 2000 });
    expect(calls.saveDraft.mock.calls[1][1]).toMatchObject({ body: 'Second local edit', expected_version: 0 });
    expect(get(value.state).draft).toBe('Second local edit');
  });
  it('binds a comment to its explicit originating conversation, never producer metadata', async () => {
    const value = controller();
    await value.load(); value.setDraft('Change the comparison.');
    await value.save();
    expect(calls.create).toHaveBeenCalledOnce();
    expect(calls.create.mock.calls[0][0]).toMatchObject({ context: source.context, body: 'Change the comparison.',
      anchor: { source_token: source.source_token, content_digest: source.content_digest, locator: { kind: 'whole' } } });
  });
  it('replays the same saved comment after response loss while retaining newer input', async () => {
    const value = controller();
    await value.load(); value.setDraft('Original comment');
    calls.create.mockRejectedValueOnce(new TypeError('Response lost'));
    await value.save();
    const request = structuredClone(calls.create.mock.calls[0][0]);
    value.setDraft('Newer private draft');
    await value.save();
    expect(calls.create.mock.calls[1][0]).toEqual(request);
    expect(get(value.state)).toMatchObject({ draft: 'Newer private draft', pending_body: null, saving: false });
  });
  it('retains the immutable pending operation across reload instead of creating a duplicate comment', async () => {
    const first = controller();
    await first.load(); first.setDraft('Persist this comment');
    calls.create.mockRejectedValueOnce(new TypeError('Response lost'));
    await first.save();
    const request = structuredClone(calls.create.mock.calls[0][0]);
    first.dispose();
    const reloaded = controller();
    await reloaded.load(); await reloaded.save();
    expect(calls.create.mock.calls[1][0]).toEqual(request);
  });
  it('rejects old canonical text callbacks after leaving a source', async () => {
    const value = controller();
    await value.load();
    const binding = value.binding(get(value.state))!;
    value.dispose();
    await expect(binding.resolveTextScope({ kind: 'document' })).rejects.toThrow('source changed');
    expect(calls.text).not.toHaveBeenCalled();
  });
  it('establishes a verified account lease for standalone private draft persistence', async () => {
    const value = controller(true);
    await value.load(); value.setDraft('Standalone private draft');
    await value.save();
    expect(calls.owner).toHaveBeenCalledOnce();
    expect(calls.create.mock.calls[0][0].context).toEqual({ kind: 'standalone' });
    await documentCoordination().invalidate();
    expect(() => value.setDraft('Must not reach a new account')).toThrow();
  });
});
