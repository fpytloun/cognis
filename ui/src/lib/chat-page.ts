import type { ChatMode, ChatModeSource, Conversation } from '$lib/types/api';

export interface ConversationRetryScope {
  sessions: boolean;
  history: boolean;
}

export type ConversationStatusFilter = 'active' | 'starred' | 'archived';
export type ConversationAttentionTone = 'default' | 'amber' | 'rose';
export type ChatModeTone = 'default' | 'plan' | 'build';
export type PendingDirectQuestionKind = 'question' | 'auth_challenge';

export interface PendingDirectQuestion {
  notificationId: string;
  stepName?: string;
  question: string;
  questionId?: string;
  options: string[];
  context: string;
  kind?: PendingDirectQuestionKind;
  structured?: boolean;
}

export interface FailedTurnRetryTailItem {
  kind: string;
  role?: string | null;
  content?: string | null;
  text?: string | null;
  partial?: boolean | null;
}

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
  'session_compaction_finished',
  'session_compaction_started',
  'session_compacted',
  'tool_call',
  'tool_progress',
  'tool_output_chunk',
  'tool_result',
  'tool_result_chunk',
]);

const RECOVERABLE_FAILED_TURN_NOTICE_MARKERS = [
  'Your tool results have been saved. Please try sending your message again.',
  'A model error occurred while generating the response.',
  'Turn failed: the model did not produce output',
];

const CONVERSATION_STATUS_FILTERS = new Set<ConversationStatusFilter>(['active', 'starred', 'archived']);

export const CHAT_STORAGE_KEYS = {
  enterToSend: 'cognis-chat-enter-to-send',
  selectedAgent: 'cognis-chat-selected-agent',
  selectedChannel: 'cognis-chat-selected-channel',
  sidebarCollapsed: 'cognis-chat-sidebar-collapsed',
  lastOpenedConversation: 'cognis-chat-last-opened-conversation'
} as const;

const LAST_OPENED_CONVERSATION_STORAGE_PREFIX = `${CHAT_STORAGE_KEYS.lastOpenedConversation}:`;

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

export function managedConversationTurnState(conversation: {
  has_active_turn?: boolean | null;
  managed_agent?: { turn_state?: string | null } | null;
} | null | undefined): string {
  if (conversation?.has_active_turn) return 'running';
  return conversation?.managed_agent?.turn_state ?? 'idle';
}

export function conversationTurnModeTone(
  conversation: {
    has_active_turn?: boolean | null;
    active_turn_chat_mode?: unknown;
  } | null | undefined,
  fallbackMode: unknown,
): ChatModeTone {
  if (conversation?.has_active_turn) {
    const activeTurnMode = normalizeChatModeTone(conversation.active_turn_chat_mode);
    if (activeTurnMode !== 'default') return activeTurnMode;
  }
  return normalizeChatModeTone(fallbackMode);
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

export function conversationHasAttention(conversation: {
  active_session_status?: string | null;
  active_session_completion_reason?: string | null;
  pending_notification_types?: string[] | null;
}): boolean {
  return conversationAttentionTone(conversation) !== 'default';
}

export function conversationShowsAttentionDot(
  conversation: {
    has_unread?: boolean | null;
    active_session_status?: string | null;
    active_session_completion_reason?: string | null;
    pending_notification_types?: string[] | null;
  },
  isActive: boolean,
  inProgress: boolean,
): boolean {
  const unread = Boolean(conversation.has_unread) && !isActive;
  return (unread || conversationHasAttention(conversation)) && !inProgress;
}

export interface ConversationUpdatedRowPatchEvent {
  title?: string;
  has_unread?: boolean;
  has_active_turn?: boolean;
  active_turn_chat_mode?: ChatMode | null;
  active_turn_chat_mode_source?: ChatModeSource | null;
  active_session_status?: string | null;
  active_session_completion_reason?: string | null;
  pending_notification_types?: string[];
  last_read_at?: string | null;
  last_message_at?: string | null;
  updated_at?: string | null;
}

export function conversationUpdatedRowPatch(
  event: ConversationUpdatedRowPatchEvent,
): Partial<Conversation> {
  const patch: Partial<Conversation> = {};
  if (typeof event.title === 'string') patch.title = event.title;
  if (typeof event.has_unread === 'boolean') patch.has_unread = event.has_unread;
  if (typeof event.has_active_turn === 'boolean') {
    patch.has_active_turn = event.has_active_turn;
    if (!event.has_active_turn) {
      patch.active_turn_chat_mode = event.active_turn_chat_mode ?? null;
      patch.active_turn_chat_mode_source = event.active_turn_chat_mode_source ?? null;
    }
  }
  if (typeof event.active_turn_chat_mode === 'string') patch.active_turn_chat_mode = event.active_turn_chat_mode;
  if (typeof event.active_turn_chat_mode_source === 'string') patch.active_turn_chat_mode_source = event.active_turn_chat_mode_source;
  if (typeof event.active_session_status === 'string' || event.active_session_status === null) patch.active_session_status = event.active_session_status;
  if (typeof event.active_session_completion_reason === 'string' || event.active_session_completion_reason === null) patch.active_session_completion_reason = event.active_session_completion_reason;
  if (Array.isArray(event.pending_notification_types)) patch.pending_notification_types = event.pending_notification_types;
  if (typeof event.last_read_at === 'string' || event.last_read_at === null) patch.last_read_at = event.last_read_at;
  if (typeof event.last_message_at === 'string') patch.last_message_at = event.last_message_at;
  if (typeof event.updated_at === 'string') patch.updated_at = event.updated_at;
  return patch;
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

function isRecoverableFailedTurnNotice(item: FailedTurnRetryTailItem): boolean {
  if (item.kind !== 'system_message' && item.kind !== 'notice') return false;
  const text = `${item.text ?? ''}\n${item.content ?? ''}`;
  return RECOVERABLE_FAILED_TURN_NOTICE_MARKERS.some((marker) => text.includes(marker));
}

/**
 * Detects a conversation tail where the latest user turn did not produce a
 * completed assistant message. This covers both persisted model-failure
 * notices and the raw "last item is a user message" state seen after a turn
 * fails before any assistant output is saved.
 */
export function hasRetryableFailedTurnTail(items: FailedTurnRetryTailItem[]): boolean {
  let latestUserIndex = -1;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind === 'message' && item.role === 'user') {
      latestUserIndex = index;
      break;
    }
  }
  if (latestUserIndex < 0) return false;

  const tail = items.slice(latestUserIndex + 1);
  if (tail.length === 0) return true;

  let sawFailureNotice = false;
  for (const item of tail) {
    if (!item) continue;
    if (item.kind === 'message' && item.role === 'assistant' && item.partial !== true) {
      return false;
    }
    if (isRecoverableFailedTurnNotice(item)) {
      sawFailureNotice = true;
    }
  }

  return sawFailureNotice;
}

export function normalizeChatModeTone(value: unknown): ChatModeTone {
  return value === 'plan' || value === 'build' ? value : 'default';
}

export function optimisticConversationTurnPatch(chatMode: unknown): {
  has_unread: false;
  has_active_turn: true;
  active_turn_chat_mode: ChatModeTone;
  active_turn_chat_mode_source: null;
} {
  return {
    has_unread: false,
    has_active_turn: true,
    active_turn_chat_mode: normalizeChatModeTone(chatMode),
    active_turn_chat_mode_source: null,
  };
}

export function lastOpenedConversationStorageKey(agentId: string | null | undefined): string {
  const normalizedAgentId = (agentId ?? '').trim();
  if (!normalizedAgentId) return CHAT_STORAGE_KEYS.lastOpenedConversation;
  return `${LAST_OPENED_CONVERSATION_STORAGE_PREFIX}${encodeURIComponent(normalizedAgentId)}`;
}

export function isLastOpenedConversationStorageKey(key: string): boolean {
  return key === CHAT_STORAGE_KEYS.lastOpenedConversation
    || key.startsWith(LAST_OPENED_CONVERSATION_STORAGE_PREFIX);
}

function directQuestionContext(context: unknown): string {
  if (typeof context === 'string') return context;
  if (context && typeof context === 'object') {
    const record = context as Record<string, unknown>;
    const text = record.context ?? record.note;
    return typeof text === 'string' ? text : '';
  }
  return '';
}

export function pendingDirectQuestionFromAuthChallengeEvent(event: {
  notification_id?: string | null;
  step_name?: string | null;
  message?: string | null;
  label?: string | null;
  metadata?: unknown;
}): PendingDirectQuestion | null {
  if (!event.notification_id) return null;
  const question = typeof event.message === 'string' && event.message.trim().length > 0
    ? event.message.trim()
    : typeof event.label === 'string' && event.label.trim().length > 0
      ? event.label.trim()
      : 'Authentication is required to continue.';
  return {
    notificationId: event.notification_id,
    stepName: event.step_name ?? undefined,
    question,
    questionId: undefined,
    options: [],
    context: directQuestionContext(event.metadata),
    kind: 'auth_challenge',
  };
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
  agent_id?: string | null;
  status?: string | null;
  context?: { type?: string | null; platform_data?: Record<string, unknown> | null } | null;
} | null | undefined): boolean {
  return conversation?.status === 'active'
    && (conversation.context?.type ?? '').toLowerCase() === 'web'
    && conversation.context?.platform_data?.kind !== 'agent_direct';
}

export function shouldRestoreLastOpenedConversation(conversation: {
  agent_id?: string | null;
  status?: string | null;
  context?: { type?: string | null; platform_data?: Record<string, unknown> | null } | null;
} | null | undefined, selectedAgentId: string | null | undefined): boolean {
  return isRestorableChatConversation(conversation)
    && (!selectedAgentId || conversation?.agent_id === selectedAgentId);
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
