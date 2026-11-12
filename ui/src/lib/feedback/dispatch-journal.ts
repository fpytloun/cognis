import { DocumentDrafts } from '$lib/offline/document-drafts';
import { documentCoordination } from '$lib/document-coordination';
import type { FeedbackPreflight, SubmissionReceipt } from './types';

export interface FeedbackDispatchRecord {
  creation_key: string; preflight: FeedbackPreflight; receipt: SubmissionReceipt | null; overall_message: string | null;
}
/** Standalone feedback dispatch has one account-fenced, durable request owner. */
export class FeedbackDispatchJournal {
  private drafts = new DocumentDrafts();
  private lease = documentCoordination().capture();
  private key: Promise<string>;
  private queue: Promise<void> = Promise.resolve();
  constructor(subjects: string[]) {
    this.key = crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([...subjects].sort())))
      .then(bytes => `feedback-dispatch:${Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('')}`);
  }
  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    // Reads also change the draft's CAS observation. Serialize whole operations,
    // while leaving cross-document locking and authority checks with the drafts.
    const result = this.queue.then(operation);
    this.queue = result.then(() => undefined, () => undefined);
    return result;
  }
  load(): Promise<FeedbackDispatchRecord | null> {
    return this.enqueue(() => this.read());
  }
  private async read(): Promise<FeedbackDispatchRecord | null> {
    this.lease.assertCurrent();
    const raw = this.drafts.read(await this.key);
    this.lease.assertCurrent();
    if (!raw) return null;
    if (raw.length > 2 * 1024 * 1024) throw new Error('The retained feedback request cannot be read safely.');
    const value = JSON.parse(raw) as FeedbackDispatchRecord;
    if (!value.creation_key || !value.preflight?.manifest_hash || !value.preflight.preflight_id
      || (value.overall_message !== null && typeof value.overall_message !== 'string')) throw new Error('The retained feedback request is invalid.');
    return value;
  }
  freeze(preflight: FeedbackPreflight, creation_key: string, overall_message: string | null = null): Promise<FeedbackDispatchRecord> {
    return this.enqueue(async () => {
      const existing = await this.read();
      if (existing) {
        if (existing.preflight.manifest_hash !== preflight.manifest_hash || existing.overall_message !== overall_message) throw new Error('Check the previous feedback delivery before preparing another packet.');
        return existing;
      }
      const value = structuredClone({ creation_key, preflight, receipt: null, overall_message });
      const saved = await this.drafts.persistSubmission(await this.key, JSON.stringify(value));
      this.lease.assertCurrent();
      if (!saved) throw new Error('Feedback delivery changed in another window. Check the saved send before retrying.');
      return value;
    });
  }
  record(receipt: SubmissionReceipt): Promise<void> {
    return this.enqueue(async () => {
      const value = await this.read();
      if (!value || value.preflight.manifest_hash !== receipt.manifest_hash
        || value.creation_key !== receipt.creation_key) throw new Error('Feedback receipt does not match the retained packet.');
      if (value.receipt && ['accepted', 'rejected'].includes(value.receipt.delivery)
        && !['accepted', 'rejected'].includes(receipt.delivery)) return;
      const saved = await this.drafts.persistSubmission(await this.key, JSON.stringify({ ...value, receipt }));
      this.lease.assertCurrent();
      if (!saved) throw new Error('Feedback delivery changed in another window. Check the saved send before retrying.');
    });
  }
  clearAccepted(creationKey: string): Promise<void> {
    return this.enqueue(async () => {
      await this.lease.validate();
      const value = await this.read();
      if (!value || value.creation_key !== creationKey) throw new Error('The reviewed request changed. Reopen its receipt before starting another review.');
      if (value.receipt?.delivery !== 'accepted' || value.receipt.creation_key !== value.creation_key
        || value.receipt.manifest_hash !== value.preflight.manifest_hash) throw new Error('Resolve delivery before starting another feedback request.');
      await this.drafts.removeIfValue(await this.key, JSON.stringify(value));
      this.lease.assertCurrent();
      if (await this.read()) throw new Error('Feedback delivery changed in another window. Check the saved send before starting another review.');
    });
  }
}
