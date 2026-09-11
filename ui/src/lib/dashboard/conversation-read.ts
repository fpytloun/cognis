import type { Conversation } from '$lib/types/api';

export interface DashboardConversationReadDependencies {
  markRead(conversationId: string): Promise<unknown>;
  optimisticRead(conversationId: string, readThrough: string | null): void;
  reconcile(): Promise<void>;
  isRetryable(error: unknown): boolean;
}

export interface ObservedConversationReadDependencies {
  markRead(conversationId: string): Promise<unknown>;
  optimisticRead(conversationId: string, readThrough: string | null): void;
  isObserved(conversationId: string): boolean;
  reconcile(): void;
}

export interface ObservedConversationVersion {
  conversationId: string;
  version: string;
  readThrough: string | null;
}

const MAX_MARK_READ_ATTEMPTS = 2;

export class ObservedConversationReadTracker {
  private attemptedVersions = new Map<string, string>();
  private inFlight = new Map<string, Promise<void>>();
  private pending = new Map<string, ObservedConversationVersion>();

  constructor(private readonly dependencies: ObservedConversationReadDependencies) {}

  observe(observation: ObservedConversationVersion): Promise<void> | null {
    if (!this.dependencies.isObserved(observation.conversationId)) return null;
    if (this.attemptedVersions.get(observation.conversationId) === observation.version) return null;

    const existing = this.inFlight.get(observation.conversationId);
    if (existing) {
      this.pending.set(observation.conversationId, observation);
      return existing;
    }

    this.attemptedVersions.set(observation.conversationId, observation.version);
    this.dependencies.optimisticRead(observation.conversationId, observation.readThrough);
    const operation = this.dependencies.markRead(observation.conversationId)
      .then(
        () => undefined,
        () => {
          if (this.attemptedVersions.get(observation.conversationId) === observation.version) {
            this.attemptedVersions.delete(observation.conversationId);
          }
          this.dependencies.reconcile();
        },
      )
      .finally(() => {
        if (this.inFlight.get(observation.conversationId) === operation) {
          this.inFlight.delete(observation.conversationId);
        }
        const pending = this.pending.get(observation.conversationId);
        this.pending.delete(observation.conversationId);
        if (
          pending
          && this.dependencies.isObserved(pending.conversationId)
          && this.attemptedVersions.get(pending.conversationId) !== pending.version
        ) {
          void this.observe(pending);
        }
      });
    this.inFlight.set(observation.conversationId, operation);
    return operation;
  }
}

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
