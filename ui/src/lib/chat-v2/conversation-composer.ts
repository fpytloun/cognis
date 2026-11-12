import { documentCoordination, type DocumentLease } from '$lib/document-coordination';
import { DocumentDrafts, type CompositeInput } from '$lib/offline/document-drafts';
import { emptyComposerInput, hydrateComposer, type ComposerHydration } from './composer-hydration';
import { isEmptySubmissionInput, sameSubmissionInput } from './submission-input';
import { snapshotPersistedMetadata } from './persisted-metadata';

interface Channel {
  hydration: ComposerHydration;
  dirty: boolean;
  listeners: Set<(input: CompositeInput) => void>;
}

/** One live input per conversation in this document. Persistence still uses
 * the existing cross-document CAS and outbox submission reservation. */
export class ConversationComposer {
  readonly drafts = new DocumentDrafts();
  private channels = new Map<string, Channel>();
  private readonly stopStorage: () => void;
  constructor(private readonly lease: DocumentLease) {
    const changed = (event: StorageEvent) => {
      if (event.storageArea !== localStorage) return;
      for (const [id, channel] of this.channels) {
        if (event.key !== documentCoordination().storageName(`composer-v3:cognis-chat-draft:${id}`, lease)
          || channel.dirty) continue;
        // Never adopt another document's input over an unpersisted local edit.
        channel.hydration = hydrateComposer(this.drafts, id);
        this.notify(channel);
      }
    };
    window.addEventListener('storage', changed);
    this.stopStorage = () => window.removeEventListener('storage', changed);
    lease.signal.addEventListener('abort', () => {
      this.stopStorage();
      this.channels.clear();
    }, { once: true });
  }
  private channel(id: string): Channel {
    this.lease.assertCurrent();
    let channel = this.channels.get(id);
    if (!channel) {
      channel = { hydration: hydrateComposer(this.drafts, id), dirty: false, listeners: new Set() };
      this.channels.set(id, channel);
    }
    return channel;
  }
  hydrate(id: string): ComposerHydration {
    return snapshotPersistedMetadata(this.channel(id).hydration);
  }
  subscribe(id: string, listener: (input: CompositeInput) => void): () => void {
    // Revocation already cleared these channels. A late view effect must not
    // reattach an observer or throw while the existing recovery UI is mounting.
    if (this.lease.signal.aborted) return () => {};
    this.lease.assertCurrent();
    // A failed hydration must remain fail-closed in its view, not retry from a
    // subscription effect and bypass the recovery UI's error boundary.
    const channel = this.channels.get(id);
    if (!channel) return () => {};
    channel.listeners.add(listener);
    listener(snapshotPersistedMetadata(channel.hydration.input));
    return () => { channel.listeners.delete(listener); };
  }
  private notify(channel: Channel): void {
    for (const listener of channel.listeners) listener(snapshotPersistedMetadata(channel.hydration.input));
  }
  update(id: string, input: CompositeInput): void {
    const channel = this.channel(id);
    const previous = channel.hydration.input;
    // Hydration, mounting and upload progress do not mint a new user intent.
    if (isEmptySubmissionInput(previous) && isEmptySubmissionInput(input)) return;
    if (previous.input_revision === input.input_revision && sameSubmissionInput(previous, input)) return;
    channel.hydration = { input: snapshotPersistedMetadata(input), legacy: [] };
    channel.dirty = true;
    this.notify(channel);
    void this.drafts.writeComposite(`cognis-chat-draft:${id}`, input).then((saved) => {
      this.lease.assertCurrent();
      if (saved && channel.hydration.input.input_revision === input.input_revision) channel.dirty = false;
    }).catch(() => { /* Submission reports persistence failures without discarding input. */ });
  }
  consumed(id: string, revision: string): void {
    const channel = this.channel(id);
    if (channel.hydration.input.input_revision !== revision) return;
    channel.hydration = { input: emptyComposerInput(), legacy: [] };
    channel.dirty = false;
    this.notify(channel);
  }
}

let current: { lease: DocumentLease; composer: ConversationComposer } | undefined;
export function conversationComposer(): ConversationComposer {
  const lease = documentCoordination().capture();
  if (!current || current.lease.signal.aborted || current.lease.owner !== lease.owner
    || current.lease.epoch !== lease.epoch) {
    current = { lease, composer: new ConversationComposer(lease) };
  }
  return current.composer;
}
