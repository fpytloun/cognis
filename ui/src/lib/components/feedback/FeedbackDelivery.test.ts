import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { createIndexedDbChatV2Outbox, type OutboxEntry } from '$lib/chat-v2/outbox';
import { sendOutboxEntry } from '$lib/chat-v2/outbox-send';
import FeedbackDelivery from './FeedbackDelivery.svelte';

const calls = vi.hoisted(() => ({ receipt: vi.fn(), send: vi.fn() }));
vi.mock('$lib/feedback/api', () => ({ feedbackApi: { receipt: calls.receipt } }));
vi.mock('$lib/chat-v2/api', () => ({ chatV2Api: { sendMessage: calls.send } }));
vi.mock('$lib/chat-v2/attachment-uploads', () => ({
  attachmentUploads: { resolveAll: async (refs: unknown[]) => refs },
  isLocalAttachment: () => false, toServerAttachment: (ref: unknown) => ref,
}));
beforeEach(async () => { localStorage.clear(); await loginFixtureAccount(); });
afterEach(async () => { cleanup(); await auth.clear(); });
it('reconciles 404 after reload and explicitly retries exactly the retained transaction', async () => {
  const entry: OutboxEntry = { client_txn_id: crypto.randomUUID(), client_message_id: crypto.randomUUID(),
    conversation_id: crypto.randomUUID(), content: 'Original instruction', chat_mode: 'build',
    attachments: [{ artifact_id: 'art_87d6238b098f4798919dc4b299894379', filename: 'review.md', mime_type: 'text/markdown', kind: 'file', size_bytes: 40 }],
    feedback: { creation_key: crypto.randomUUID(), preflight_id: 'saved-preflight', manifest_hash: 'a'.repeat(64) },
    status: 'pending', created_at: '2026-10-26T17:00:00Z', updated_at: '2026-10-26T17:00:00Z' };
  calls.receipt.mockRejectedValue(Object.assign(new Error('No admission yet'), { status: 404 }));
  calls.send.mockRejectedValueOnce(new TypeError('Connection lost before admission'))
    .mockImplementation(async (conversation_id, client_txn_id, payload) => ({
      status: 'accepted', client_txn_id, client_message_id: payload.client_message_id,
      conversation_id, message_id: 'accepted-feedback-message', queue_id: null,
      cursor: 'feedback-admission-cursor', server_time: '2026-10-26T17:00:01Z',
    }));
  const outbox = createIndexedDbChatV2Outbox();
  await outbox.put(entry);
  await expect(sendOutboxEntry(outbox, entry)).rejects.toThrow('Connection lost');
  render(FeedbackDelivery, { conversationId: entry.conversation_id });
  await fireEvent.click(await screen.findByRole('button', { name: 'Check delivery' }));
  expect(calls.send).toHaveBeenCalledOnce();
  await fireEvent.click(await screen.findByRole('button', { name: 'Retry original message' }));
  await waitFor(() => expect(calls.send).toHaveBeenCalledTimes(2));
  expect(calls.send.mock.calls[1]).toEqual(calls.send.mock.calls[0]);
  await waitFor(async () => expect(await createIndexedDbChatV2Outbox().get(entry.client_txn_id)).toMatchObject({
    status: 'acked', feedback: entry.feedback, attachments: entry.attachments, chat_mode: 'build',
  }));
});
