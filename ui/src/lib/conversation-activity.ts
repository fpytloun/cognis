import {
  conversationAttentionLabel,
  conversationAttentionTone,
  conversationHasAttention,
} from '$lib/chat-page';
import { backgroundWorkItemIsRunning } from '$lib/ongoing-work';
import type { BackgroundWorkItem, Conversation } from '$lib/types/api';

export interface ActivityAvatarState {
  active: boolean;
  background: boolean;
  attention: boolean;
  unread: boolean;
  error: boolean;
  tone: 'default' | 'amber' | 'rose';
  label: string;
}

const ERROR_SESSION_STATUSES = new Set(['failed', 'terminated']);

export function conversationActivityState(
  conversation: Pick<
    Conversation,
    | 'conversation_id'
    | 'has_active_turn'
    | 'has_unread'
    | 'active_session_status'
    | 'active_session_completion_reason'
    | 'pending_notification_types'
  > | null,
  options: {
    open?: boolean;
    runtimeActive?: boolean | null;
    backgroundWork?: BackgroundWorkItem[];
  } = {},
): ActivityAvatarState {
  const active = Boolean(options.runtimeActive ?? conversation?.has_active_turn);
  const background = !active && Boolean(conversation && options.backgroundWork?.some((item) => (
    item.controller_conversation_id === conversation.conversation_id
    && backgroundWorkItemIsRunning(item)
  )));
  const attentionTone = conversation ? conversationAttentionTone(conversation) : 'default';
  const error = attentionTone === 'rose' || Boolean(
    conversation?.active_session_status
    && ERROR_SESSION_STATUSES.has(conversation.active_session_status),
  );
  const attention = Boolean(conversation && conversationHasAttention(conversation) && attentionTone === 'amber');
  const unread = Boolean(conversation?.has_unread) && !options.open;

  const finalActive = active;
  const finalBackground = !finalActive && background;
  const finalError = !finalActive && !finalBackground && error;
  const finalAttention = !finalActive && !finalBackground && !finalError && attention;
  const finalUnread = !finalActive && !finalBackground && !finalError && !finalAttention && unread;

  let label = 'No new activity';
  if (finalActive) label = 'Control chat is working';
  else if (finalBackground) label = 'Background work active';
  else if (finalError) label = conversationAttentionLabel('rose');
  else if (finalAttention) label = conversationAttentionLabel('amber');
  else if (finalUnread) label = 'Unread control chat activity';

  return {
    active: finalActive,
    background: finalBackground,
    attention: finalAttention,
    unread: finalUnread,
    error: finalError,
    tone: error ? 'rose' : attention ? 'amber' : 'default',
    label,
  };
}
