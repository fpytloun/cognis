import { DocumentDrafts } from '$lib/offline/document-drafts';
import { documentCoordination } from '$lib/document-coordination';
import type { FeedbackLocator } from './types';
import type { feedbackApi } from './api';
export interface FeedbackDraftRecord {
  body: string; locator: FeedbackLocator | null;
  pending: { request: Parameters<typeof feedbackApi.createThread>[0]; original_locator: FeedbackLocator | null } | null;
}

/** Feedback drafts use the same account lease and cross-document CAS as chat. */
export class FeedbackDrafts {
  private drafts = new DocumentDrafts();
  private readonly lease = documentCoordination().capture();
  private key(source: string): string { return `feedback-draft:${source}`; }
  read(source: string): FeedbackDraftRecord | null {
    this.lease.assertCurrent();
    const raw = this.drafts.read(this.key(source));
    if (!raw || raw.length > 262144) return null;
    try { const value = JSON.parse(raw) as FeedbackDraftRecord; return typeof value.body === 'string' ? value : null; }
    catch { return null; }
  }
  write(source: string, body: FeedbackDraftRecord): Promise<void> {
    this.lease.assertCurrent();
    return this.drafts.write(this.key(source), JSON.stringify(body));
  }
  async pin(source: string, body: FeedbackDraftRecord): Promise<void> {
    this.lease.assertCurrent();
    if (!await this.drafts.persistSubmission(this.key(source), JSON.stringify(body))) {
      throw new Error('This comment changed in another window. Reopen feedback before retrying.');
    }
    this.lease.assertCurrent();
  }
  consume(source: string, body: FeedbackDraftRecord): Promise<void> { return this.drafts.removeIfValue(this.key(source), JSON.stringify(body)); }
}
