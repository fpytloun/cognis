import { guestDraftKey, readPublicFeedbackSession } from './public-session';
import type { FeedbackAnchor, FeedbackLocator } from './types';

export interface GuestDraftInput {
  body: string; locator: FeedbackLocator | null; reply: string; reply_thread_id: string | null;
}
export type GuestCommentRequest =
  | { kind: 'thread'; anchor: FeedbackAnchor; body: string; original_locator: FeedbackLocator | null }
  | { kind: 'reply'; thread_id: string; expected_thread_version: number; reply_to_entry_id: string | null; body: string };
export type PendingGuestComment = GuestCommentRequest & { creation_key: string };
interface GuestDraftRecord extends GuestDraftInput {
  version: 1; content_digest: string; pending: PendingGuestComment | null;
}

/** One tab/share/guest owns an immutable pending request and separate editable input. */
export class GuestFeedbackDrafts {
  private key: string;
  constructor(private share: string, private guest: string, private digest: string) {
    this.key = guestDraftKey(share, guest);
  }
  read(): GuestDraftRecord | null {
    const raw = sessionStorage.getItem(this.key);
    if (!raw) return null;
    if (raw.length > 1024 * 1024) throw new Error('The retained guest comment exceeds the storage limit.');
    const value = JSON.parse(raw) as GuestDraftRecord;
    if (value.version !== 1 || value.content_digest !== this.digest || typeof value.body !== 'string'
      || typeof value.reply !== 'string' || (value.pending && (!value.pending.creation_key
        || !['thread', 'reply'].includes(value.pending.kind) || typeof value.pending.body !== 'string'))) {
      throw new Error('The retained guest comment cannot be read safely.');
    }
    return value;
  }
  private store(value: GuestDraftRecord): void {
    const raw = JSON.stringify(value);
    if (raw.length > 1024 * 1024) throw new Error('The guest comment exceeds the storage limit.');
    sessionStorage.setItem(this.key, raw);
    if (sessionStorage.getItem(this.key) !== raw) throw new Error('Guest comment could not be retained safely.');
  }
  write(input: GuestDraftInput): void {
    const current = this.read();
    this.store({ ...input, version: 1, content_digest: this.digest, pending: current?.pending ?? null });
  }
  freeze(request: GuestCommentRequest): PendingGuestComment {
    const session = readPublicFeedbackSession();
    if (session?.access.share_id !== this.share || session.guest?.guest_session_id !== this.guest) {
      throw new Error('The guest session must be retained before saving comments.');
    }
    const current = this.read();
    if (current?.pending) return structuredClone(current.pending);
    const pending = structuredClone({ ...request, creation_key: crypto.randomUUID() });
    this.store({ body: '', locator: null, reply: '', reply_thread_id: null, ...current,
      version: 1, content_digest: this.digest, pending });
    return pending;
  }
  complete(creationKey: string, input: GuestDraftInput): void {
    if (this.read()?.pending?.creation_key !== creationKey) throw new Error('The retained guest save changed.');
    this.store({ ...input, version: 1, content_digest: this.digest, pending: null });
  }
}
