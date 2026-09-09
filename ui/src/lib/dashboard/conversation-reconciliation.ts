import type { AttentionActionSummary, Conversation } from '$lib/types/api';

export class AttentionActionTombstones {
  private readonly byConversation = new Map<string, Set<string>>();
  private readonly knownActionsByConversation = new Map<string, Set<string>>();

  has(conversationId: string, actionId: string): boolean {
    return this.byConversation.get(conversationId)?.has(actionId) ?? false;
  }

  add(conversationId: string, actionId: string): void {
    let actions = this.byConversation.get(conversationId);
    if (!actions) {
      actions = new Set<string>();
      this.byConversation.set(conversationId, actions);
    }
    actions.add(actionId);
  }

  hasKnownConversation(conversationId: string): boolean {
    return this.knownActionsByConversation.has(conversationId);
  }

  reconcileCompleteProjection(
    conversationId: string,
    current: AttentionActionSummary[] | undefined,
    incoming: AttentionActionSummary[],
  ): AttentionActionSummary[] {
    const knownActionIds = new Set([
      ...(this.knownActionsByConversation.get(conversationId) ?? []),
      ...(current ?? []).map((action) => action.action_id),
    ]);
    const incomingIds = new Set(incoming.map((action) => action.action_id));
    for (const actionId of knownActionIds) {
      if (!incomingIds.has(actionId)) this.add(conversationId, actionId);
    }
    const currentById = new Map((current ?? []).map((action) => [action.action_id, action]));
    const accepted = incoming.flatMap((action) => {
      if (this.has(conversationId, action.action_id)) return [];
      const previous = currentById.get(action.action_id);
      return [previous && previous.revision > action.revision ? previous : action];
    });
    this.knownActionsByConversation.set(
      conversationId,
      new Set(accepted.map((action) => action.action_id)),
    );
    return accepted;
  }

  rememberCurrent(
    conversationId: string,
    current: AttentionActionSummary[] | undefined,
  ): void {
    if (current === undefined || this.knownActionsByConversation.has(conversationId)) return;
    this.knownActionsByConversation.set(
      conversationId,
      new Set(current.map((action) => action.action_id)),
    );
  }

  dropConversation(conversationId: string): void {
    this.byConversation.delete(conversationId);
    this.knownActionsByConversation.delete(conversationId);
  }

  clear(): void {
    this.byConversation.clear();
    this.knownActionsByConversation.clear();
  }
}

function mergeAttentionActions(
  conversationId: string,
  current: AttentionActionSummary[] | undefined,
  incoming: AttentionActionSummary[] | undefined,
  tombstones?: AttentionActionTombstones,
): AttentionActionSummary[] | undefined {
  if (incoming === undefined) {
    tombstones?.rememberCurrent(conversationId, current);
    return current;
  }
  if (tombstones) {
    return tombstones.reconcileCompleteProjection(conversationId, current, incoming);
  }
  const currentById = new Map((current ?? []).map((action) => [action.action_id, action]));
  return incoming.map((action) => {
    const previous = currentById.get(action.action_id);
    return previous && previous.revision > action.revision ? previous : action;
  });
}

export function mergeReconciledConversation(
  current: Conversation,
  incoming: Conversation,
  tombstones?: AttentionActionTombstones,
): Conversation {
  return {
    ...current,
    ...incoming,
    attention_actions: mergeAttentionActions(
      incoming.conversation_id,
      current.attention_actions,
      incoming.attention_actions,
      tombstones,
    ),
  };
}

export function reconcileConversationProjection(
  current: Conversation[],
  canonicalPage: Conversation[],
  details: Conversation[],
  tombstones?: AttentionActionTombstones,
  retainTail: (conversation: Conversation) => boolean = () => true,
): Conversation[] {
  const canonicalIds = new Set(canonicalPage.map((item) => item.conversation_id));
  const currentById = new Map(current.map((item) => [item.conversation_id, item]));
  const currentIds = new Set(current.map((item) => item.conversation_id));
  const detailsById = new Map(
    details
      .filter((item) => currentIds.has(item.conversation_id))
      .map((item) => [item.conversation_id, item]),
  );
  for (const detail of details) {
    if (
      currentIds.has(detail.conversation_id)
      || detail.attention_actions === undefined
      || !tombstones?.hasKnownConversation(detail.conversation_id)
    ) continue;
    tombstones.reconcileCompleteProjection(
      detail.conversation_id,
      undefined,
      detail.attention_actions,
    );
  }
  const reconciledCanonical = canonicalPage.map((incoming) => (
    mergeReconciledConversation(
      currentById.get(incoming.conversation_id) ?? {
        ...incoming,
        attention_actions: [],
      },
      incoming,
      tombstones,
    )
  ));
  const retainedTail = current
    .filter((item) => !canonicalIds.has(item.conversation_id))
    .map((item) => {
      const detail = detailsById.get(item.conversation_id);
      if (!detail) return item;
      if (!retainTail(detail)) return null;
      return mergeReconciledConversation(item, detail, tombstones);
    })
    .filter((item): item is Conversation => item !== null);
  return [...reconciledCanonical, ...retainedTail];
}
