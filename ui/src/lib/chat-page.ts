export interface ConversationRetryScope {
  sessions: boolean;
  history: boolean;
}

export type ConversationStatusFilter = 'active' | 'starred' | 'archived';
export type ConversationAttentionTone = 'default' | 'amber' | 'rose';
export type ChatModeTone = 'default' | 'plan' | 'build';

const ATTENTION_PENDING_NOTIFICATION_TYPES = new Set<string>([
  'auth_challenge',
  'credential_request',
  'escalation',
  'gate',
  'step_question',
]);

const ROSE_PENDING_NOTIFICATION_TYPES = new Set<string>([
  'credential_request',
  'escalation',
]);

const ROSE_SESSION_STATUSES = new Set<string>(['failed', 'terminated']);
const AMBER_SESSION_STATUSES = new Set<string>(['cancelled', 'suspended']);
const NORMAL_COMPLETION_REASONS = new Set<string>([
  'compacted',
  'step_approved',
  'user_reset',
]);

const ROOT_SESSION_TIMELINE_EVENT_TYPES = new Set<string>([
  'assistant_stream_snapshot',
  'assistant_thinking_block',
  'assistant_thinking_chunk',
  'chunk',
  'message_complete',
  'tool_call',
  'tool_progress',
  'tool_output_chunk',
  'tool_result',
  'tool_result_chunk',
]);

const CONVERSATION_STATUS_FILTERS = new Set<ConversationStatusFilter>(['active', 'starred', 'archived']);

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
export const CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX = 24;
export const CHAT_USER_SCROLL_DELTA_THRESHOLD_PX = 2;

export interface ChatScrollState {
  distanceFromBottom: number;
  userScrolledUp: boolean;
}

export function distanceFromScrollBottom(params: {
  scrollHeight: number;
  scrollTop: number;
  clientHeight: number;
}): number {
  return Math.max(0, params.scrollHeight - params.scrollTop - params.clientHeight);
}

export function isNearScrollBottom(
  distanceFromBottom: number,
  thresholdPx = CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
): boolean {
  return distanceFromBottom <= thresholdPx;
}

export function nextChatScrollState(params: {
  currentScrollTop: number;
  lastScrollTop: number;
  distanceFromBottom: number;
  userScrolledUp: boolean;
  userScrollIntentUp: boolean;
  bottomThresholdPx?: number;
  scrollDeltaThresholdPx?: number;
}): ChatScrollState {
  const bottomThresholdPx = params.bottomThresholdPx ?? CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX;
  const scrollDeltaThresholdPx = params.scrollDeltaThresholdPx ?? CHAT_USER_SCROLL_DELTA_THRESHOLD_PX;
  const userMovedUp = params.currentScrollTop < params.lastScrollTop - scrollDeltaThresholdPx;

  if (params.userScrollIntentUp || (userMovedUp && params.distanceFromBottom > 0)) {
    return {
      distanceFromBottom: params.distanceFromBottom,
      userScrolledUp: true,
    };
  }

  if (isNearScrollBottom(params.distanceFromBottom, bottomThresholdPx)) {
    return {
      distanceFromBottom: params.distanceFromBottom,
      userScrolledUp: false,
    };
  }

  return {
    distanceFromBottom: params.distanceFromBottom,
    userScrolledUp: params.userScrolledUp,
  };
}

export function nextPollDelayMs(currentDelayMs: number): number {
  const baseDelay = Math.max(currentDelayMs, SESSION_LOG_POLL_INTERVAL_MS);
  const doubled = Math.min(baseDelay * 2, SESSION_LOG_POLL_MAX_INTERVAL_MS);
  const jitter = 0.85 + Math.random() * 0.3;
  return Math.round(doubled * jitter);
}

export function nextConversationLoadId(current: number): number {
  return current + 1;
}

export function parseConversationStatusFilter(value: string | null | undefined): ConversationStatusFilter {
  return CONVERSATION_STATUS_FILTERS.has(value as ConversationStatusFilter)
    ? value as ConversationStatusFilter
    : 'active';
}

export function setConversationStatusSearchParam(params: URLSearchParams, status: ConversationStatusFilter): void {
  if (status === 'active') {
    params.delete('status');
  } else {
    params.set('status', status);
  }
}

export function conversationStatusFilterForConversation(
  conversation: { status?: string | null; starred_at?: string | null },
  currentFilter: ConversationStatusFilter,
): ConversationStatusFilter {
  if (currentFilter === 'starred' && conversation.starred_at) return 'starred';
  if (conversation.status === 'archived') return 'archived';
  return 'active';
}

export function conversationAttentionTone(conversation: {
  active_session_status?: string | null;
  active_session_completion_reason?: string | null;
  pending_notification_types?: string[] | null;
}): ConversationAttentionTone {
  const status = conversation.active_session_status ?? null;
  if (status && ROSE_SESSION_STATUSES.has(status)) return 'rose';
  if (status && AMBER_SESSION_STATUSES.has(status)) return 'amber';

  const pendingNotificationTypes = conversation.pending_notification_types ?? [];
  if (pendingNotificationTypes.some((type) => ROSE_PENDING_NOTIFICATION_TYPES.has(type))) {
    return 'rose';
  }
  if (pendingNotificationTypes.some((type) => ATTENTION_PENDING_NOTIFICATION_TYPES.has(type))) {
    return 'amber';
  }

  const completionReason = conversation.active_session_completion_reason ?? null;
  if (status === 'completed' && completionReason && !NORMAL_COMPLETION_REASONS.has(completionReason)) {
    return 'amber';
  }

  return 'default';
}

export function conversationAttentionDotClass(tone: ConversationAttentionTone): string {
  if (tone === 'rose') return 'bg-rose-400';
  if (tone === 'amber') return 'bg-amber-400';
  return 'bg-sky-400';
}

export function conversationAttentionOrbitClass(tone: ConversationAttentionTone): string {
  if (tone === 'rose') return 'conversation-turn-orbit--rose';
  if (tone === 'amber') return 'conversation-turn-orbit--amber';
  return '';
}

export function conversationAttentionLabel(tone: ConversationAttentionTone): string {
  if (tone === 'rose') return 'requires attention: session failed or ended unexpectedly';
  if (tone === 'amber') return 'requires attention: blocked or waiting for input';
  return 'unread';
}

export function pendingNotificationTypesFromNotifications(
  notifications: Array<{ notification_type?: string | null; status?: string | null }>
): string[] {
  const types = new Set<string>();
  for (const notification of notifications) {
    if (notification.status !== 'pending' || !notification.notification_type) continue;
    types.add(notification.notification_type);
  }
  return [...types];
}

export function normalizeChatModeTone(value: unknown): ChatModeTone {
  return value === 'plan' || value === 'build' ? value : 'default';
}

export function buildConversationUrl(
  conversationId: string,
  status: ConversationStatusFilter,
  extraParams?: URLSearchParams | Record<string, string>,
): string {
  const params = extraParams instanceof URLSearchParams
    ? new URLSearchParams(extraParams)
    : new URLSearchParams(extraParams ?? undefined);
  setConversationStatusSearchParam(params, status);
  const query = params.toString();
  return `/chat/${conversationId}${query ? `?${query}` : ''}`;
}

export function isCurrentConversationLoad(requestId: number, activeRequestId: number): boolean {
  return requestId === activeRequestId;
}

export function shouldReconcileAfterReconnect(params: {
  remoteLastSeq?: number | null;
  activeSessionLastSeq: number;
  remoteHasActiveTurn?: boolean | null;
  localTurnInProgress: boolean;
}): boolean {
  const remoteLastSeq = typeof params.remoteLastSeq === 'number' ? params.remoteLastSeq : 0;
  if (remoteLastSeq > params.activeSessionLastSeq) return true;
  if (params.remoteHasActiveTurn === false && params.localTurnInProgress) return true;
  return false;
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
  context?: { type?: string | null; platform_data?: Record<string, unknown> | null } | null;
} | null | undefined): boolean {
  return conversation?.status === 'active'
    && (conversation.context?.type ?? '').toLowerCase() === 'web'
    && conversation.context?.platform_data?.kind !== 'agent_direct';
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
    && (eventType === 'turn_started' || eventType === 'message_complete' || eventType === 'session_recovered')
    && typeof eventSessionId === 'string'
    && eventSessionId.length > 0;
}

export function isForeignSessionTimelineEvent(params: {
  eventType: string;
  eventSessionId?: string | null;
  rootSessionId?: string | null;
}): boolean {
  return ROOT_SESSION_TIMELINE_EVENT_TYPES.has(params.eventType)
    && typeof params.rootSessionId === 'string'
    && params.rootSessionId.length > 0
    && typeof params.eventSessionId === 'string'
    && params.eventSessionId.length > 0
    && params.eventSessionId !== params.rootSessionId;
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
