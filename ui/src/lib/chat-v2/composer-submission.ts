import type { AttachmentRef } from '$lib/types/api';
import { CoordinationUnavailableError, type DocumentLease } from '$lib/document-coordination';
import type { DocumentDrafts, CompositeSubmission } from '$lib/offline/document-drafts';
import { compositeDraftKey } from '$lib/offline/document-drafts';
import type { ChatV2Outbox, OutboxEntry } from './outbox';
import { attachmentUploads } from './attachment-uploads';
import { snapshotPersistedMetadata } from './persisted-metadata';
import { sameSubmissionInput } from './submission-input';
import type { ChatMode } from './types';
import type { IncludedFeedback } from '$lib/feedback/types';

export interface ComposerView {
  conversationId: string;
  generation: number;
  draftKey: string | null;
  revision: number;
  input_revision: string;
  chat_mode: ChatMode | null;
  text: string;
  attachments: AttachmentRef[];
  feedback?: IncludedFeedback | null;
}

function attachmentInput(refs: AttachmentRef[]): string {
  // Upload progress is not a user edit. A completing upload must not strand
  // the already-queued input in the composer and invite a second submission.
  return JSON.stringify(refs.map(({ upload_state, upload_progress, upload_error, ...ref }) => ref));
}

/** The normal composer's persistence/consumption boundary. */
export class ComposerSubmission {
  readonly view: ComposerView;
  entry: OutboxEntry;
  private draft: CompositeSubmission | null = null;
  private clearedRevision: number | null = null;
  private prepared = false;
  constructor(view: ComposerView, entry: OutboxEntry, private readonly lease: DocumentLease,
    private readonly uploads: Pick<typeof attachmentUploads, 'prepare'> = attachmentUploads) {
    this.view = snapshotPersistedMetadata(view);
    this.entry = snapshotPersistedMetadata(entry);
  }
  async prepare(
    drafts: DocumentDrafts,
    outbox: ChatV2Outbox,
    flushPendingDraft?: () => void,
  ): Promise<OutboxEntry> {
    // Drain view-owned autosave before pinning a revision across durable writes.
    flushPendingDraft?.();
    await this.lease.validate();
    if (!outbox.isDurable()) throw new CoordinationUnavailableError();
    if (!this.view.draftKey) throw new Error('Draft storage is not ready');
    if (!this.view.input_revision) throw new Error('Input revision is unavailable');
    this.draft = await drafts.persistComposite(this.view.draftKey, this.view);
    await this.lease.validate();
    if (!this.draft) throw new Error('Draft changed in another window. Input retained; review it before sending.');
    this.entry = await outbox.reserve(this.entry, { version: 1, draft_key: compositeDraftKey(this.view.draftKey),
      input_revision: this.view.input_revision, input: {
        text: this.view.text, attachments: this.view.attachments, chat_mode: this.view.chat_mode, feedback: this.view.feedback,
      } });
    if (this.entry.status === 'preparing') {
      await this.uploads.prepare(this.entry.attachments, this.lease);
      await this.lease.validate();
      this.entry = await outbox.activate(this.entry.client_txn_id, this.entry.attachments);
    }
    await this.lease.validate();
    if (!outbox.isDurable()) throw new CoordinationUnavailableError();
    this.prepared = true;
    return this.entry;
  }
  async consume(drafts: DocumentDrafts, current: () => ComposerView, clear: () => void): Promise<boolean> {
    if (!this.prepared || !this.draft || !this.matchesInput(current())) return false;
    const consumed = await drafts.consumeComposite(this.draft, () => this.matchesInput(current()));
    if (!consumed || !this.matchesInput(current())) return false;
    clear();
    this.clearedRevision = current().revision;
    return true;
  }
  canResize(current: ComposerView): boolean {
    return this.clearedRevision !== null && this.isCurrentView(current)
      && current.revision === this.clearedRevision && current.text === '' && current.attachments.length === 0 && !current.feedback;
  }
  isCurrentView(current: ComposerView): boolean {
    try { this.lease.assertCurrent(); } catch { return false; }
    return current.conversationId === this.view.conversationId
      && current.generation === this.view.generation && current.draftKey === this.view.draftKey;
  }
  matchesInput(current: ComposerView): boolean {
    return this.isCurrentView(current) && current.revision === this.view.revision
      && current.input_revision === this.view.input_revision
      && sameSubmissionInput(current, this.view);
  }
}
