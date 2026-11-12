import { beforeEach, describe, expect, it, vi } from 'vitest';
import 'fake-indexeddb/auto';
import type { ComposerView } from '$lib/chat-v2/composer-submission';
import { ComposerSubmission } from '$lib/chat-v2/composer-submission';
import { DocumentDrafts } from '$lib/offline/document-drafts';
import { documentCoordination } from '$lib/document-coordination';
import { IndexedDbChatV2Outbox, CoordinatedOutbox, type OutboxEntry } from '$lib/chat-v2/outbox';
import { sendOutboxEntry } from '$lib/chat-v2/outbox-send';
import { dispatchChatComposerMessage } from '$lib/chat-v2/composer-dispatch';
import { sameSubmissionInput } from '$lib/chat-v2/submission-input';

const mocks = vi.hoisted(() => ({ send: vi.fn(), command: vi.fn(), receipt: vi.fn() }));
vi.mock('$lib/chat-v2/api', () => ({ chatV2Api: { sendMessage: mocks.send, executeCommand: mocks.command } }));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: { receipt: mocks.receipt } }));
vi.mock('$lib/chat-v2/attachment-uploads', () => ({
  attachmentUploads: { prepare: async () => undefined, resolveAll: async (refs: unknown[]) => refs, forget: vi.fn(), isDurable: async () => true },
  isLocalAttachment: () => false, toServerAttachment: (ref: unknown) => ref,
}));
const feedback = { creation_key: 'reviewed-packet-key', preflight_id: 'reviewed-preflight', manifest_hash: 'a'.repeat(64) };
const attachment = { artifact_id: 'art_87d6238b098f4798919dc4b299894379', kind: 'file' as const,
  filename: 'review.md', mime_type: 'text/markdown', size_bytes: 120 };
function fixture() {
  const raw = new IndexedDbChatV2Outbox(indexedDB, crypto.randomUUID(), documentCoordination().capture());
  const outbox = new CoordinatedOutbox(raw, documentCoordination());
  const drafts = new DocumentDrafts();
  const view: ComposerView = { generation: 1, conversationId: 'conversation-under-review',
    draftKey: 'conversation-composer', revision: 1, input_revision: 'reviewed-input-revision',
    text: 'Apply these selected changes.', attachments: [attachment], chat_mode: 'build', feedback };
  const entry: OutboxEntry = { client_txn_id: 'reviewed-message-transaction', client_message_id: 'reviewed-message',
    conversation_id: view.conversationId!, content: view.text, attachments: view.attachments,
    chat_mode: view.chat_mode, feedback, status: 'pending', created_at: '2026-10-26T17:00:00Z', updated_at: '2026-10-26T17:00:00Z' };
  return { outbox, drafts, view, entry };
}
beforeEach(async () => {
  localStorage.clear();
  await documentCoordination().initialize();
  await documentCoordination().beginLogin();
  await documentCoordination().verified('feedback-composer@cognis.test');
  mocks.send.mockReset().mockResolvedValue({ kind: 'accepted' });
  mocks.command.mockReset(); mocks.receipt.mockReset();
});
describe('feedback through the durable composer/admission boundary', () => {
  it('freezes feedback with attachments and mode, consumes the draft, and sends exactly one normal turn', async () => {
    const { outbox, drafts, view, entry } = fixture();
    const submission = new ComposerSubmission(view, entry, documentCoordination().capture());
    const reserved = await submission.prepare(drafts, outbox);
    let cleared = false;
    expect(await submission.consume(drafts, () => view, () => { cleared = true; })).toBe(true);
    expect(cleared).toBe(true);
    await sendOutboxEntry(outbox, reserved);
    expect(mocks.command).not.toHaveBeenCalled();
    expect(mocks.send).toHaveBeenCalledExactlyOnceWith(view.conversationId, reserved.client_txn_id,
      expect.objectContaining({ content: view.text, attachments: [attachment], chat_mode: 'build', feedback }));
    expect(new DocumentDrafts().readComposite(view.draftKey!)).toMatchObject({ consumed: true });
    expect(await outbox.get(reserved.client_txn_id)).toMatchObject({ status: 'acked', feedback });
  });
  it('supports a feedback-only message with the same admission path', async () => {
    const { outbox, entry } = fixture();
    entry.content = ''; entry.attachments = [];
    await outbox.put(entry);
    await sendOutboxEntry(outbox, entry);
    expect(mocks.send).toHaveBeenCalledOnce();
    expect(mocks.send.mock.calls[0][2]).toMatchObject({ content: '', attachments: [], feedback });
  });
  it('does not resend an uncertain packet and preserves its original transaction', async () => {
    const { outbox, entry } = fixture();
    await outbox.put(entry);
    mocks.send.mockRejectedValueOnce(new TypeError('Response lost'));
    await expect(sendOutboxEntry(outbox, entry)).rejects.toThrow('Response lost');
    const failed = (await outbox.get(entry.client_txn_id))!;
    expect(failed).toMatchObject({ status: 'failed', retryable: false, feedback });
    mocks.receipt.mockResolvedValue({ delivery: 'uncertain', submission_id: 'accepted-command-under-reconciliation' });
    await expect(sendOutboxEntry(outbox, failed)).rejects.toThrow('unresolved');
    expect(mocks.send).toHaveBeenCalledTimes(1);
    expect(mocks.receipt).toHaveBeenCalledWith(feedback.creation_key);
  });
  it('blocks actual commands, without silently dropping included feedback', async () => {
    await expect(dispatchChatComposerMessage({ conversationId: 'conversation-under-review', content: '/clear', feedback }))
      .rejects.toThrow('Exclude included feedback');
    expect(mocks.command).not.toHaveBeenCalled();
    expect(mocks.send).not.toHaveBeenCalled();
  });
  it('preserves a newer packet/input on another-tab CAS contention and refuses stale consumption', async () => {
    const { outbox, drafts, view, entry } = fixture();
    const submission = new ComposerSubmission(view, entry, documentCoordination().capture());
    await submission.prepare(drafts, outbox);
    const sibling = new DocumentDrafts();
    const current = sibling.readComposite(view.draftKey!)!;
    const changed = { ...current, input_revision: 'newer-feedback-input', feedback: { ...feedback, manifest_hash: 'b'.repeat(64) } };
    await sibling.writeComposite(view.draftKey!, changed);
    expect(await submission.consume(drafts, () => view, () => { throw new Error('Must not clear newer input'); })).toBe(false);
    expect(new DocumentDrafts().readComposite(view.draftKey!)).toMatchObject({ feedback: changed.feedback });
    expect(sameSubmissionInput(view, changed)).toBe(false);
  });
});
