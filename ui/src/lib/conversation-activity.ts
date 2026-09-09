import {
  conversationAttentionLabel,
  conversationAttentionTone,
  conversationHasAttention,
  isTaskControlConversationSummary,
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

function pendingInputState(
  types: string[] | null | undefined,
): { label: string; critical: boolean } | null {
  const pending = new Set(types ?? []);
  if (pending.has('credential_request')) return { label: 'Credential required', critical: true };
  if (pending.has('auth_challenge')) return { label: 'Authentication required', critical: false };
  if (pending.has('escalation') || pending.has('gate') || pending.has('workflow_gate')) {
    return { label: 'Waiting for approval', critical: pending.has('escalation') };
  }
  if (pending.has('step_question')) return { label: 'Waiting for answer', critical: false };
  return pending.size > 0 ? { label: 'Waiting for input', critical: false } : null;
}

export function conversationActivityState(
  conversation: Pick<
    Conversation,
    | 'conversation_id'
    | 'has_active_turn'
    | 'has_unread'
    | 'last_message_at'
    | 'active_session_status'
    | 'active_session_completion_reason'
    | 'pending_notification_types'
    | 'context'
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
  const waiting = pendingInputState(conversation?.pending_notification_types);
  const emptyTaskControl = Boolean(
    conversation
    && isTaskControlConversationSummary(conversation as Conversation)
    && !conversation.last_message_at,
  );
  // An empty task-control conversation can inherit a stale/default unread
  // flag at creation. Event-only activity in normal conversations is valid.
  const unread = Boolean(conversation?.has_unread)
    && !emptyTaskControl
    && !options.open;

  const finalError = waiting?.critical ?? (!waiting && error);
  const finalAttention = Boolean(waiting && !waiting.critical)
    || (!waiting && !finalError && attention);
  const finalActive = !finalError && !finalAttention && active;
  const finalBackground = !finalError && !finalAttention && !finalActive && background;
  const finalUnread = !finalActive && !finalBackground && !finalError && !finalAttention && unread;

  let label = 'No new activity';
  if (waiting) label = waiting.label;
  else if (finalError) label = conversationAttentionLabel('rose');
  else if (finalAttention) label = conversationAttentionLabel('amber');
  else if (finalActive) label = 'Control chat is working';
  else if (finalBackground) label = 'Background work active';
  else if (finalUnread) label = 'Unread control chat activity';

  return {
    active: finalActive,
    background: finalBackground,
    attention: finalAttention,
    unread: finalUnread,
    error: finalError,
    tone: finalError ? 'rose' : finalAttention ? 'amber' : 'default',
    label,
  };
}
