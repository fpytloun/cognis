import type { MessageTimelineItem, RuntimeOverlaySnapshot, TimelineItem } from '$lib/chat-v2/types';

export interface SpeechSentence {
  message_id: string;
  sentence_index: number;
  text: string;
}

export interface SettledVoiceTurn {
  turn_id: string;
  items: readonly TimelineItem[];
}

function spokenText(text: string): string {
  return text
    .replace(/```[\s\S]*?(?:```|$)/g, '')
    .replace(/~~~[\s\S]*?(?:~~~|$)/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/^#{1,6}\s+|^\s*[-*+]\s+/gm, '')
    .replace(/[*_`]/g, '')
    .trim();
}

/**
 * Presentation-only speech projection of the admitted Chat v2 turn.
 * Uses cumulative text, not token/legacy frames, so HA relays and replayed
 * runtime updates have the same deduplication and completion behavior.
 */
export class ConversationSpeech {
  private messages = new Map<string, { content: string; offset: number }>();
  private index = 0;
  private finished = false;
  private turnId: string | null = null;
  private idleRevision: string | null = null;
  private revisions = new Map<string, number>();
  private reconciling = false;
  private reconcilePending = false;

  constructor(
    private readonly clientMessageId: string,
    private readonly onSentence: (sentence: SpeechSentence) => void,
    private readonly onComplete: () => void,
    private readonly onIdle: () => void,
    private readonly onDeliverable: (ref: string) => void = () => {},
  ) {}

  get activeTurnId(): string | null { return this.turnId; }

  update(runtime: RuntimeOverlaySnapshot, items: readonly TimelineItem[] = []): void {
    if (this.finished) return;
    if (runtime.runtime_revision < (this.revisions.get(runtime.runtime_epoch) ?? -1)) return;
    this.revisions.set(runtime.runtime_epoch, runtime.runtime_revision);
    const all = [...items, ...runtime.volatile_items];
    const input = all.find((item): item is MessageTimelineItem => item.kind === 'message'
      && item.role === 'user' && item.client_message_id === this.clientMessageId);
    this.turnId ??= input?.turn_id ?? (runtime.active_turn?.primary_client_message_id === this.clientMessageId
      ? runtime.active_turn.turn_id : null);
    this.readMessages(all);
    const revision = `${runtime.runtime_epoch}:${runtime.runtime_revision}`;
    if (!runtime.has_active_turn && this.idleRevision !== revision) {
      this.idleRevision = revision;
      this.onIdle();
    }
  }

  /** Finish only after authoritative admission and canonical snapshot recovery. */
  settle(turn: SettledVoiceTurn): void {
    if (this.finished) return;
    this.turnId = turn.turn_id;
    this.readMessages(turn.items);
    for (const message of this.messages.values()) this.emit(message, message.content.length);
    this.finished = true;
    const delivered = new Set<string>();
    for (const item of [...turn.items].sort((a, b) => a.sort_key.localeCompare(b.sort_key))) {
      if (item.kind !== 'assistant_deliverable' || item.turn_id !== this.turnId
        || delivered.has(item.deliverable_id)) continue;
      delivered.add(item.deliverable_id);
      this.onDeliverable(item.deliverable_id);
    }
    this.onComplete();
  }

  /** Coalesce observations without dropping an idle transition during a read. */
  async reconcile(
    recover: () => Promise<SettledVoiceTurn | null>,
    isCurrent: () => boolean,
    onError: () => void,
  ): Promise<void> {
    this.reconcilePending = true;
    if (this.reconciling) return;
    this.reconciling = true;
    try {
      while (this.reconcilePending && !this.finished && isCurrent()) {
        this.reconcilePending = false;
        try {
          const settled = await recover();
          if (!isCurrent()) return;
          if (settled) this.settle(settled);
        } catch {
          if (isCurrent()) {
            this.idleRevision = null;
            onError();
          }
        }
      }
    } finally {
      this.reconciling = false;
    }
  }

  private readMessages(items: readonly TimelineItem[]): void {
    if (!this.turnId) return;
    const messages = items.filter((item): item is MessageTimelineItem =>
      item.kind === 'message' && item.role === 'assistant' && item.turn_id === this.turnId
    ).sort((a, b) => a.sort_key.localeCompare(b.sort_key));
    for (const item of messages) {
      this.read(item, item.status === 'complete' || item.text_streaming === false);
    }
  }

  private read(item: MessageTimelineItem, complete: boolean): void {
    const previous = this.messages.get(item.id);
    // Never replay spoken text when a cumulative snapshot is repeated, shortened,
    // or replaced. A new assistant phase has its own canonical item identity.
    if (previous && !item.content.startsWith(previous.content.slice(0, previous.offset))) return;
    const message = previous ?? { content: '', offset: 0 };
    message.content = item.content;
    this.messages.set(item.id, message);
    if (complete) {
      this.emit(message, item.content.length);
      return;
    }
    const pending = item.content.slice(message.offset);
    let inFence = false;
    let fence = '';
    let start = 0;
    for (let i = 0; i < pending.length; i += 1) {
      const marker = pending.slice(i, i + 3);
      if (marker === '```' || marker === '~~~') {
        if (!inFence) { inFence = true; fence = marker; }
        else if (marker === fence) inFence = false;
        i += 2;
        continue;
      }
      const closing = !inFence && /[.!?]/.test(pending[i])
        ? pending.slice(i + 1).match(/^([*_`"'’”)\]]*)\s/) : null;
      const end = i + 1 + (closing?.[1].length ?? 0);
      if (closing && spokenText(pending.slice(start, end)).length >= 8) {
        this.emit(message, message.offset + end - start);
        start = end;
        i = end - 1;
      }
    }
  }

  private emit(message: { content: string; offset: number }, end: number): void {
    const text = spokenText(message.content.slice(message.offset, end));
    message.offset = end;
    if (text) this.onSentence({ message_id: this.clientMessageId, sentence_index: this.index++, text });
  }
}
