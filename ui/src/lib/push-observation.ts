const MAX_OBSERVED_CONVERSATIONS_PER_CLIENT = 50;

function timestampMillis(value: unknown): number | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}

export class PushObservationRegistry {
  private readonly observedByClient = new Map<string, Map<string, number>>();

  record(clientId: string, conversationId: unknown, observedAt: unknown): void {
    if (!clientId) return;
    const normalizedConversationId = typeof conversationId === 'string' ? conversationId.trim() : '';
    const observedTimestamp = timestampMillis(observedAt);
    if (!normalizedConversationId || observedTimestamp === null) return;

    const observations = this.observedByClient.get(clientId) ?? new Map<string, number>();
    const previousTimestamp = observations.get(normalizedConversationId);
    if (previousTimestamp === undefined || observedTimestamp > previousTimestamp) {
      observations.delete(normalizedConversationId);
      observations.set(normalizedConversationId, observedTimestamp);
    }
    while (observations.size > MAX_OBSERVED_CONVERSATIONS_PER_CLIENT) {
      const oldestConversationId = observations.keys().next().value;
      if (typeof oldestConversationId !== 'string') break;
      observations.delete(oldestConversationId);
    }
    this.observedByClient.set(clientId, observations);
  }

  wasObserved(
    clientIds: Iterable<string>,
    conversationId: unknown,
    occurredAt: unknown,
  ): boolean {
    const normalizedConversationId = typeof conversationId === 'string' ? conversationId.trim() : '';
    const occurredTimestamp = timestampMillis(occurredAt);
    if (!normalizedConversationId || occurredTimestamp === null) return false;

    for (const clientId of clientIds) {
      const observedTimestamp = this.observedByClient.get(clientId)?.get(normalizedConversationId);
      if (observedTimestamp !== undefined && observedTimestamp >= occurredTimestamp) return true;
    }
    return false;
  }

  retainClients(clientIds: Iterable<string>): void {
    const retainedClientIds = new Set(clientIds);
    for (const clientId of this.observedByClient.keys()) {
      if (!retainedClientIds.has(clientId)) this.observedByClient.delete(clientId);
    }
  }
}
