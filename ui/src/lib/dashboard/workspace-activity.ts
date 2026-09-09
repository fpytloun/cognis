import { conversationActivityState, type ActivityAvatarState } from '$lib/conversation-activity';
import type { Conversation } from '$lib/types/api';
import type { WorkspaceWindowState } from './workspace';

const ACTIVE_STATUSES = new Set(['active', 'queued', 'ready', 'running', 'starting']);
const ATTENTION_STATUSES = new Set(['attention', 'blocked', 'paused', 'waiting']);
const ERROR_STATUSES = new Set(['error', 'failed', 'terminated']);

function neutralState(label = 'No new activity'): ActivityAvatarState {
  return {
    active: false,
    background: false,
    attention: false,
    unread: false,
    error: false,
    tone: 'default',
    label,
  };
}

export function entityActivityState(status: string | null | undefined): ActivityAvatarState {
  const normalized = status?.trim().toLowerCase() ?? '';
  if (ACTIVE_STATUSES.has(normalized)) {
    return { ...neutralState('Work is active'), active: true };
  }
  if (ERROR_STATUSES.has(normalized)) {
    return { ...neutralState('Work failed'), error: true, tone: 'rose' };
  }
  if (ATTENTION_STATUSES.has(normalized)) {
    return { ...neutralState('Work needs attention'), attention: true, tone: 'amber' };
  }
  return neutralState();
}

export function workspaceWindowActivityState(
  window: WorkspaceWindowState,
  conversation: Conversation | null,
): ActivityAvatarState {
  if (window.kind === 'conversation' || window.resolvedConversationId) {
    return conversationActivityState(conversation, { open: !window.minimized });
  }
  return entityActivityState(window.status);
}

export function workspaceConversationForWindow(
  window: WorkspaceWindowState,
  conversations: readonly Conversation[],
): Conversation | null {
  const conversationId = window.kind === 'conversation'
    ? window.entityId
    : window.resolvedConversationId;
  return conversations.find((item) => item.conversation_id === conversationId) ?? null;
}

export function workspaceWindowTitle(
  window: WorkspaceWindowState,
  conversation: Conversation | null,
): string {
  return conversation?.title ?? window.title;
}
