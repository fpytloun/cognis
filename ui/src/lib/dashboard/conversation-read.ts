import type { Conversation } from '$lib/types/api';

export interface DashboardConversationReadDependencies {
  markRead(conversationId: string): Promise<unknown>;
  optimisticRead(conversationId: string, readThrough: string | null): void;
  reconcile(): Promise<void>;
  isRetryable(error: unknown): boolean;
}

const MAX_MARK_READ_ATTEMPTS = 2;

export class DashboardConversationProjectionGate {
  private generation = 0;

  begin(): number {
    this.generation += 1;
    return this.generation;
  }

  invalidate(): void {
    this.generation += 1;
  }

  isCurrent(generation: number): boolean {
    return generation === this.generation;
  }
}

export class DashboardConversationReadTracker {
  private openConversationIds = new Set<string>();
  private loadedScopes = new Set<string>();
  private pendingInitialLoads = new Map<string, Conversation>();
  private attemptedUnreadVersions = new Map<string, string>();
  private inFlight = new Map<string, Promise<void>>();
  private pendingUnread = new Map<string, Conversation>();

  constructor(private readonly dependencies: DashboardConversationReadDependencies) {}

  open(conversationId: string): void {
    this.openConversationIds.add(conversationId);
    const pending = this.pendingInitialLoads.get(conversationId);
    if (!pending) return;
    this.pendingInitialLoads.delete(conversationId);
    void this.initialLoaded(pending);
  }

  close(conversationId?: string): void {
    if (conversationId) {
      this.openConversationIds.delete(conversationId);
      this.loadedScopes.delete(`conversation:${conversationId}`);
      this.pendingInitialLoads.delete(conversationId);
      return;
    }
    this.openConversationIds.clear();
    this.loadedScopes.clear();
    this.pendingInitialLoads.clear();
  }

  initialLoaded(conversation: Conversation): Promise<void> | null {
    if (!this.openConversationIds.has(conversation.conversation_id)) {
      this.pendingInitialLoads.set(conversation.conversation_id, conversation);
      return null;
    }
    const scopeKey = `conversation:${conversation.conversation_id}`;
    if (this.loadedScopes.has(scopeKey)) return null;
    this.loadedScopes.add(scopeKey);
    return this.markRead(conversation);
  }

  observe(conversations: Conversation[]): Promise<void> | null {
    const unread = conversations.filter((conversation) => (
      this.openConversationIds.has(conversation.conversation_id)
      && conversation.has_unread
      && this.attemptedUnreadVersions.get(conversation.conversation_id) !== this.unreadVersion(conversation)
    ));
    if (unread.length === 0) return null;
    return Promise.all(unread.map((conversation) => this.markRead(conversation))).then(() => undefined);
  }

  private unreadVersion(conversation: Conversation): string {
    return conversation.last_message_at
      ?? conversation.updated_at
      ?? conversation.active_session_id
      ?? 'initial';
  }

  private markRead(conversation: Conversation): Promise<void> {
    const conversationId = conversation.conversation_id;
    const existing = this.inFlight.get(conversationId);
    if (existing) {
      this.pendingUnread.set(conversationId, conversation);
      return existing;
    }

    const version = this.unreadVersion(conversation);
    this.attemptedUnreadVersions.set(conversationId, version);
    this.dependencies.optimisticRead(conversationId, conversation.last_message_at ?? null);
    const operation = this.runMarkRead(conversationId)
      .finally(() => {
        if (this.inFlight.get(conversationId) === operation) {
          this.inFlight.delete(conversationId);
        }
        const pending = this.pendingUnread.get(conversationId);
        this.pendingUnread.delete(conversationId);
        if (
          pending
          && this.openConversationIds.has(pending.conversation_id)
          && this.unreadVersion(pending) !== version
        ) {
          void this.markRead(pending);
        }
      });
    this.inFlight.set(conversationId, operation);
    return operation;
  }

  private async runMarkRead(conversationId: string): Promise<void> {
    for (let attempt = 1; attempt <= MAX_MARK_READ_ATTEMPTS; attempt += 1) {
      try {
        await this.dependencies.markRead(conversationId);
        return;
      } catch (error) {
        if (attempt < MAX_MARK_READ_ATTEMPTS && this.dependencies.isRetryable(error)) continue;
        await this.dependencies.reconcile();
        return;
      }
    }
  }
}
