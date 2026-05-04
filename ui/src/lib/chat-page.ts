export interface ConversationRetryScope {
  sessions: boolean;
  history: boolean;
}

export const CHAT_STORAGE_KEYS = {
  enterToSend: 'cognis-chat-enter-to-send',
  selectedAgent: 'cognis-chat-selected-agent',
  selectedChannel: 'cognis-chat-selected-channel',
  sidebarCollapsed: 'cognis-chat-sidebar-collapsed',
  lastOpenedConversation: 'cognis-chat-last-opened-conversation'
} as const;

export const SESSION_LOG_PAGE_SIZE = 200;
export const SESSION_LOG_BOOTSTRAP_MAX_PAGES = 5;
export const SESSION_LOG_POLL_INTERVAL_MS = 3000;
export const SESSION_LOG_POLL_MAX_INTERVAL_MS = 30000;

export function nextPollDelayMs(currentDelayMs: number): number {
  const baseDelay = Math.max(currentDelayMs, SESSION_LOG_POLL_INTERVAL_MS);
  const doubled = Math.min(baseDelay * 2, SESSION_LOG_POLL_MAX_INTERVAL_MS);
  const jitter = 0.85 + Math.random() * 0.3;
  return Math.round(doubled * jitter);
}

export function nextConversationLoadId(current: number): number {
  return current + 1;
}

export function isCurrentConversationLoad(requestId: number, activeRequestId: number): boolean {
  return requestId === activeRequestId;
}

export function getConversationRetryScope(errors: {
  sessionsError: string;
  historyError: string;
}): ConversationRetryScope {
  return {
    sessions: Boolean(errors.sessionsError),
    history: Boolean(errors.historyError)
  };
}

export function getNextHistoryAfterSeq(response: {
  last_seq: number;
  items: Array<{ seq: number | null }>;
}): number {
  for (let index = response.items.length - 1; index >= 0; index -= 1) {
    const seq = response.items[index]?.seq;
    if (typeof seq === 'number' && seq > 0) {
      return seq;
    }
  }

  return response.last_seq > 0 ? response.last_seq : 0;
}

export function isRestorableChatConversation(conversation: {
  status?: string | null;
  context?: { type?: string | null } | null;
} | null | undefined): boolean {
  return conversation?.status === 'active' && (conversation.context?.type ?? '').toLowerCase() === 'web';
}

export function isPreSessionChatConversation(conversation: {
  status?: string | null;
  context?: { type?: string | null } | null;
  active_session_id?: string | null;
} | null | undefined, sessionCount = 0): boolean {
  return isRestorableChatConversation(conversation)
    && !conversation?.active_session_id
    && sessionCount === 0;
}

export function isMissingSessionError(message: string | null | undefined): boolean {
  const normalized = (message ?? '').trim().toLowerCase();
  return normalized.includes('session not found');
}

export function shouldAdoptConversationSessionId(
  currentActiveSessionId: string | null | undefined,
  eventType: string,
  eventSessionId: string | null | undefined,
): boolean {
  return !currentActiveSessionId
    && (eventType === 'message_complete' || eventType === 'session_recovered')
    && typeof eventSessionId === 'string'
    && eventSessionId.length > 0;
}

export function shouldSuppressPreSessionSocketError(params: {
  code?: string | null;
  message?: string | null;
  conversation: {
    status?: string | null;
    context?: { type?: string | null } | null;
    active_session_id?: string | null;
  } | null | undefined;
  sessionCount: number;
}): boolean {
  return params.code === 'not_found'
    && isPreSessionChatConversation(params.conversation, params.sessionCount)
    && isMissingSessionError(params.message);
}
