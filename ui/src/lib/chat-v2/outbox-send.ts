// Durable outbox sending shared by chat surfaces.
//
// A message is written to the outbox before it is sent, keeps its attachment
// references while their uploads finish in the background, and is resent with
// the same client transaction id until the server admits it. Transient
// failures are retried automatically in order; permanent rejections are left
// for the user.

import { attachmentUploads, isLocalAttachment, toServerAttachment } from './attachment-uploads';
import { chatV2Api } from './api';
import type { ComposerDispatchResult } from './composer-dispatch';
import type { ChatV2Outbox, OutboxEntry } from './outbox';
import { CoordinatedOutbox } from './outbox';
import { isRetryableSendError } from './outbox-retry';
import type { AttachmentRef } from '$lib/types/api';
import { documentCoordination, StaleDocumentError, CoordinationUnavailableError } from '$lib/document-coordination';
import { recordPerf } from '$lib/perf-log';
import { feedbackApi } from '$lib/feedback/api';

async function measureSend<T>(
  path: 'attachments' | 'admission' | 'receipt',
  operation: () => Promise<T>,
): Promise<T> {
  const started = performance.now();
  let outcome = 'error';
  try {
    const result = await operation();
    outcome = 'success';
    return result;
  } finally {
    recordPerf({ kind: 'send', path, ms: performance.now() - started, detail: outcome });
  }
}

/**
 * Finish background uploads for an entry and persist the server references,
 * so a later retry does not upload the files again.
 */
export async function resolveOutboxAttachments(
  outbox: ChatV2Outbox,
  entry: Pick<OutboxEntry, 'client_txn_id' | 'attachments'>,
): Promise<AttachmentRef[]> {
  // Entries written by older builds may have no attachments field.
  const refs = entry.attachments ?? [];
  if (!refs.some(isLocalAttachment)) return refs.map(toServerAttachment);
  const resolved = await attachmentUploads.resolveAll(refs);
  await outbox.update(entry.client_txn_id, { attachments: resolved }).catch(() => undefined);
  return resolved;
}

/** True when the entry (text and attachment files) survives a reload. */
export async function isOutboxEntryDurable(
  outbox: ChatV2Outbox,
  stored: boolean,
  attachments: AttachmentRef[],
): Promise<boolean> {
  if (!stored || !outbox.isDurable()) return false;
  const files = await Promise.all((attachments ?? []).map((ref) => attachmentUploads.isDurable(ref)));
  return files.every(Boolean);
}

/**
 * Send one outbox entry (uploads first); throws on failure.
 *
 * Outbox entries hold already-normalized message content. They are sent
 * straight to the message API with their stored ids, never re-parsed as
 * composer input (that could turn e.g. "/plan /yolo on" into a command).
 * An admission receipt remains in the canonical outbox so a queued sibling
 * document can settle without duplicating the network send.
 */
export async function sendOutboxEntry(
  outbox: ChatV2Outbox,
  entry: OutboxEntry,
  sendMessage: typeof chatV2Api.sendMessage = (...args) => chatV2Api.sendMessage(...args),
): Promise<ComposerDispatchResult> {
  const owner = outbox instanceof CoordinatedOutbox ? outbox.coordination : documentCoordination();
  const lease = outbox instanceof CoordinatedOutbox ? outbox.lease : owner.capture();
  if (!outbox.isDurable()) throw new CoordinationUnavailableError();
  const lockStarted = performance.now();
  return owner.run(`send:${lease.owner}:${entry.conversation_id}`, lease, async () => {
   recordPerf({ kind: 'send', path: 'outbox-lock', ms: performance.now() - lockStarted });
  // Re-read after acquisition: a sibling may have admitted or removed this row.
  const current = await outbox.get(entry.client_txn_id);
  await lease.validate();
  if (!current || current.owner !== lease.owner) throw new StaleDocumentError();
  entry = current;
  if (entry.status === 'preparing') throw new Error('Submission preparation is incomplete');
  if (entry.admitted) {
    return { kind: 'message', response: entry.admitted,
      clientTxnId: entry.client_txn_id, clientMessageId: entry.client_message_id };
  }
  if (entry.status === 'acked') throw new Error('Admission receipt unavailable; transaction retained for recovery');
  // Reconcile a response-loss packet before replaying its existing admission.
  if (entry.feedback && (entry.status === 'sending' || entry.status === 'failed')) {
    try {
      const receipt = await feedbackApi.receipt(entry.feedback.creation_key);
      await lease.validate();
      if (receipt.delivery !== 'accepted') {
        throw new Error(receipt.delivery === 'rejected'
          ? 'Feedback was rejected. Review it again before sending a new message.'
          : 'Feedback delivery is unresolved. Check delivery before retrying.');
      }
    } catch (error) {
      if (!(error && typeof error === 'object' && 'status' in error && error.status === 404)) throw error;
    }
  }
  try {
   const attachments = await measureSend('attachments', () => resolveOutboxAttachments(outbox, entry));
  await lease.validate();
  await outbox.update(entry.client_txn_id, { status: 'sending', updated_at: new Date().toISOString() })
    .catch(() => undefined);
  await lease.validate();
   const response = await measureSend('admission', () => sendMessage(entry.conversation_id, entry.client_txn_id, {
    content: entry.content,
    attachments,
    client_message_id: entry.client_message_id,
    chat_mode: entry.chat_mode ?? undefined,
    ...(entry.feedback ? { feedback: entry.feedback } : {}),
   }));
  await lease.validate();
   await measureSend('receipt', () => outbox.update(entry.client_txn_id, { status: 'acked', admitted: response }));
  await lease.validate();
  // Keep receipts for sibling entries/windows that reference the same file.
  // Only account-scoped retention cleanup may delete durable upload receipts.
  return {
    kind: 'message',
    response,
    clientTxnId: entry.client_txn_id,
    clientMessageId: entry.client_message_id,
  };
  } catch (error) {
    await lease.validate();
    await recordOutboxFailure(outbox, entry.client_txn_id, error);
    throw error;
  } finally {
    try {
      lease.assertCurrent();
      if (entry.feedback && typeof window !== 'undefined') window.dispatchEvent(new Event('feedback-changed'));
    } catch { /* An old account must not notify a new document. */ }
  }
  });
}

/** Record a failed send; returns whether it should be retried automatically. */
async function recordOutboxFailure(
  outbox: ChatV2Outbox,
  clientTxnId: string,
  error: unknown,
): Promise<boolean> {
  if (error instanceof StaleDocumentError) return false;
  const current = await outbox.get(clientTxnId);
  if (current?.status === 'acked' || current?.admitted) return false;
  const retryable = !current?.feedback && isRetryableSendError(error);
  await outbox.update(clientTxnId, {
    status: 'failed',
    updated_at: new Date().toISOString(),
    last_error: error instanceof Error ? error.message : String(error),
    retryable,
  }).catch(() => undefined);
  return retryable;
}

/**
 * Reconcile a delayed UI failure under the same admission lock. No durable
 * writes here. An admitted txn projects success, never failure/retry/restore.
 * Callbacks must not acquire admission again; use assertCurrent after awaits.
 */
export async function projectAdmissionFailure(
  outbox: ChatV2Outbox, entry: OutboxEntry,
  onPending: (current: OutboxEntry | null, assertCurrent: () => void) => Promise<void> | void,
  onAdmitted: (result: ComposerDispatchResult) => Promise<void> | void,
): Promise<'pending' | 'admitted' | 'stale'> {
  const owner = outbox instanceof CoordinatedOutbox ? outbox.coordination : documentCoordination();
  const lease = outbox instanceof CoordinatedOutbox ? outbox.lease : owner.capture();
  try {
    return await owner.run(`send:${lease.owner}:${entry.conversation_id}`, lease, async () => {
      const current = await outbox.get(entry.client_txn_id);
      await lease.validate();
      if (current?.admitted) {
        await onAdmitted({ kind: 'message', response: current.admitted,
          clientTxnId: current.client_txn_id, clientMessageId: current.client_message_id });
        return 'admitted';
      }
      if (current?.status === 'acked') return 'admitted';
      await onPending(current, () => lease.assertCurrent());
      await lease.validate();
      return 'pending';
    });
  } catch (error) {
    if (error instanceof StaleDocumentError) return 'stale';
    throw error;
  }
}

/** Queued (not yet admitted, not permanently rejected) entries in send order. */
export async function pendingOutboxEntries(
  outbox: ChatV2Outbox,
  conversationId: string,
): Promise<OutboxEntry[]> {
  return (await outbox.list(conversationId))
    .filter((entry) => entry.conversation_id === conversationId && entry.status !== 'preparing' && entry.status !== 'acked' && !entry.admitted)
    .sort((left, right) =>
      left.created_at.localeCompare(right.created_at)
      || left.client_txn_id.localeCompare(right.client_txn_id)
    );
}

export interface DrainOutboxOptions {
  sendMessage?: typeof chatV2Api.sendMessage;
  outbox: ChatV2Outbox;
  conversationId: string;
  /** Stop when the view moved to another conversation. */
  isCurrent: () => boolean;
  onAdmitted: (entry: OutboxEntry, result: ComposerDispatchResult) => Promise<void> | void;
  /**
   * Projects still-pending failures under the admission lock after re-reading
   * the row. Never restore queued content or acquire admission from here.
   * UI updates must be synchronous or separately fenced after every await.
   */
  onFailed?: (entry: OutboxEntry, error: unknown, retryable: boolean) => Promise<void> | void;
  /**
   * Brackets the whole send of one entry (attachment uploads, request, and
   * the admitted/failed callback): `entry` while in flight, then null.
   */
  onInFlight?: (entry: OutboxEntry | null) => void;
}

/**
 * Send queued entries in order. Stops at the first transient failure so later
 * messages never overtake an earlier one; skips permanently rejected entries.
 */
export async function drainConversationOutbox(
  options: DrainOutboxOptions,
): Promise<{ sentAny: boolean; retryRemaining: boolean }> {
  const { outbox, conversationId, isCurrent, onAdmitted, onFailed, onInFlight } = options;
  let sentAny = false;
  const entries = (await pendingOutboxEntries(outbox, conversationId))
    .filter((entry) => entry.retryable !== false);
  for (const entry of entries) {
    if (!isCurrent()) return { sentAny, retryRemaining: false };
    onInFlight?.(entry);
    try {
      let result: ComposerDispatchResult;
      try {
        result = await sendOutboxEntry(outbox, entry, options.sendMessage);
      } catch (error) {
        if (error instanceof StaleDocumentError) return { sentAny, retryRemaining: false };
        let retryable = false;
        const projection = await projectAdmissionFailure(outbox, entry, async (current, assertCurrent) => {
          retryable = current?.retryable !== false && isRetryableSendError(error);
          await onFailed?.(current ?? entry, error, retryable);
          assertCurrent();
        }, async (admitted) => {
          sentAny = true;
          if (isCurrent()) await onAdmitted(entry, admitted);
        });
        if (projection === 'stale') return { sentAny, retryRemaining: false };
        if (retryable) return { sentAny, retryRemaining: true };
        continue;
      }
      sentAny = true;
      if (isCurrent()) await onAdmitted(entry, result);
    } finally {
      onInFlight?.(null);
    }
  }
  return { sentAny, retryRemaining: false };
}
