import type {
  ChatMode,
  ChatModeSource,
  Conversation,
  ConversationStateEnvelope,
  LastOpenedConversationCandidate,
  QuestionSetQuestion,
  QuestionSetReply,
  SidebarProjection,
} from '$lib/types/api';
import { isAuthChallengeToolCall } from '$lib/chat-v2/selectors';
import type { TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';

export interface ConversationRetryScope {
  sessions: boolean;
  history: boolean;
}

export type ConversationStatusFilter = 'active' | 'starred' | 'archived';
export type ConversationAttentionTone = 'default' | 'amber' | 'rose';
export type ChatModeTone = 'default' | 'plan' | 'build';
export type PendingDirectQuestionKind = 'question' | 'auth_challenge';
export const DEFAULT_INITIAL_TIMELINE_LIMIT = 200;
export const DIRECT_CHAT_INITIAL_SESSION_LIMIT = 20;
export const DIRECT_CHAT_INITIAL_TIMELINE_LIMIT = 80;

export function shouldApplyLegacyLifecycleFrame(chatV2OwnsConversation: boolean): boolean {
  return !chatV2OwnsConversation;
}

export const LEGACY_LIFECYCLE_EVENT_TYPES = [
  'turn_started',
  'queued',
  'turn_settled',
  'message_complete',
  'conversation_updated',
  'reconnected',
  'history_rebased',
  'session_reset',
  'session_compacted',
  'workflow_step_question',
  'auth_challenge',
] as const;

export function applyRuntimeAuthoritySequence(params: {
  chatV2OwnsConversation: boolean;
  canonicalActive: boolean;
  legacyFrames: Array<{ type: string; active: boolean }>;
}): boolean {
  let active = params.canonicalActive;
  for (const frame of params.legacyFrames) {
    if (shouldApplyLegacyLifecycleFrame(params.chatV2OwnsConversation)) active = frame.active;
  }
  return active;
}

const CHAT_V2_OWNED_CONVERSATION_FIELDS = new Set([
  'has_active_turn',
  'active_session_status',
  'active_session_completion_reason',
  'active_turn_chat_mode',
  'active_turn_chat_mode_source',
]);

export function conversationStatePatchForAuthority<T extends Record<string, unknown>>(
  patch: T,
  chatV2OwnsConversation: boolean,
): Partial<T> {
  if (!chatV2OwnsConversation) return patch;
  return Object.fromEntries(
    Object.entries(patch).filter(([key]) => !CHAT_V2_OWNED_CONVERSATION_FIELDS.has(key)),
  ) as Partial<T>;
}

export function conversationStateTurnActivity(params: {
  currentActive: boolean;
  snapshotActive: boolean;
  hasPendingInput: boolean;
  chatV2OwnsConversation: boolean;
}): boolean {
  if (params.chatV2OwnsConversation) return params.currentActive;
  return params.snapshotActive && !params.hasPendingInput;
}

export function resolveTurnActivityAuthority(params: {
  chatV2OwnsConversation: boolean;
  canonicalActive: boolean;
  legacyActive: boolean | null;
}): boolean {
  return params.chatV2OwnsConversation
    ? params.canonicalActive
    : params.legacyActive ?? params.canonicalActive;
}

export interface SidebarProjectionFilter {
  selectedChannels: string[];
  selectedAgentIds: string[];
  selectedConversationStatus: ConversationStatusFilter;
}

export interface PendingDirectQuestion {
  notificationId: string;
  stepName?: string;
  question: string;
  questionId?: string;
  options: string[];
  questions?: QuestionSetQuestion[];
  context: string;
  kind?: PendingDirectQuestionKind;
  structured?: boolean;
}

export interface ConversationPendingSnapshotFlags {
  hasDirectQuestion: boolean;
  hasCredentialRequest: boolean;
  hasEscalation: boolean;
  hasAnyPendingInput: boolean;
}

const DIRECT_QUESTION_NOTIFICATION_TYPES = new Set([
  'step_question',
  'gate',
  'workflow_gate',
  'auth_challenge',
]);
const CREDENTIAL_REQUEST_NOTIFICATION_TYPES = new Set(['credential_request']);

export function conversationPendingSnapshotFlags(
  state: Pick<ConversationStateEnvelope, 'pending'> | null | undefined,
): ConversationPendingSnapshotFlags {
  const pending = state?.pending;
  const notificationTypes = new Set(pending?.notification_types ?? []);
  const pendingInputType = pending?.pending_input?.notification_type ?? null;
  if (pendingInputType) {
    notificationTypes.add(pendingInputType);
  }

  return {
    hasDirectQuestion: Boolean(
      pending?.auth_challenge
      || (pendingInputType && DIRECT_QUESTION_NOTIFICATION_TYPES.has(pendingInputType))
      || [...notificationTypes].some((type) => DIRECT_QUESTION_NOTIFICATION_TYPES.has(type)),
    ),
    hasCredentialRequest: Boolean(
      pending?.credential_request
      || (pendingInputType && CREDENTIAL_REQUEST_NOTIFICATION_TYPES.has(pendingInputType))
      || [...notificationTypes].some((type) => CREDENTIAL_REQUEST_NOTIFICATION_TYPES.has(type)),
    ),
    hasEscalation: Boolean(pending?.escalation || notificationTypes.has('escalation')),
    hasAnyPendingInput: Boolean(
      pending?.pending_input
      || pending?.credential_request
      || pending?.auth_challenge
      || pending?.escalation
      || notificationTypes.size > 0,
    ),
  };
}

export interface ConversationInitialLoadPolicy {
  historyLimit: number;
  sessionOptions?: {
    rootOnly: boolean;
    order: 'asc' | 'desc';
    limit: number;
  };
}

export interface RuntimeStalenessInput {
  turnInProgress: boolean;
  hasActiveTimelineItem: boolean;
  lastRuntimeAt: number;
  now: number;
  staleMs: number;
}

export function shouldRefreshForStaleRuntime(input: RuntimeStalenessInput): boolean {
  if (!input.turnInProgress && !input.hasActiveTimelineItem) return false;
  if (input.lastRuntimeAt <= 0) return false;
  return input.now - input.lastRuntimeAt > input.staleMs;
}

/**
 * Minimum interval between opportunistic conversation-view refreshes. Focus,
 * visibility, pageshow, online, and stale-runtime triggers fire liberally
 * (every window focus, every foregrounding); each refresh does a full snapshot
 * replace plus a scroll restore, so an undebounced storm of them makes the
 * timeline unstable under an actively scrolling user.
 */
export const CONVERSATION_VIEW_REFRESH_MIN_INTERVAL_MS = 5000;
export const SIDEBAR_RESYNC_MIN_INTERVAL_MS = 5000;
export const MISSING_CONVERSATION_RECOVERY_COOLDOWN_MS = 5000;
export const CONVERSATION_SWITCH_TIMEOUT_MS = 35_000;

export interface ChatV2ConversationRealtime {
  acquireChatV2: (scope: TimelineScope, cursor: string) => void;
  updateChatV2Cursor: (scope: TimelineScope, cursor: string) => void;
  releaseChatV2: (scopeKey: string) => void;
}

/**
 * Own the conversation scope for one mounted main chat page. Recovery and
 * reset snapshots refresh the cursor only; they never create another ref.
 */
export class ChatV2ConversationLifecycle {
  private acquiredScopeKey: string | null = null;

  constructor(private readonly realtime: ChatV2ConversationRealtime) {}

  acceptSnapshot(scope: TimelineScope, cursor: string): void {
    if (this.acquiredScopeKey !== null && this.acquiredScopeKey !== scope.key) {
      this.release();
    }
    if (this.acquiredScopeKey === scope.key) {
      this.realtime.updateChatV2Cursor(scope, cursor);
      return;
    }
    this.realtime.acquireChatV2(scope, cursor);
    this.acquiredScopeKey = scope.key;
  }

  release(): void {
    if (this.acquiredScopeKey === null) return;
    const scopeKey = this.acquiredScopeKey;
    this.acquiredScopeKey = null;
    this.realtime.releaseChatV2(scopeKey);
  }

  get scopeKey(): string | null {
    return this.acquiredScopeKey;
  }
}

/**
 * Refresh reasons that must never be debounced: they indicate the client KNOWS
 * it missed frames and the view is materially stale.
 */
const FORCED_VIEW_REFRESH_REASONS = new Set(['websocket-reconnect-gap']);

/**
 * Number of consecutive stale-runtime refreshes at the normal interval before
 * backing off. A leaked non-terminal item (e.g. a tool call stuck in running
 * state) makes the staleness condition permanent — refreshing cannot fix it,
 * and an endless 30s refresh loop keeps disrupting the user's scroll position.
 */
export const STALE_RUNTIME_REFRESH_MAX_ATTEMPTS = 3;

/**
 * Interval between stale-runtime refreshes after the attempt budget is
 * exhausted. Deliberately slow (leaked items cannot be fixed by refreshing)
 * but never zero: if a REAL turn is active with a silently broken WebSocket,
 * the periodic refresh is the only recovery path — it must not stop entirely.
 */
export const STALE_RUNTIME_REFRESH_BACKOFF_MS = 5 * 60_000;

export const CONTROLLER_RECOVERY_MAX_DELAY_MS = 30_000;
export const CONTROLLER_RECOVERY_MAX_ATTEMPTS = 8;

export function nextControllerRecoveryDelayMs(attempt: number): number {
  return Math.min(1_000 * 2 ** Math.max(0, attempt), CONTROLLER_RECOVERY_MAX_DELAY_MS);
}

export function shouldContinueControllerRecovery(attempts: number): boolean {
  return attempts < CONTROLLER_RECOVERY_MAX_ATTEMPTS;
}

/**
 * Decide whether a stale-runtime refresh attempt is due. Normal cadence for
 * the first {@link STALE_RUNTIME_REFRESH_MAX_ATTEMPTS} consecutive attempts,
 * then the slow backoff cadence. The attempt counter is reset by the caller
 * when the active-work condition clears or a genuine runtime signal arrives.
 */
export function shouldAttemptStaleRuntimeRefresh(params: {
  attempts: number;
  lastAttemptAt: number;
  now: number;
  intervalMs: number;
  maxAttempts?: number;
  backoffMs?: number;
}): boolean {
  const maxAttempts = params.maxAttempts ?? STALE_RUNTIME_REFRESH_MAX_ATTEMPTS;
  const backoffMs = params.backoffMs ?? STALE_RUNTIME_REFRESH_BACKOFF_MS;
  const requiredIntervalMs = params.attempts >= maxAttempts ? backoffMs : params.intervalMs;
  return params.now - params.lastAttemptAt >= requiredIntervalMs;
}

export function shouldDebounceConversationViewRefresh(params: {
  reason: string;
  lastRefreshAt: number;
  now: number;
  minIntervalMs?: number;
}): boolean {
  if (FORCED_VIEW_REFRESH_REASONS.has(params.reason)) return false;
  if (params.lastRefreshAt <= 0) return false;
  return params.now - params.lastRefreshAt < (params.minIntervalMs ?? CONVERSATION_VIEW_REFRESH_MIN_INTERVAL_MS);
}

export function shouldDebounceSidebarResync(params: {
  lastSuccessfulSyncAt: number;
  now: number;
  minIntervalMs?: number;
}): boolean {
  if (params.lastSuccessfulSyncAt <= 0) return false;
  return params.now - params.lastSuccessfulSyncAt < (params.minIntervalMs ?? SIDEBAR_RESYNC_MIN_INTERVAL_MS);
}

export function missingConversationPatchNeedsRecovery(patch: {
  has_unread?: boolean | null;
  active_session_status?: string | null;
  active_session_completion_reason?: string | null;
  pending_notification_types?: string[] | null;
}): boolean {
  return Boolean(patch.has_unread)
    || (patch.pending_notification_types?.length ?? 0) > 0
    || conversationHasAttention(patch);
}

export function shouldRecoverMissingConversationRow(params: {
  conversationId: string;
  patch: {
    has_unread?: boolean | null;
    active_session_status?: string | null;
    active_session_completion_reason?: string | null;
    pending_notification_types?: string[] | null;
  };
  lastAttemptByConversation: ReadonlyMap<string, number>;
  now: number;
  cooldownMs?: number;
}): boolean {
  if (!missingConversationPatchNeedsRecovery(params.patch)) return false;
  const lastAttemptAt = params.lastAttemptByConversation.get(params.conversationId) ?? 0;
  if (lastAttemptAt > 0 && params.now - lastAttemptAt < (params.cooldownMs ?? MISSING_CONVERSATION_RECOVERY_COOLDOWN_MS)) {
    return false;
  }
  return true;
}

export function shouldApplyPendingNotificationRefresh(params: {
  requestEpoch: number;
  currentEpoch: number | undefined;
}): boolean {
  return params.currentEpoch === params.requestEpoch;
}

export function isConversationSwitchStale(params: {
  startedAt: number;
  now: number;
  timeoutMs?: number;
}): boolean {
  if (params.startedAt <= 0) return false;
  const timeoutMs = params.timeoutMs ?? CONVERSATION_SWITCH_TIMEOUT_MS;
  return params.now - params.startedAt >= timeoutMs;
}

export function shouldApplyChatV2Recovery(conversationId: string, routeConversationId: string): boolean {
  return Boolean(conversationId) && conversationId === routeConversationId;
}

export function shouldApplyChatSendFailureSideEffects(
  sendConversationId: string,
  routeConversationId: string,
): boolean {
  return Boolean(sendConversationId) && sendConversationId === routeConversationId;
}

const TERMINAL_RETRY_REJECTION_CODES = new Set([
  'retry_turn_not_available',
  'retry_source_not_persisted',
]);

export function shouldClearRecoverableRetry(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const code = 'code' in error ? error.code : undefined;
  return typeof code === 'string' && TERMINAL_RETRY_REJECTION_CODES.has(code);
}

export function settleWithTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  label: string,
  timer: Pick<Window, 'setTimeout' | 'clearTimeout'> = window,
): Promise<PromiseSettledResult<T>> {
  let timeoutId: number | undefined;
  const settled: Promise<PromiseSettledResult<T>> = promise.then(
    (value): PromiseFulfilledResult<T> => ({ status: 'fulfilled', value }),
    (reason): PromiseRejectedResult => ({ status: 'rejected', reason }),
  );
  const timedOut = new Promise<PromiseRejectedResult>((resolve) => {
    timeoutId = timer.setTimeout(() => {
      resolve({
        status: 'rejected',
        reason: new Error(`${label} timed out after ${Math.round(timeoutMs / 1000)} seconds.`),
      });
    }, timeoutMs);
  });
  return Promise.race([settled, timedOut]).finally(() => {
    if (timeoutId !== undefined) timer.clearTimeout(timeoutId);
  });
}

export function isRuntimeSnapshotOlderThanView(
  snapshotServerTime: string | null | undefined,
  viewServerTimeMs: number,
): boolean {
  if (!snapshotServerTime || viewServerTimeMs <= 0) return false;
  const snapshotTimeMs = Date.parse(snapshotServerTime);
  if (!Number.isFinite(snapshotTimeMs) || snapshotTimeMs <= 0) return false;
  return snapshotTimeMs < viewServerTimeMs;
}

export function isTimelinePatchStale(
  patchLastSeq: number | null | undefined,
  currentHighWatermarkSeq: number,
): boolean {
  if (patchLastSeq === null || typeof patchLastSeq !== 'number') return false;
  if (!Number.isFinite(patchLastSeq) || patchLastSeq <= 0 || currentHighWatermarkSeq <= 0) return false;
  return patchLastSeq <= currentHighWatermarkSeq;
}

function timestampValue(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function conversationActivityValue(conversation: Pick<Conversation, 'created_at' | 'last_message_at'>): number {
  return Math.max(
    timestampValue(conversation.last_message_at),
    timestampValue(conversation.created_at)
  );
}

export interface ConversationActivitySection<T extends Pick<Conversation, 'created_at' | 'last_message_at'>> {
  key: string;
  label: string;
  conversations: T[];
}

function localDateKey(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function localDayNumber(date: Date): number {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86_400_000;
}

function conversationActivityDate(conversation: Pick<Conversation, 'created_at' | 'last_message_at'>): Date | null {
  const activity = conversationActivityValue(conversation);
  if (activity <= 0) return null;
  const date = new Date(activity);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function conversationActivitySectionLabel(
  date: Date | null,
  now = new Date(),
  locale?: Intl.LocalesArgument,
): string {
  if (!date) return 'No activity';
  const dayDelta = localDayNumber(now) - localDayNumber(date);
  if (dayDelta === 0) return 'Today';
  if (dayDelta === 1) return 'Yesterday';
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(date);
}

export function groupConversationsByActivity<T extends Pick<Conversation, 'created_at' | 'last_message_at'>>(
  conversations: T[],
  now = new Date(),
  locale?: Intl.LocalesArgument,
): ConversationActivitySection<T>[] {
  const sections: ConversationActivitySection<T>[] = [];
  const sectionByKey = new Map<string, ConversationActivitySection<T>>();

  for (const conversation of conversations) {
    const date = conversationActivityDate(conversation);
    const key = date ? `date:${localDateKey(date)}` : 'no-activity';
    let section = sectionByKey.get(key);
    if (!section) {
      section = {
        key,
        label: conversationActivitySectionLabel(date, now, locale),
        conversations: [],
      };
      sectionByKey.set(key, section);
      sections.push(section);
    }
    section.conversations.push(conversation);
  }

  return sections;
}

function maxTimestampValue<T extends string | null | undefined>(left: T, right: T): T {
  return timestampValue(right) > timestampValue(left) ? right : left;
}

function hasOwn(object: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(object, key);
}

export function hasUnreadFromConversationTimestamps(
  lastMessageAt: string | null | undefined,
  lastReadAt: string | null | undefined,
): boolean {
  const lastMessageValue = timestampValue(lastMessageAt);
  if (lastMessageValue <= 0) return false;
  const lastReadValue = timestampValue(lastReadAt);
  return lastReadValue <= 0 || lastMessageValue > lastReadValue;
}

export function mergeConversationRowPatch<T extends Conversation>(
  existing: T,
  patch: Partial<Conversation>,
): T {
  const hasLastReadAt = hasOwn(patch, 'last_read_at');
  const hasLastMessageAt = hasOwn(patch, 'last_message_at');
  const merged = {
    ...existing,
    ...patch,
  } as T;
  if (hasLastReadAt) {
    merged.last_read_at = maxTimestampValue(existing.last_read_at, patch.last_read_at) ?? null;
  }
  if (hasLastMessageAt) {
    merged.last_message_at = maxTimestampValue(existing.last_message_at, patch.last_message_at) ?? null;
  }
  if (hasLastReadAt || hasLastMessageAt) {
    merged.has_unread = hasUnreadFromConversationTimestamps(merged.last_message_at, merged.last_read_at);
  } else if (typeof patch.has_unread === 'boolean') {
    merged.has_unread = patch.has_unread;
  }
  return merged;
}

export function mergeConversationPreservingActivity(
  existing: Conversation | null | undefined,
  incoming: Conversation,
): Conversation {
  if (!existing) return incoming;
  const incomingIsOlder = timestampValue(incoming.updated_at) < timestampValue(existing.updated_at);
  // A sidebar request can finish after a newer socket event. Preserve the
  // volatile runtime projection from that newer event instead of letting the
  // late REST row re-arm a completed turn or restore a cleared attention dot.
  const runtimeProjection = incomingIsOlder
    ? {
        has_active_turn: existing.has_active_turn,
        active_turn_chat_mode: existing.active_turn_chat_mode,
        active_turn_chat_mode_source: existing.active_turn_chat_mode_source,
        active_session_status: existing.active_session_status,
        active_session_completion_reason: existing.active_session_completion_reason,
        pending_notification_types: existing.pending_notification_types,
      }
    : {};
  return mergeConversationRowPatch(existing, {
    ...incoming,
    ...runtimeProjection,
    last_message_at: maxTimestampValue(existing.last_message_at, incoming.last_message_at),
    updated_at: maxTimestampValue(existing.updated_at, incoming.updated_at),
  });
}

/**
 * A pending-input event is replayable across reconnects. Rebuilding the same
 * form must retain its in-memory answers, page, collapsed state, and submit
 * state; only a different notification represents a new form.
 */
export function shouldResetPendingDirectQuestionForm(
  currentNotificationId: string | null | undefined,
  nextNotificationId: string | null | undefined,
): boolean {
  return currentNotificationId !== nextNotificationId;
}

export function mergeSidebarConversationRows(
  existing: Conversation[],
  incoming: Conversation[],
  { reset = false }: { reset?: boolean } = {},
): Conversation[] {
  const existingById = new Map(existing.map((conversation) => [conversation.conversation_id, conversation]));
  const next = reset
    ? []
    : existing.filter((conversation) => !isAgentDirectConversationSummary(conversation));
  const indexById = new Map(next.map((conversation, index) => [conversation.conversation_id, index]));
  for (const conversation of incoming) {
    if (isAgentDirectConversationSummary(conversation)) {
      continue;
    }
    const row = mergeConversationPreservingActivity(existingById.get(conversation.conversation_id), conversation);
    const index = indexById.get(conversation.conversation_id);
    if (index === undefined) {
      indexById.set(conversation.conversation_id, next.length);
      next.push(row);
    } else {
      next[index] = mergeConversationPreservingActivity(next[index], row);
    }
  }
  return [...next].sort((left, right) => {
    const activityDelta = conversationActivityValue(right) - conversationActivityValue(left);
    if (activityDelta !== 0) return activityDelta;
    const createdDelta = timestampValue(right.created_at) - timestampValue(left.created_at);
    if (createdDelta !== 0) return createdDelta;
    return left.conversation_id.localeCompare(right.conversation_id);
  });
}

export function removeSidebarConversationRow(
  existing: Conversation[],
  conversationId: string,
): Conversation[] {
  return existing.filter((conversation) => conversation.conversation_id !== conversationId);
}

export function cloneSidebarProjection(projection: SidebarProjection): SidebarProjection {
  return {
    agents: projection.agents.map((agent) => ({ ...agent })),
    agent_direct_chats: projection.agent_direct_chats.map((item) => ({
      agent: { ...item.agent },
      conversation: { ...item.conversation },
    })),
    conversations: {
      items: projection.conversations.items.map((conversation) => ({ ...conversation })),
      cursor: projection.conversations.cursor,
      has_more: projection.conversations.has_more,
    },
    context_types: [...projection.context_types],
  };
}

export function rememberSidebarProjectionSnapshot(
  cache: Map<string, SidebarProjection>,
  key: string,
  projection: SidebarProjection,
  limit: number,
): void {
  cache.delete(key);
  cache.set(key, cloneSidebarProjection(projection));
  while (cache.size > limit) {
    const oldestKey = cache.keys().next().value;
    if (!oldestKey) break;
    cache.delete(oldestKey);
  }
}

export function isAgentDirectConversationSummary(conversation: Conversation | null | undefined): boolean {
  return conversation?.context?.type === 'web'
    && conversation.context.platform_data?.kind === 'agent_direct';
}

export function conversationInitialLoadPolicy(
  conversation: Conversation | null | undefined,
): ConversationInitialLoadPolicy {
  if (!isAgentDirectConversationSummary(conversation)) {
    return { historyLimit: DEFAULT_INITIAL_TIMELINE_LIMIT };
  }
  return {
    historyLimit: DIRECT_CHAT_INITIAL_TIMELINE_LIMIT,
    sessionOptions: {
      rootOnly: true,
      order: 'desc',
      limit: DIRECT_CHAT_INITIAL_SESSION_LIMIT,
    },
  };
}

export function conversationMatchesSidebarProjectionFilter(
  conversation: Conversation,
  filter: SidebarProjectionFilter,
): boolean {
  const selectedAgentIds = new Set(filter.selectedAgentIds);
  if (selectedAgentIds.size > 0 && !selectedAgentIds.has(conversation.agent_id)) {
    return false;
  }

  const contextType = conversation.context?.type?.toLowerCase() ?? 'unknown';
  const selectedChannels = new Set(filter.selectedChannels.map((channel) => channel.toLowerCase()));
  if (isAgentDirectConversationSummary(conversation)) {
    const channelMatches = selectedChannels.size === 0 || selectedChannels.has('web');
    return channelMatches && conversation.status === 'active';
  }

  if (selectedChannels.size > 0 && !selectedChannels.has(contextType)) {
    return false;
  }

  if (filter.selectedConversationStatus === 'active') return conversation.status === 'active';
  if (filter.selectedConversationStatus === 'archived') return conversation.status === 'archived';
  return conversation.status === 'active' && Boolean(conversation.starred_at);
}

export interface FailedTurnRetryTailItem {
  kind: string;
  role?: string | null;
  content?: string | null;
  text?: string | null;
  noticeKind?: string | null;
  noticeScope?: string | null;
  partial?: boolean | null;
  turnId?: string | null;
  orderKey?: string | null;
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
  'escalation',
  'escalation_resolved',
  'message_complete',
  'session_compaction_finished',
  'session_compaction_started',
  'session_compacted',
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
/**
 * Distance from the bottom of the rendered rows (px) within which scrolling
 * down triggers downward window re-expansion, so unmounted newer rows remount
 * before the user hits the very bottom. Larger than the live-tail threshold so
 * expansion happens ahead of the edge and stays smooth.
 */
export const CHAT_TIMELINE_EXPAND_DOWN_THRESHOLD_PX = 400;

export interface ChatScrollState {
  distanceFromBottom: number;
  userScrolledUp: boolean;
}

export interface ChatAutoScrollState {
  shouldScroll: boolean;
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
  // Positive delta means the user (or a programmatic scroll) moved downward.
  const userMovedDown = params.currentScrollTop > params.lastScrollTop + scrollDeltaThresholdPx;

  if (params.userScrollIntentUp || (userMovedUp && params.distanceFromBottom > 0)) {
    return {
      distanceFromBottom: params.distanceFromBottom,
      userScrolledUp: true,
    };
  }

  // Only re-attach live-tail when the user actively scrolled DOWN to the
  // bottom threshold. Clearing userScrolledUp on position alone (without a
  // downward movement) is the root cause of the scroll-jump regression on
  // tall messages: a reflow-induced scrollTop clamp, or a delayed scroll
  // event from a programmatic scrollTop write, can land near the bottom
  // without any user gesture — falsely re-pinning the tail and causing the
  // viewport to jump back to the bottom the next time the ResizeObserver
  // fires. Requiring an explicit downward movement means only a real
  // user-initiated scroll-to-bottom re-attaches the tail.
  if (isNearScrollBottom(params.distanceFromBottom, bottomThresholdPx) && userMovedDown) {
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

export function nextChatAutoScrollState(params: {
  force: boolean;
  userScrolledUp: boolean;
  autoScrollPending?: boolean;
  // distanceFromBottom and positionGate are retained in the signature for
  // backwards compatibility with existing call sites and tests, but are no
  // longer used in the decision. The position-gate branch was the source of
  // the scroll-jump regression: it inferred userScrolledUp=true from a racy
  // distance measurement taken while streaming content was still growing,
  // dismounting auto-tail for messages taller than the viewport. The correct
  // rule is: only genuine user gestures (handleTimelineScroll, wheel/touch/key
  // intent) may set userScrolledUp=true. Auto-scroll is purely idempotent —
  // "if pinned, re-pin; else do nothing" — and never infers un-pinning.
  distanceFromBottom?: number;
  positionGate?: boolean;
  bottomThresholdPx?: number;
}): ChatAutoScrollState {
  if (params.force) {
    return {
      shouldScroll: true,
      userScrolledUp: false,
    };
  }

  if (params.userScrolledUp) {
    return {
      shouldScroll: false,
      userScrolledUp: true,
    };
  }

  if (params.autoScrollPending) {
    return {
      shouldScroll: false,
      userScrolledUp: false,
    };
  }

  return {
    shouldScroll: true,
    userScrolledUp: false,
  };
}

export function shouldPreserveLiveTailOnResize(params: {
  tailPinned: boolean;
  autoScrollPending: boolean;
}): boolean {
  // tailPinned is always kept in sync with !userScrolledUp (single source of
  // truth). This function is equivalent to: !userScrolledUp || autoScrollPending.
  return params.tailPinned || params.autoScrollPending;
}

export function chatScrollDimensionsChanged(
  previous: { scrollHeight: number; clientHeight: number },
  current: { scrollHeight: number; clientHeight: number },
): boolean {
  return previous.scrollHeight !== current.scrollHeight || previous.clientHeight !== current.clientHeight;
}

/**
 * Maximum drift (px) between a captured scroll position and the current one
 * before a scroll-position restore is considered stale and skipped. A user
 * actively scrolling during an async fetch will move well beyond this; layout
 * noise (scrollbar rounding, sub-pixel reflow) stays well under it.
 */
export const CHAT_SCROLL_RESTORE_DRIFT_THRESHOLD_PX = 40;

/**
 * Decide whether a scroll-position restore captured earlier is still safe to
 * apply. Restoring an absolute scrollTop captured before async work (network
 * fetch, snapshot replace) while the user kept scrolling yanks them back to a
 * stale position — the "jumps back while scrolling" bug. The restore is only
 * applied when the viewport has not moved materially since capture.
 */
export function shouldApplyScrollRestore(params: {
  capturedScrollTop: number;
  currentScrollTop: number;
  driftThresholdPx?: number;
}): boolean {
  const threshold = params.driftThresholdPx ?? CHAT_SCROLL_RESTORE_DRIFT_THRESHOLD_PX;
  return Math.abs(params.currentScrollTop - params.capturedScrollTop) <= threshold;
}

/**
 * Timeline render window bounds over the renderable-items array.
 *
 * `start` is the first rendered index. `end` is exclusive; `null` means "up to
 * the current tail" (no upper bound) so freshly appended items render live.
 * A finite `end` bounds the mounted DOM for very long conversations, but MUST
 * be re-expanded when the user scrolls back toward the tail — otherwise newer
 * rows stay unmounted and the user has to jump to bottom to see them.
 */
export interface TimelineWindow {
  start: number;
  end: number | null;
}

export const TIMELINE_WINDOW_TARGET_ROWS = 100;
export const TIMELINE_WINDOW_PAGE_ROWS = 50;
export const TIMELINE_WINDOW_MAX_ROWS = TIMELINE_WINDOW_TARGET_ROWS + TIMELINE_WINDOW_PAGE_ROWS;
export const TIMELINE_VIEWPORT_FILL_SLACK_PX = 96;
export const TIMELINE_VIEWPORT_FILL_MAX_ATTEMPTS = 8;

export interface TimelineViewportFillParams {
  visibleStartIndex: number;
  hasOlderMessages: boolean;
  loadingOlderMessages: boolean;
  scrollHeight: number;
  clientHeight: number;
  slackPx?: number;
}

/**
 * Decide whether the chat should opportunistically page older rows into the
 * mounted window because the current DOM does not fill the viewport.
 *
 * The render window is counted in raw timeline items, but Chat v2 can collapse
 * large tool-call/activity groups into a handful of DOM rows. In that case the
 * tail window may contain enough raw items while still being visually too short
 * for the user to scroll up and trigger manual older loading.
 */
export function shouldAutoLoadOlderForViewport(params: TimelineViewportFillParams): boolean {
  if (params.loadingOlderMessages) return false;
  if (params.visibleStartIndex <= 0 && !params.hasOlderMessages) return false;
  if (params.clientHeight <= 0) return false;
  const slackPx = params.slackPx ?? TIMELINE_VIEWPORT_FILL_SLACK_PX;
  return params.scrollHeight <= params.clientHeight + slackPx;
}

/** Resolve the effective (exclusive) end index for a window over `total` rows. */
export function timelineWindowEnd(window: TimelineWindow, total: number): number {
  return Math.min(total, window.end ?? total);
}

/** Number of currently rendered rows. */
export function timelineWindowSize(window: TimelineWindow, total: number): number {
  return Math.max(0, timelineWindowEnd(window, total) - Math.min(window.start, timelineWindowEnd(window, total)));
}

/** Whether rows newer than the window's end are hidden (unmounted). */
export function timelineWindowHasHiddenTail(window: TimelineWindow, total: number): boolean {
  return timelineWindowEnd(window, total) < total;
}

/**
 * Expand the window upward by one page (scrolling toward older history that is
 * already in memory). The end is kept but bounded so the mounted span never
 * exceeds the max; when the tail was unbounded it becomes finite so the DOM
 * stays capped while paging up.
 */
export function expandWindowUp(
  window: TimelineWindow,
  total: number,
  pageRows = TIMELINE_WINDOW_PAGE_ROWS,
  maxRows = TIMELINE_WINDOW_MAX_ROWS,
): TimelineWindow {
  const currentEnd = timelineWindowEnd(window, total);
  const start = Math.max(0, window.start - pageRows);
  const end = Math.min(total, currentEnd, start + maxRows);
  return { start, end: end >= total ? null : end };
}

/**
 * Expand older rows into a visually under-filled live tail without detaching
 * the user from the newest rows. This is intentionally less aggressive than
 * explicit scroll-up paging: while the window is live (`end === null`), keep it
 * live even if the raw mounted span exceeds the steady-state cap. The auto-fill
 * caller is separately bounded by attempt count, and once the user explicitly
 * scrolls up the normal capped windowing rules apply again.
 */
export function expandWindowUpPreservingLiveTail(
  window: TimelineWindow,
  total: number,
  pageRows = TIMELINE_WINDOW_PAGE_ROWS,
  maxRows = TIMELINE_WINDOW_MAX_ROWS,
): TimelineWindow {
  if (window.end === null) {
    return clampWindow({ start: Math.max(0, window.start - pageRows), end: null }, total);
  }
  return expandWindowUp(window, total, pageRows, maxRows);
}

/**
 * Expand the window downward by one page (scrolling back toward the tail). The
 * end grows first; once the tail is reachable the end becomes `null` (live).
 * The start is advanced only if needed to keep the mounted span within max.
 * This is the missing inverse of {@link expandWindowUp} — without it, hidden
 * newer rows never remount on scroll-down.
 */
export function expandWindowDown(
  window: TimelineWindow,
  total: number,
  pageRows = TIMELINE_WINDOW_PAGE_ROWS,
  maxRows = TIMELINE_WINDOW_MAX_ROWS,
): TimelineWindow {
  const currentEnd = timelineWindowEnd(window, total);
  const nextEnd = Math.min(total, currentEnd + pageRows);
  const start = Math.max(0, window.start, nextEnd - maxRows);
  return { start, end: nextEnd >= total ? null : nextEnd };
}

/**
 * Recompute the window after older items are prepended (REST backfill).
 *
 * Two invariants, in priority order:
 *  1. The newly prepended older page MUST be rendered (that is what the user
 *     asked for by scrolling to the top) — so `start` goes to 0.
 *  2. The previously rendered newer rows MUST stay mounted (no tail cut, so
 *     the content the user was reading does not vanish) — so `end` shifts down
 *     by `prependedCount`.
 * Both can be satisfied at once by widening the mounted span; the window is a
 * DOM-perf bound, and briefly exceeding the steady-state target right after an
 * explicit user backfill is fine. A hard ceiling (`maxSpanRows`) still caps the
 * DOM for pathological prepends; when exceeded, the fetched older page wins
 * (start stays 0) and the far tail is dropped — it is reachable again via
 * downward re-expansion on scroll-down.
 */
export function windowAfterPrepend(
  window: TimelineWindow,
  prependedCount: number,
  total: number,
  maxSpanRows = TIMELINE_WINDOW_MAX_ROWS * 3,
): TimelineWindow {
  if (prependedCount <= 0) {
    return clampWindow(window, total);
  }
  const priorEnd = window.end === null ? total - prependedCount : window.end;
  const shiftedEnd = Math.min(total, priorEnd + prependedCount);
  // start=0 shows the fetched page; end keeps the prior newer rows mounted,
  // capped at the hard span ceiling (fetched page wins; far tail, if dropped,
  // is recoverable via downward re-expansion).
  const end = Math.min(total, Math.max(prependedCount, Math.min(shiftedEnd, maxSpanRows)));
  return { start: 0, end: end >= total ? null : end };
}

/**
 * Recompute the window after an automatic viewport-fill backfill. When the user
 * has not explicitly scrolled up and the tail is live, keep the tail live and
 * temporarily allow a wider mounted raw span so collapsed tool groups can fill
 * the viewport without hiding the newest rows. Explicit/manual backfills keep
 * using the normal capped prepend behavior.
 */
export function windowAfterViewportFillBackfill(
  window: TimelineWindow,
  prependedCount: number,
  total: number,
  preserveLiveTail: boolean,
): TimelineWindow {
  if (preserveLiveTail && window.end === null && prependedCount > 0) {
    return clampWindow({ start: 0, end: null }, total);
  }
  return windowAfterPrepend(window, prependedCount, total);
}

/** Clamp a window's bounds to `total`, collapsing a full-tail end to `null`. */
export function clampWindow(window: TimelineWindow, total: number): TimelineWindow {
  const end = timelineWindowEnd(window, total);
  const start = Math.min(Math.max(0, window.start), end);
  return { start, end: end >= total ? null : end };
}

/**
 * Freeze the window's tail while the user is scrolled up and new items arrive,
 * anchored to the COUNT of rows that were newer than the frozen end at freeze
 * time rather than an absolute index. Passing the number of rows appended
 * since the freeze (`appendedSinceFreeze`) shifts the frozen end so appends do
 * not remount the tail, while a prepend (which also changes indices) is
 * absorbed by callers via {@link windowAfterPrepend} before this runs.
 */
export function freezeTailWindow(
  window: TimelineWindow,
  total: number,
  previousTotal: number,
): TimelineWindow {
  if (window.end !== null) {
    return clampWindow(window, total);
  }
  if (previousTotal <= 0 || total <= previousTotal) {
    return clampWindow(window, total);
  }
  // The tail was unbounded and rows were appended: freeze the end at the prior
  // total so the newly appended rows stay hidden (user is reading older
  // history). Prepends are handled before this by windowAfterPrepend, so an
  // index freeze here is safe against appends only.
  return clampWindow({ start: window.start, end: previousTotal }, total);
}

/**
 * Element-anchored scroll restore. Given the viewport-relative top offset of a
 * reference row before a DOM mutation and its offset after, return the delta to
 * add to `scrollTop` so the reference row stays visually fixed.
 *
 * This is immune to changes that add rows ABOVE and remove rows BELOW in the
 * same commit (the DOM-window prepend+tail-cut case), unlike a whole-document
 * `scrollHeight` delta which conflates the two.
 */
export function anchoredScrollTop(params: {
  currentScrollTop: number;
  anchorTopBefore: number;
  anchorTopAfter: number;
}): number {
  const shift = params.anchorTopAfter - params.anchorTopBefore;
  return params.currentScrollTop + shift;
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
  if (item.noticeKind === 'model_error') {
    return item.noticeScope == null || item.noticeScope === 'failed_turn';
  }
  const text = `${item.text ?? ''}\n${item.content ?? ''}`;
  return RECOVERABLE_FAILED_TURN_NOTICE_MARKERS.some((marker) => text.includes(marker));
}

/**
 * Detects a conversation tail where the latest user turn did not produce a
 * completed assistant message.
 *
 * **Turn-based, not positional.** We identify the latest user message's
 * ``turnId`` and then scan the full item list for that turn's outcome.  This
 * is order-independent: the user message can be anywhere in the array (e.g.
 * temporarily below a streaming assistant due to orderKey ordering) and the
 * result is still correct.
 *
 * Returns true only when:
 *   - There is a latest user message with a turnId, AND
 *   - No completed (non-partial) assistant message shares that turnId, AND
 *   - At least one recoverable-failure notice is present for that turn.
 *
 * The "no assistant at all" case (turn failed before any output) is NOT
 * treated as retryable here — that state is transient while the turn is
 * still in progress and would produce false positives during streaming.
 */
export function hasRetryableFailedTurnTail(items: FailedTurnRetryTailItem[]): boolean {
  // Find the latest user message and its turnId.
  let latestUserTurnId: string | null | undefined = null;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind === 'message' && item.role === 'user') {
      latestUserTurnId = (item as { turnId?: string | null }).turnId ?? null;
      break;
    }
  }
  if (latestUserTurnId == null) return false;

  // Find the latest user message's orderKey so we can determine whether a
  // failure notice appeared before or after it in the canonical timeline order.
  let latestUserOrderKey: string | null = null;
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const item = items[index];
    if (item?.kind === 'message' && item.role === 'user') {
      latestUserOrderKey = (item as { orderKey?: string | null }).orderKey ?? null;
      break;
    }
  }

  // Scan all items for this turn's outcome.
  let sawCompletedAssistant = false;
  let sawFailureNotice = false;
  for (const item of items) {
    if (!item) continue;
    if (item.kind === 'message' && item.role === 'assistant' && item.partial !== true) {
      const itemTurnId = (item as { turnId?: string | null }).turnId ?? null;
      if (itemTurnId === latestUserTurnId) {
        sawCompletedAssistant = true;
      }
    }
    if (isRecoverableFailedTurnNotice(item)) {
      // Only count a failure notice if it sorts AFTER the latest user message
      // (i.e., it was produced by the current turn, not a previous one).
      // We compare orderKeys: if the notice has a higher orderKey than the
      // user message it is from the current turn; if lower it is stale.
      // When either key is absent we conservatively count the notice.
      const noticeOrderKey = (item as { orderKey?: string | null }).orderKey ?? null;
      const isAfterUser =
        latestUserOrderKey === null
        || noticeOrderKey === null
        || noticeOrderKey > latestUserOrderKey;
      if (isAfterUser) {
        sawFailureNotice = true;
      }
    }
  }

  return !sawCompletedAssistant && sawFailureNotice;
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

export function lastOpenedConversationEntry(
  conversation: Pick<Conversation, 'conversation_id' | 'agent_id' | 'agent_profile_id' | 'context'>,
  openedAt = new Date(),
): LastOpenedConversationCandidate {
  return {
    conversation_id: conversation.conversation_id,
    opened_at: openedAt.toISOString(),
    agent_id: conversation.agent_id,
    agent_profile_id: conversation.agent_profile_id ?? null,
    context_type: conversation.context?.type ?? null,
  };
}

export function serializeLastOpenedConversationEntry(
  entry: LastOpenedConversationCandidate,
): string {
  return JSON.stringify(entry);
}

export function parseLastOpenedConversationEntry(
  raw: string | null | undefined,
): LastOpenedConversationCandidate | null {
  const trimmed = raw?.trim();
  if (!trimmed) return null;
  try {
    const value = JSON.parse(trimmed) as unknown;
    if (!value || typeof value !== 'object') return null;
    const record = value as Record<string, unknown>;
    const conversationId = record.conversation_id;
    if (typeof conversationId !== 'string' || !conversationId.trim()) return null;
    const openedAt = record.opened_at;
    const agentId = record.agent_id;
    const agentProfileId = record.agent_profile_id;
    const contextType = record.context_type;
    return {
      conversation_id: conversationId.trim(),
      opened_at: typeof openedAt === 'string' && Number.isFinite(Date.parse(openedAt)) ? openedAt : null,
      agent_id: typeof agentId === 'string' && agentId.trim() ? agentId : null,
      agent_profile_id: typeof agentProfileId === 'string' && agentProfileId.trim() ? agentProfileId : null,
      context_type: typeof contextType === 'string' && contextType.trim() ? contextType : null,
    };
  } catch {
    // Legacy format stored the conversation id directly.
    return { conversation_id: trimmed, opened_at: null };
  }
}

export function dedupeLastOpenedConversationEntries(
  entries: LastOpenedConversationCandidate[],
): LastOpenedConversationCandidate[] {
  const seen = new Set<string>();
  const unique: LastOpenedConversationCandidate[] = [];
  for (const entry of entries) {
    const conversationId = entry.conversation_id.trim();
    if (!conversationId || seen.has(conversationId)) continue;
    seen.add(conversationId);
    unique.push({ ...entry, conversation_id: conversationId });
  }
  return unique;
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

export function questionSetReplyText(reply: QuestionSetReply): string {
  const parts: string[] = [];
  for (const answer of reply.answers) {
    parts.push(...answer.selected_option_ids);
    const customAnswer = answer.custom_answer?.trim();
    if (customAnswer) parts.push(customAnswer);
  }
  return parts.join('\n').trim();
}

export function pendingInputRequestKind(params: {
  pendingStepTool?: ToolCallTimelineItem | null;
  pendingDirectKind?: PendingDirectQuestionKind | null;
}): PendingDirectQuestionKind {
  if (params.pendingStepTool) {
    return isAuthChallengeToolCall(params.pendingStepTool) ? 'auth_challenge' : 'question';
  }
  return params.pendingDirectKind === 'auth_challenge' ? 'auth_challenge' : 'question';
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
  eventPreviousSessionId?: string | null;
  rootSessionId?: string | null;
}): boolean {
  if (
    params.eventType === 'session_compacted'
    && typeof params.rootSessionId === 'string'
    && params.rootSessionId.length > 0
    && params.eventPreviousSessionId === params.rootSessionId
  ) {
    return false;
  }
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
