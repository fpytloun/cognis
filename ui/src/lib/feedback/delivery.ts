import { get, writable } from 'svelte/store';
import { feedbackApi } from './api';
import { createIndexedDbChatV2Outbox, type OutboxEntry } from '$lib/chat-v2/outbox';
import { sendOutboxEntry } from '$lib/chat-v2/outbox-send';
import { documentCoordination } from '$lib/document-coordination';
import type { SubmissionReceipt } from './types';

/** Eager journal observation and explicit recovery; independent of downloaded chrome. */
export class FeedbackDeliveryModel {
  readonly state = writable({
    pending: [] as OutboxEntry[], receipts: {} as Record<string, SubmissionReceipt>,
    error: '', busy: false, missing: [] as string[],
  });
  private readonly outbox = createIndexedDbChatV2Outbox();
  private readonly lease = documentCoordination().capture();
  private alive = true;
  private stopInvalidation = () => {};
  constructor(readonly conversationId: string) {}
  private patch(value: Partial<ReturnType<typeof this.snapshot>>) { this.state.update(state => ({ ...state, ...value })); }
  private snapshot() { return get(this.state); }
  private assertCurrent() {
    if (!this.alive) throw new Error('Feedback delivery view closed.');
    this.lease.assertCurrent();
  }
  start(): () => void {
    let stopped = false;
    const stop = () => { if (!stopped) { stopped = true; this.dispose(); } };
    void this.refresh();
    window.addEventListener('feedback-changed', this.refresh);
    window.addEventListener('focus', this.refresh);
    this.stopInvalidation = documentCoordination().onInvalidated(stop);
    return stop;
  }
  readonly refresh = async () => {
    try {
      const entries = await this.outbox.list(this.conversationId);
      this.assertCurrent();
      this.patch({ pending: entries.filter(entry => entry.feedback && ['sending', 'failed'].includes(entry.status)) });
    } catch { /* An invalidated document cannot expose retained packets. */ }
  };
  async check(entry: OutboxEntry) {
    if (!entry.feedback || this.snapshot().busy) return;
    this.patch({ busy: true, error: '' });
    try {
      this.assertCurrent();
      let receipt = await feedbackApi.receipt(entry.feedback.creation_key);
      this.assertCurrent();
      if (['pending', 'attempting', 'uncertain'].includes(receipt.delivery)) receipt = await feedbackApi.check(receipt.submission_id);
      this.assertCurrent();
      this.patch({ receipts: { ...this.snapshot().receipts, [entry.client_txn_id]: receipt } });
      if (receipt.delivery === 'accepted') {
        await sendOutboxEntry(this.outbox, entry);
        await this.refresh();
      }
    } catch (caught) {
      try {
        this.assertCurrent();
        if (caught && typeof caught === 'object' && 'status' in caught && caught.status === 404) {
          this.patch({ missing: [...this.snapshot().missing.filter(id => id !== entry.client_txn_id), entry.client_txn_id] });
        } else this.patch({ error: caught instanceof Error ? caught.message : 'Delivery could not be checked. The original message is retained.' });
      } catch { /* A stale document cannot authorize recovery. */ }
    } finally { if (this.alive) this.patch({ busy: false }); }
  }
  async retryOriginal(entry: OutboxEntry) {
    if (this.snapshot().busy || !this.snapshot().missing.includes(entry.client_txn_id)) return;
    this.patch({ busy: true, error: '' });
    try {
      await this.lease.validate();
      this.assertCurrent();
      await sendOutboxEntry(this.outbox, entry);
      this.assertCurrent();
      await this.refresh();
    } catch (caught) {
      if (this.alive) this.patch({ error: caught instanceof Error ? caught.message : 'Original message remains retained.' });
    } finally { if (this.alive) this.patch({ busy: false }); }
  }
  dispose() {
    if (!this.alive) return;
    this.alive = false;
    this.stopInvalidation();
    window.removeEventListener('feedback-changed', this.refresh);
    window.removeEventListener('focus', this.refresh);
    this.patch({ pending: [], receipts: {}, error: '', missing: [], busy: false });
  }
}
