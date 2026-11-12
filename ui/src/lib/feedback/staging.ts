import { writable } from 'svelte/store';
import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
import { conversationComposer } from '$lib/chat-v2/conversation-composer';
import { feedbackApi } from './api';
import { DocumentDrafts } from '$lib/offline/document-drafts';
import type { FeedbackReviewSource } from './review-selection';
import type { FeedbackStagingSnapshot, FeedbackEntryVersion, SelectedFeedbackVersion, FeedbackPreflight, IncludedFeedback, FeedbackThread } from './types';

/** Comments a chat can include: the latest version of each open, staged human entry. */
export interface ChatFeedbackEntries { staging: FeedbackStagingSnapshot; threads: FeedbackThread[]; entries: FeedbackEntryVersion[]; sources: FeedbackReviewSource[] }

/** Mounted composers apply feedback changes to their own live input, keeping unsaved text. */
export const COMPOSER_FEEDBACK_EVENT = 'cognis:composer-feedback';
export interface ComposerFeedbackDetail { conversationId: string; feedback: IncludedFeedback | null; expected?: string }

/**
 * Change only the included feedback of a conversation's composer input. The live composer owns
 * the text; rewriting the whole input from a stored copy could drop text typed since it was saved.
 */
function setComposerFeedback(conversationId: string, feedback: IncludedFeedback | null, expected?: string): void {
  const event = new CustomEvent<ComposerFeedbackDetail>(COMPOSER_FEEDBACK_EVENT, { cancelable: true, detail: { conversationId, feedback, expected } });
  if (!window.dispatchEvent(event)) return;
  const composer = conversationComposer();
  const latest = composer.hydrate(conversationId).input;
  if (expected !== undefined && latest.feedback?.creation_key !== expected) return;
  composer.update(conversationId, { ...latest, input_revision: crypto.randomUUID(), feedback });
}

export class ConversationFeedback {
  readonly snapshot = writable<FeedbackStagingSnapshot | null>(null);
  readonly error = writable('');
  readonly loading = writable(false);
  /** An inclusion change is running; every surface of this chat disables its controls. */
  readonly busy = writable(false);
  private refreshGeneration = 0;
  private changes: Promise<unknown> = Promise.resolve();
  /** Inclusion changes from the chip, the comment list and document panels run one at a time. */
  private serial<T>(change: () => Promise<T>): Promise<T> {
    const run = this.changes.catch(() => undefined).then(async () => {
      this.busy.set(true);
      try { return await change(); } finally { this.busy.set(false); }
    });
    this.changes = run;
    return run;
  }
  private drafts = new DocumentDrafts();
  constructor(readonly conversationId: string, private readonly lease: DocumentLease) {}
  async refresh(): Promise<void> {
    const generation = ++this.refreshGeneration;
    this.loading.set(true);
    try {
      const value = await feedbackApi.staging(this.conversationId);
      this.lease.assertCurrent();
      if (generation === this.refreshGeneration) { this.snapshot.set(value); this.error.set(''); }
    } catch (caught) {
      if (generation !== this.refreshGeneration) return;
      this.snapshot.set(null);
      try { this.lease.assertCurrent(); } catch { return; }
      this.error.set(caught instanceof Error ? caught.message : 'Feedback is unavailable.');
    } finally { if (generation === this.refreshGeneration) this.loading.set(false); }
  }
  /** Load the threads behind this chat's staged comments. */
  async entries(signal?: AbortSignal): Promise<ChatFeedbackEntries> {
    const staging = await feedbackApi.staging(this.conversationId);
    this.lease.assertCurrent();
    this.snapshot.set(staging);
    const threads: FeedbackThread[] = [];
    for (const subject of new Set(staging.items.map(item => item.subject_id))) {
      let cursor: string | undefined;
      do {
        const page = await feedbackApi.threads(subject, cursor, signal ?? this.lease.signal);
        this.lease.assertCurrent();
        threads.push(...page.items); cursor = page.next_cursor ?? undefined;
      } while (cursor);
    }
    // Match by entry, not version: an edited comment is staged again under its new version.
    const staged = new Set(staging.items.map(item => item.entry_id));
    const entries = threads.filter(thread => thread.state === 'open' && !thread.hidden)
      .flatMap(thread => thread.entries).filter(entry => staged.has(entry.entry_id));
    const sources = threads.flatMap(thread => thread.entries.map(entry => ({
      entry_version_id: entry.entry_version_id, version: entry.version, thread_id: thread.thread_id,
      anchor_id: thread.anchor.anchor_id, source: thread.anchor.source,
    } satisfies FeedbackReviewSource)));
    return { staging, threads, entries, sources };
  }
  /**
   * Make exactly these comments the ones included with the next message. An empty choice clears
   * the inclusion. This is the one owner of include, exclude and re-review for a chat.
   */
  setIncluded(chosen: FeedbackEntryVersion[], sources: FeedbackReviewSource[], reviewLease: Pick<DocumentLease, 'assertCurrent'> = this.lease): Promise<void> {
    return this.serial(() => this.replace(chosen, sources, reviewLease));
  }
  /** Add comments to what is already included, for example every ready comment at once. */
  add(chosen: FeedbackEntryVersion[], sources: FeedbackReviewSource[], reviewLease: Pick<DocumentLease, 'assertCurrent'> = this.lease): Promise<void> {
    return this.serial(async () => {
      const picked = new Set(chosen.map(entry => entry.entry_id));
      const staging = await feedbackApi.staging(this.conversationId);
      this.lease.assertCurrent();
      const others = staging.items.filter(item => item.inclusion === 'included' && !picked.has(item.entry_id));
      if (!others.length) { await this.replace(chosen, sources, reviewLease); return; }
      // Keep what is already included (e.g. comments on another document) next to the new choice;
      // the explicit choice wins, even for a version staging no longer offers (already sent).
      const loaded = await this.entries();
      const kept = new Set(others.map(item => item.entry_id));
      await this.replace([...loaded.entries.filter(entry => kept.has(entry.entry_id)), ...chosen],
        [...loaded.sources, ...sources], reviewLease);
    });
  }
  /** Include every comment that is ready in this chat. */
  addAll(): Promise<void> {
    return this.serial(async () => {
      const { entries, sources } = await this.entries();
      await this.replace(entries, sources, this.lease);
    });
  }
  private async replace(chosen: FeedbackEntryVersion[], sources: FeedbackReviewSource[], reviewLease: Pick<DocumentLease, 'assertCurrent'>): Promise<void> {
    // Keep the composer's binding until the replacement is confirmed: if re-review fails, the
    // stale binding blocks sending instead of letting the message go without its comments.
    await this.clear(chosen.length > 0);
    if (chosen.length) {
      const preflight = await this.include(chosen, sources);
      await this.confirm(preflight, reviewLease, Object.fromEntries(chosen.map(entry => [entry.entry_version_id, entry.entry_id])));
    }
    await this.refresh();
  }
  /**
   * Re-review the message's comments after the reviewed packet expired, went missing or no longer
   * matches staging (for example an edited comment). The composer's reviewed packet is the intent:
   * a retry after a half-finished re-review (rows already back to ready) still restores all of it.
   * Without a packet, the rows staging still marks included are the intent.
   */
  reinclude(): Promise<void> {
    return this.serial(async () => {
      const { staging, threads, entries, sources } = await this.entries();
      const binding = conversationComposer().hydrate(this.conversationId).input.feedback;
      const packet = binding ? this.review(binding) : null;
      let wanted: Set<string>;
      if (packet) {
        // Map reviewed versions to their comments, so an edited comment carries its new version.
        // The packet's own entries name the comment even after staging and threads moved on.
        const byVersion = new Map<string, string>(Object.entries(this.reviewedEntries(binding!)));
        for (const item of packet.context_items) byVersion.set(item.entry.entry_version_id, item.entry.entry_id);
        for (const item of staging.items) byVersion.set(item.entry_version_id, item.entry_id);
        for (const entry of threads.flatMap(thread => thread.entries)) byVersion.set(entry.entry_version_id, entry.entry_id);
        const ids = packet.selection.map(item => byVersion.get(item.entry_version_id));
        // Never silently drop a comment the message carried: an unknown one blocks sending instead.
        if (ids.some(id => !id)) throw new Error('A comment in this message changed. Choose the comments again.');
        wanted = new Set(ids as string[]);
        const missing = [...wanted].filter(id => !entries.some(entry => entry.entry_id === id));
        if (missing.length) throw new Error('A comment in this message is no longer available. Choose the comments again.');
      } else {
        wanted = new Set(staging.items.filter(item => item.inclusion === 'included').map(item => item.entry_id));
      }
      await this.replace(entries.filter(entry => wanted.has(entry.entry_id)), sources, this.lease);
    });
  }
  private async include(entries: FeedbackEntryVersion[], sources: FeedbackReviewSource[]): Promise<FeedbackPreflight> {
    const reviewed = entries.map(({ entry_id, entry_version_id, version, thread_id }) => ({ entry_id, entry_version_id, version, thread_id }));
    const snapshot = await feedbackApi.staging(this.conversationId);
    this.lease.assertCurrent();
    const selection: SelectedFeedbackVersion[] = [];
    for (const entry of reviewed) {
      const previous = snapshot.items.find(item => item.entry_id === entry.entry_id);
      const item = await feedbackApi.stage(this.conversationId, entry.entry_id, {
        expected_version: previous?.staging_version ?? 0, entry_version_id: entry.entry_version_id, inclusion: 'included',
      });
      this.lease.assertCurrent();
      selection.push({ entry_version_id: entry.entry_version_id, expected_entry_version: entry.version, staging_version: item.staging_version });
    }
    const preflight = await feedbackApi.preflight({ context: { kind: 'chat', conversation_id: this.conversationId },
      selection, reviewed_guest_entry_version_ids: [], overall_message: null,
      destination: { kind: 'origin_chat', conversation_id: this.conversationId }, update_mode: 'follow_request' });
    this.lease.assertCurrent();
    return preflight;
  }
  /** Reviewed version → comment (entry) ids, kept with the packet so an edit cannot orphan it. */
  private reviewedEntries(binding: IncludedFeedback): Record<string, string> {
    const raw = this.drafts.read(`feedback-review-entries:${binding.creation_key}`);
    return raw ? JSON.parse(raw) : {};
  }
  async confirm(preflight: FeedbackPreflight, reviewLease: Pick<DocumentLease, 'assertCurrent'> = this.lease, entryIds: Record<string, string> = {}): Promise<IncludedFeedback> {
    this.lease.assertCurrent();
    if (preflight.destination.kind !== 'origin_chat' || preflight.destination.conversation_id !== this.conversationId) {
      throw new Error('Feedback belongs to a different conversation.');
    }
    if (Date.parse(preflight.expires_at) <= Date.now()) throw new Error('Review expired. Review the feedback again.');
    const feedback = { creation_key: crypto.randomUUID(), preflight_id: preflight.preflight_id, manifest_hash: preflight.manifest_hash };
    await this.drafts.write(`feedback-review:${feedback.creation_key}`, JSON.stringify(preflight));
    await this.drafts.write(`feedback-review-entries:${feedback.creation_key}`, JSON.stringify(entryIds));
    const snapshot = await feedbackApi.staging(this.conversationId);
    this.lease.assertCurrent();
    reviewLease.assertCurrent();
    if (preflight.selection.some(selected => !snapshot.items.some(item =>
      item.entry_version_id === selected.entry_version_id && item.staging_version === selected.staging_version
      && item.inclusion === 'included'))) throw new Error('Feedback selection changed. Review it again.');
    this.snapshot.set(snapshot);
    setComposerFeedback(this.conversationId, feedback);
    return feedback;
  }
  review(binding: IncludedFeedback): FeedbackPreflight | null {
    this.lease.assertCurrent();
    const raw = this.drafts.read(`feedback-review:${binding.creation_key}`);
    if (!raw) return null;
    const value: FeedbackPreflight = JSON.parse(raw);
    if (value.preflight_id !== binding.preflight_id || value.manifest_hash !== binding.manifest_hash) return null;
    return value;
  }
  /**
   * Clear the inclusion: every row this chat has included goes back to ready, whether or not the
   * reviewed packet still describes it, so nothing stays included behind the composer's back.
   */
  exclude(): Promise<void> { return this.serial(() => this.clear()); }
  private async clear(keepBinding = false): Promise<void> {
    this.lease.assertCurrent();
    const key = conversationComposer().hydrate(this.conversationId).input.feedback?.creation_key;
    const snapshot = await feedbackApi.staging(this.conversationId);
    this.lease.assertCurrent();
    for (const row of snapshot.items.filter(item => item.inclusion === 'included')) {
      await feedbackApi.stage(this.conversationId, row.entry_id, { expected_version: row.staging_version,
        entry_version_id: row.entry_version_id, inclusion: 'prepared' });
      this.lease.assertCurrent();
    }
    if (key && !keepBinding) setComposerFeedback(this.conversationId, null, key);
    await this.refresh();
  }
}
let channels = new Map<string, ConversationFeedback>();
let lease: DocumentLease | undefined;
export function conversationFeedback(id: string): ConversationFeedback {
  const next = documentCoordination().capture();
  if (!lease || lease.signal.aborted || lease.owner !== next.owner || lease.epoch !== next.epoch) {
    lease = next; channels = new Map();
  }
  let channel = channels.get(id);
  if (!channel) { channel = new ConversationFeedback(id, lease); channels.set(id, channel); }
  return channel;
}
