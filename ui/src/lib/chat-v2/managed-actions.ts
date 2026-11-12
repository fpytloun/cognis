import { api } from '$lib/api/client';
import { confirmAction } from '$lib/stores/confirm';
import type { Conversation } from '$lib/types/api';

export type ManagedAction = 'stop' | 'send' | 'take-control' | 'close';

/** Shared user-facing managed controls; the API remains the authority. */
export async function performManagedAction(
  conversation: Conversation,
  action: ManagedAction,
  payload?: Record<string, unknown>,
): Promise<{ conversation: Conversation; followUpConversationId: string | null } | null> {
  if (action === 'close' && !await confirmAction({
    title: 'Terminate channel conversation?',
    message: 'Stop the current turn and close the reply route. Both participants keep their visible history.',
    confirmLabel: 'Terminate conversation',
  })) return null;
  if (action === 'take-control' && !await confirmAction({
    title: 'Take control of managed conversation?',
    message: 'The managed conversation will be closed read-only and forked into a normal conversation.',
    confirmLabel: 'Take control',
  })) return null;
  const response = await api.conversations.managedAction(conversation.conversation_id, action, payload);
  const updated = response.managed_agent
    ? {
        ...conversation,
        managed_agent: response.managed_agent,
        has_active_turn: ['running', 'queued'].includes(response.managed_agent.turn_state ?? ''),
      }
    : await api.conversations.detail(conversation.conversation_id, { includeState: false });
  const followUpId = typeof response.result?.conversation_id === 'string'
    ? response.result.conversation_id
    : response.managed_agent?.follow_up_conversation_id;
  return { conversation: updated, followUpConversationId: action === 'take-control' ? followUpId ?? null : null };
}
