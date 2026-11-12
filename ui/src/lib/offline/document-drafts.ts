import { documentCoordination, type DocumentCoordination, type DocumentLease } from '$lib/document-coordination';
import { isEmptySubmissionInput, normalizeSubmissionInput, sameSubmissionInput, type SubmissionInput } from '$lib/chat-v2/submission-input';
import { snapshotPersistedMetadata } from '$lib/chat-v2/persisted-metadata';

export interface CompositeInput extends SubmissionInput { input_revision: string }
export interface CompositeDraft extends CompositeInput {
  version: 3; revision: string; consumed: boolean;
}
export interface CompositeSubmission extends CompositeDraft { key: string }
export function compositeDraftKey(key: string): string {
  return key.replace(/^cognis:chat-v2-draft:/, 'cognis-chat-draft:');
}

interface Draft { revision: string; value: string }
export interface SubmissionDraft { readonly key: string; readonly revision: string; readonly value: string }

/**
 * One instance per composer. v2 keys never read/overwrite unknown-owner legacy
 * strings. Every write is compare-and-swap under the cross-document lock.
 */
export class DocumentDrafts {
  private lease?: DocumentLease;
  private observed = new Map<string, string | null>();
  private pending = new Map<string, string | null>();
  private queue: Promise<void> = Promise.resolve();
  private compositeObserved = new Map<string, string | null>();

  private compositeContext(key: string) { return this.context(`composer-v3:${compositeDraftKey(key)}`); }
  private compositeRecord(key: string): CompositeDraft | null {
    const raw = this.storage().getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as CompositeDraft;
    if (value.version !== 3 || typeof value.revision !== 'string'
      || typeof value.input_revision !== 'string' || typeof value.text !== 'string'
      || !Array.isArray(value.attachments) || typeof value.consumed !== 'boolean') {
      throw new Error('Invalid composite draft');
    }
    return value;
  }
  readComposite(key: string): CompositeDraft | null {
    const context = this.compositeContext(key), record = this.compositeRecord(context.key);
    this.compositeObserved.set(context.key, record?.revision ?? null);
    return record;
  }
  writeComposite(key: string, input: CompositeInput): Promise<CompositeDraft | null> {
    return this.saveComposite(key, input, false);
  }
  private saveComposite(key: string, input: CompositeInput, pin: boolean): Promise<CompositeDraft | null> {
    const context = this.compositeContext(key);
    const captured = snapshotPersistedMetadata({ input_revision: input.input_revision, ...normalizeSubmissionInput(input) });
    if (!this.compositeObserved.has(context.key)) this.readComposite(key);
    const expectedRevision = this.compositeObserved.get(context.key);
    const operation = this.queue.then(() => context.owner.run(`draft:${context.key}`, context.lease, async () => {
      const record = this.compositeRecord(context.key);
      if (record?.consumed && record.input_revision === captured.input_revision) return null;
      const staleObservation = (record?.revision ?? null) !== this.compositeObserved.get(context.key);
      // Empty initialization from another document has no input to protect.
      // Keep local queued clears and all consumed tombstones behind the CAS.
      const replacesEmpty = staleObservation && record && !record.consumed
        && isEmptySubmissionInput(record) && !isEmptySubmissionInput(captured);
      if ((staleObservation && !replacesEmpty)
        || (pin && record?.input_revision !== captured.input_revision
          && (record?.revision ?? null) !== expectedRevision && !replacesEmpty)) {
        this.storage().setItem(`${context.key}:retained:${captured.input_revision}`, JSON.stringify(captured));
        return null;
      }
      if (record?.input_revision === captured.input_revision) {
        if (!sameSubmissionInput(record, captured)) throw new Error('Composite input revision mismatch');
        return record;
      }
      const next: CompositeDraft = { ...captured, version: 3, revision: crypto.randomUUID(), consumed: false };
      this.storage().setItem(context.key, JSON.stringify(next));
      this.compositeObserved.set(context.key, next.revision);
      return next;
    }));
    this.queue = operation.then(() => undefined, () => undefined);
    return operation;
  }
  async persistComposite(key: string, input: CompositeInput): Promise<CompositeSubmission | null> {
    // Pinning is not a new edit. A prior queued save of a different intent
    // must not be overwritten merely because it updated our observed CAS token.
    const record = await this.saveComposite(key, input, true);
    return record ? { ...record, key } : null;
  }
  async consumeComposite(submitted: CompositeSubmission, isCurrent: () => boolean): Promise<boolean> {
    const context = this.compositeContext(submitted.key);
    // Linearize with saves, rather than invalidating them before they drain.
    // A newer preceding save wins the CAS; a later same-intent save sees the
    // tombstone and cannot resurrect consumed input.
    const operation = this.queue.then(() => context.owner.run(`draft:${context.key}`, context.lease, async () => {
      const record = this.compositeRecord(context.key);
      if (!isCurrent() || record?.revision !== submitted.revision
        || record.input_revision !== submitted.input_revision || !sameSubmissionInput(record, submitted)) return false;
      const next = { ...record, consumed: true, revision: crypto.randomUUID() };
      this.storage().setItem(context.key, JSON.stringify(next));
      this.compositeObserved.set(context.key, next.revision);
      return true;
    }));
    this.queue = operation.then(() => undefined, () => undefined);
    return operation;
  }

  constructor(private readonly owner: () => DocumentCoordination = documentCoordination,
    private readonly storage: () => Storage = () => localStorage) {}

  private context(key: string) {
    const owner = this.owner();
    this.lease ??= owner.capture();
    this.lease.assertCurrent();
    return { owner, lease: this.lease, key: owner.storageName(key, this.lease) };
  }
  private record(key: string): Draft | null {
    const raw = this.storage().getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Draft;
    if (typeof value.revision !== 'string' || typeof value.value !== 'string') throw new Error('Invalid draft');
    return value;
  }
  read(key: string): string | null {
    const context = this.context(key);
    if (this.pending.has(context.key)) return this.pending.get(context.key)!;
    const record = this.record(context.key);
    this.observed.set(context.key, record?.revision ?? null);
    return record?.value || null;
  }
  write(key: string, value: string | null): Promise<void> {
    const context = this.context(key);
    if (!this.observed.has(context.key)) this.read(key);
    this.pending.set(context.key, value);
    const operation = this.queue.then(() => context.owner.run(`draft:${context.key}`, context.lease, async () => {
      const record = this.record(context.key);
      if ((record?.revision ?? null) !== this.observed.get(context.key)) {
        // Preserve stale nonempty edits without overwriting the newer draft.
        // Conflict data stays scoped and unavailable to automatic restore.
        if (value) this.storage().setItem(`${context.key}:retained:${crypto.randomUUID()}`, value);
        return;
      }
      // Tombstones prevent ABA (another document clears/rewrites this draft).
      const next = { revision: crypto.randomUUID(), value: value ?? '' };
      this.storage().setItem(context.key, JSON.stringify(next));
      this.observed.set(context.key, next.revision);
    }));
    this.queue = operation.catch(() => undefined);
    void operation.finally(() => {
      if (this.pending.get(context.key) === value) this.pending.delete(context.key);
    }).catch(() => undefined);
    return operation;
  }
  /** Cleanup compares exact submitted text; never clears input typed meanwhile. */
  async removeIfValue(key: string, expected: string): Promise<void> {
    const context = this.context(key);
    await this.queue;
    await context.owner.run(`draft:${context.key}`, context.lease, async () => {
      const record = this.record(context.key);
      if (record?.value !== expected || record.revision !== this.observed.get(context.key)) return;
      const next = { revision: crypto.randomUUID(), value: '' };
      this.storage().setItem(context.key, JSON.stringify(next));
      this.observed.set(context.key, next.revision);
    });
  }
  settled(): Promise<void> { return this.queue; }

  /** Pin the submitted revision, not the mutable observation used by autosave. */
  persistSubmission(key: string, value: string): Promise<SubmissionDraft | null> {
    const context = this.context(key);
    if (!this.observed.has(context.key)) this.read(key);
    const operation = this.queue.then(() => {
      // Preceding local autosaves own their new revisions. Snapshot only after
      // they settle, but before waiting on the cross-document lock.
      const expected = this.observed.get(context.key);
      return context.owner.run(`draft:${context.key}`, context.lease, async () => {
      const record = this.record(context.key);
      if ((record?.revision ?? null) !== expected) {
        if (value) this.storage().setItem(`${context.key}:retained:${crypto.randomUUID()}`, value);
        return null;
      }
      const next = { revision: crypto.randomUUID(), value };
      this.storage().setItem(context.key, JSON.stringify(next));
      this.observed.set(context.key, next.revision);
      return Object.freeze({ key, ...next });
      });
    });
    this.queue = operation.then(() => undefined, () => undefined);
    return operation;
  }

  async consumeSubmission(submitted: SubmissionDraft, isCurrent: () => boolean): Promise<boolean> {
    const context = this.context(submitted.key);
    await this.queue;
    return context.owner.run(`draft:${context.key}`, context.lease, async () => {
      const record = this.record(context.key);
      if (!isCurrent() || record?.revision !== submitted.revision || record.value !== submitted.value) return false;
      const next = { revision: crypto.randomUUID(), value: '' };
      this.storage().setItem(context.key, JSON.stringify(next));
      this.observed.set(context.key, next.revision);
      return true;
    });
  }
}
