<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount, tick, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { fade } from 'svelte/transition';
import ArrowLeft from 'lucide-svelte/icons/arrow-left';
import ArrowUp from 'lucide-svelte/icons/arrow-up';
import Paperclip from 'lucide-svelte/icons/paperclip';
import Square from 'lucide-svelte/icons/square';
import Check from 'lucide-svelte/icons/check';
import ChevronDown from 'lucide-svelte/icons/chevron-down';
import ChevronUp from 'lucide-svelte/icons/chevron-up';
import ChevronsLeft from 'lucide-svelte/icons/chevrons-left';
import ChevronsRight from 'lucide-svelte/icons/chevrons-right';
import Copy from 'lucide-svelte/icons/copy';
import ExternalLink from 'lucide-svelte/icons/external-link';
import Headphones from 'lucide-svelte/icons/headphones';
import Info from 'lucide-svelte/icons/info';
import ListPlus from 'lucide-svelte/icons/list-plus';
import Menu from 'lucide-svelte/icons/menu';
import RefreshCw from 'lucide-svelte/icons/refresh-cw';
import Search from 'lucide-svelte/icons/search';
import Star from 'lucide-svelte/icons/star';
import X from 'lucide-svelte/icons/x';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AgentProfilePopover from '$lib/components/AgentProfilePopover.svelte';
  import ChatSearchBar from '$lib/components/ChatSearchBar.svelte';
  import CredentialRequestForm from '$lib/components/CredentialRequestForm.svelte';
  import ComposerAttachments from '$lib/components/ComposerAttachments.svelte';
  import ConversationMode from '$lib/components/ConversationMode.svelte';
  import MicRecorderButton from '$lib/components/MicRecorderButton.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import NewChatModal from '$lib/components/NewChatModal.svelte';
  import ChatV2TimelineList from '$lib/components/chat-v2/ChatV2TimelineList.svelte';
  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';
  import SessionDetailsContent from '$lib/components/session/SessionDetailsContent.svelte';
  import SessionDetailsPanel from '$lib/components/session/SessionDetailsPanel.svelte';
  import TimelineTodoDrawer from '$lib/components/timeline/TimelineTodoDrawer.svelte';
  import TimelineViewport from '$lib/components/timeline/TimelineViewport.svelte';
  import TodoProgressPopover from '$lib/components/TodoProgressPopover.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Popover from '$lib/components/ui/Popover.svelte';
  import PullToRefresh from '$lib/components/ui/PullToRefresh.svelte';
  import { api, asApiError } from '$lib/api/client';
  import {
    buildConversationUrl,
    CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
    CHAT_USER_SCROLL_DELTA_THRESHOLD_PX,
    CHAT_TIMELINE_EXPAND_DOWN_THRESHOLD_PX,
    conversationPendingSnapshotFlags,
    conversationMatchesSidebarProjectionFilter,
    conversationAttentionDotClass,
    conversationAttentionLabel,
    conversationAttentionOrbitClass,
    conversationAttentionTone,
    conversationActivityValue,
    conversationShowsAttentionDot,
    conversationStatusFilterForConversation,
    conversationTurnModeTone,
    conversationUpdatedRowPatch,
    distanceFromScrollBottom,
    getConversationRetryScope,
    groupConversationsByActivity,
    lastOpenedConversationEntry,
    managedConversationTurnState,
    mergeConversationPreservingActivity,
    mergeConversationRowPatch,
    mergeSidebarConversationRows,
    removeSidebarConversationRow,
    getNextHistoryAfterSeq,
    isNearScrollBottom,
    isMissingSessionError,
    isCurrentConversationLoad,
    isForeignSessionTimelineEvent,
    isLastOpenedConversationStorageKey,
    isAgentDirectConversationSummary,
    conversationInitialLoadPolicy,
    lastOpenedConversationStorageKey,
    nextChatScrollState,
    normalizeChatModeTone,
    optimisticConversationTurnPatch,
    pendingDirectQuestionFromAuthChallengeEvent,
    parseConversationStatusFilter,
    parseLastOpenedConversationEntry,
    pendingInputRequestKind,
    conversationStatePatchForAuthority,
    conversationStateTurnActivity,
    shouldApplyLegacyLifecycleFrame,
    questionSetReplyText,
    rememberSidebarProjectionSnapshot,
    serializeLastOpenedConversationEntry,
    isPreSessionChatConversation,
    pendingNotificationTypesFromNotifications,
    setConversationStatusSearchParam,
    chatScrollDimensionsChanged,
    shouldApplyScrollRestore,
    anchoredScrollTop,
    timelineWindowEnd,
    timelineWindowSize,
    timelineWindowHasHiddenTail,
    shouldAutoLoadOlderForViewport,
    expandWindowUp,
    expandWindowUpPreservingLiveTail,
    expandWindowDown,
    windowAfterPrepend,
    windowAfterViewportFillBackfill,
    clampWindow,
    freezeTailWindow,
    TIMELINE_WINDOW_TARGET_ROWS as TIMELINE_WINDOW_TARGET_ROWS_HELPER,
    TIMELINE_WINDOW_PAGE_ROWS as TIMELINE_WINDOW_PAGE_ROWS_HELPER,
    TIMELINE_WINDOW_MAX_ROWS as TIMELINE_WINDOW_MAX_ROWS_HELPER,
    TIMELINE_VIEWPORT_FILL_MAX_ATTEMPTS,
    type TimelineWindow,
    shouldAttemptStaleRuntimeRefresh,
    shouldApplyPendingNotificationRefresh,
    shouldDebounceConversationViewRefresh,
    shouldDebounceSidebarResync,
    shouldRecoverMissingConversationRow,
    shouldResetPendingDirectQuestionForm,
    isConversationSwitchStale,
    shouldApplyChatSendFailureSideEffects,
    shouldApplyChatV2Recovery,
    shouldClearRecoverableRetry,
    settleWithTimeout,
    shouldReconcileAfterReconnect,
    nextControllerRecoveryDelayMs,
    shouldContinueControllerRecovery,
    shouldPreserveLiveTailOnResize,
    shouldRefreshForStaleRuntime,
    isRuntimeSnapshotOlderThanView,
    shouldAdoptConversationSessionId,
    shouldSuppressPreSessionSocketError,
    nextConversationLoadId,
    nextPollDelayMs,
    ChatV2ConversationLifecycle,
    type ChatModeTone,
    type ConversationStatusFilter,
    type PendingDirectQuestion,
    CHAT_STORAGE_KEYS,
    CONVERSATION_SWITCH_TIMEOUT_MS,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { edgeSwipe } from '$lib/actions/edgeSwipe';
  import { scrollPersist } from '$lib/actions/scrollPersist';
  import { auth } from '$lib/stores/auth';
  import { confirmAction } from '$lib/stores/confirm';
  import { mobileNavOpen as mobileNavOpenStore, requestOpenMobileNav } from '$lib/stores/mobileNav';
  import { registerOverlay } from '$lib/stores/overlays';
  import { canAttemptPwaAuxiliaryWindow } from '$lib/stores/pwa';
  import { onTabReset } from '$lib/stores/tabReset';
  import { addToast } from '$lib/stores/toasts';
  import { loadUserPreferences, userPreferences } from '$lib/stores/userPreferences';
  import { haptic } from '$lib/haptics';
  import {
    clearQuestionDraft,
    readOptimisticUserMessageDrafts,
    readQuestionDraft,
    removeOptimisticUserMessageDraft,
    saveOptimisticUserMessageDraft,
    writeQuestionDraft,
    type QuestionDraftAnswers,
  } from '$lib/interactive-drafts';
  import { onCancelActiveTurnRequest, onChatComposerFocusRequest } from '$lib/shortcuts';
  import { pastedFileFingerprint, pastedFilesFromClipboardData, readPastedFilesFromNavigator } from '$lib/clipboard';
  import {
    enableWebPush,
    hasDismissedWebPushPrompt,
    hasEnabledWebPush,
    isWebPushSupported,
    needsIosHomeScreenInstall,
    notifyIfHidden,
    permissionState,
    reconcileWebPushSubscription,
    setWebPushPromptDismissed
  } from '$lib/notifications';
  import { buildLinkedServiceUrl, openUrlInNewTab } from '$lib/config';
  import { workspaceHealth } from '$lib/system';
  import {
    cleanSearchSnippet,
    findLocalChatMatches,
    findVisibleServerSearchTarget,
    mergeSearchResultsByTarget,
    serverSearchFallbackTargetId,
    type ChatSearchResult,
    type LocalChatMatch
  } from '$lib/chat-search';
  import {
    applySlashSuggestion,
    isSystemSlashCommand,
    localSlashCommandSuggestions,
    normalizeSlashCommandInput,
    parseChatModeDirectiveInput,
    slashParameterSuggestionCommand
  } from '$lib/slash-commands';
  import { chatV2Api } from '$lib/chat-v2/api';
  import { conversationTimelineScope } from '$lib/chat-v2/types';
  import { MemoryChatV2Outbox, createIndexedDbChatV2Outbox, type OutboxEntry } from '$lib/chat-v2/outbox';
  import { ChatV2Store } from '$lib/chat-v2/store.svelte';
  import { isRenderableTimelineItem, selectLatestTodoState, selectPendingInputToolCall } from '$lib/chat-v2/selectors';
  import type { ChatV2ClientState } from '$lib/chat-v2/sync-engine';
  import type { ChatRealtimeFrame, ChatSnapshot, TimelineItem as ChatV2TimelineItem } from '$lib/chat-v2/types';
  import { incompleteTodos, visibleTodos as activeVisibleTodos } from '$lib/todos';
  import {
    localPerformanceMetrics,
    mergeLatestPerformance,
    responsivenessBadge
  } from '$lib/generation-performance';
  import type { ActiveThinkingSnapshot, Agent, AgentDirectChat, AttachmentRef, CognisWebSocketEvent, ContextUsage, Conversation, ConversationSearchMatch, ConversationStateEnvelope, ConversationTodoItem, Escalation, GenerationPerformanceSnapshot, MessageEvent, Notification, QueuedMessage, QuestionSetAnswer, QuestionSetQuestion, QuestionSetReply, Session, SidebarProjection, SlashCommandSuggestion } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';
  import { isNonFatalWebSocketBackpressureError } from '$lib/ws/errors';

  let initializing = $state(true);
  let initialLoadTimedOut = $state(false);
  let switchingConversationId = $state<string | null>(null);
  let switchingConversation = $derived(switchingConversationId !== null);
  let initialConversationResolved = $state(false);
  let error = $state('');
  let historyError = $state('');
  let sessionsError = $state('');
  let conversations = $state<Conversation[]>([]);
  let availableChannelTypes = $state<string[]>([]);
  let conversationCursor: string | null = null;
  let conversationsHasMore = $state(false);
  let conversationListLoading = $state(false);
  let conversationListRefreshing = $state(false);
  let conversationListLoadCount = 0;
  let conversationSearch = $state('');
  let conversationSearchResults = $state<ConversationSearchMatch[]>([]);
  let conversationSearchLoading = $state(false);
  let conversationSearchSubmitted = $state('');
  let conversationSearchError = $state('');
  let historySectionNow = $state(new Date());
  let expandedSearchSessionIds = $state<string[]>([]);
  let searchEnabled = $state(true);
  let isWindowMode = $derived(page.url.searchParams.get('window') === '1');
  let canOpenAuxiliaryWindow = $derived(canAttemptPwaAuxiliaryWindow());
  let chatSearchOpen = $state(false);
  let chatSearchQuery = $state('');
  let chatSearchLoading = $state(false);
  let chatSearchResults = $state<ChatSearchResult[]>([]);
  let chatSearchSelectedIndex = $state(0);
  let seededChatSearchRan = $state(false);
  let seededChatSearchSession = $state<string | null>(null);
  let seededChatSearchRef = $state<string | null>(null);
  let lastSeededSearchKey = '';
  let lastChatSearchConversationId = '';
  let agents = $state<Agent[]>([]);
  let agentDirectChats = $state<AgentDirectChat[]>([]);
  let sidebarProjectionLoaded = $state(false);
  let sidebarProjectionFailed = $state(false);
  let currentConversation = $state<Conversation | null>(null);
  let sessions = $state<Session[]>([]);
  type ChatTodo = ConversationTodoItem & { priority: string };
  let conversationTodoSnapshots = $state<Record<string, ChatTodo[]>>({});
  let conversationSubloadsLoading = $state(false);
  let cachedConversationRefreshing = $state(false);
  let composer = $state('');
  let composerElement = $state<HTMLTextAreaElement | null>(null);
  let composerAttachments = $state<AttachmentRef[]>([]);
  let managedInstructionOpen = $state(false);
  let managedInstruction = $state('');
  let managedActionBusy = $state<string | null>(null);
  let managedActionError = $state('');
  let recentPastedFileFingerprints = new Map<string, number>();
  let conversationModeOpen = $state(false);
  let voiceTranscribing = $state(false);

  // Composer drafts persist across app restarts (per conversation) via
  // localStorage. This protects PWA users when the browser evicts or
  // reloads the web chat while a message is still being drafted.
  const DRAFT_PREFIX = 'cognis-chat-draft:';
  let currentDraftKey: string | null = null;
  let draftSaveTimer: number | null = null;

  function readDraft(key: string): string {
    if (typeof localStorage === 'undefined') return '';
    try {
      return localStorage.getItem(key) ?? '';
    } catch {
      return '';
    }
  }

  function writeDraft(key: string, value: string): void {
    if (typeof localStorage === 'undefined') return;
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    } catch {
      // non-fatal
    }
  }

  function clearDraft(key: string | null): void {
    if (!key) return;
    writeDraft(key, '');
  }
  let showDropZone = $state(false);
  let dragCounter = 0;
  const PASTE_DUPLICATE_SUPPRESSION_MS = 2000;
  const CONTEXT_DONUT_RADIUS = 15.5;
  const CONTEXT_DONUT_CIRCUMFERENCE = 2 * Math.PI * CONTEXT_DONUT_RADIUS;
  let selectedAgentIds = $state<string[]>([]);
  let selectedConversationStatus = $state<ConversationStatusFilter>(parseConversationStatusFilter(page.url.searchParams.get('status')));
  let starringConversationId = $state<string | null>(null);
  let archivingConversation = $state(false);
  let deletingConversation = $state(false);
  let mobileListOpen = $state(false);
  let mobileListOverlayCleanup: (() => void) | null = null;
  let conversationFiltersOpen = $state(true);
  let agentFilterDropdownOpen = $state(false);
  let channelFilterDropdownOpen = $state(false);
  // Unified flag for the expanded header info panel. Replaces the older
  // pair of `sessionInfoOpen` (desktop popover) + `mobileHeaderDetailsOpen`
  // (mobile-only panel) with one state so the Info button has a single,
  // predictable effect regardless of viewport size.
  let headerInfoOpen = $state(false);
  let headerInfoMode = $state<'full' | 'context'>('full');
  // Default to iMessage-style: Enter inserts a newline and the user taps
  // the send button (or presses Cmd/Ctrl+Enter) to submit. Users who
  // previously opted into Enter-to-send keep their choice via localStorage.
  let enterToSend = $state(false);
  let queuedCount = $state(0);
  let queuedMessages = $state<QueuedMessage[]>([]);
  let queueBusyId = $state<string | null>(null);
  let queueExpandedIds = $state<string[]>([]);
  let queueEditingId = $state<string | null>(null);
  let queueEditContent = $state('');
  let queueEditError = $state('');
  const chatV2Store = new ChatV2Store();
  const chatV2Lifecycle = new ChatV2ConversationLifecycle(wsClient);
  const chatV2Outbox = typeof indexedDB === 'undefined'
    ? new MemoryChatV2Outbox()
    : createIndexedDbChatV2Outbox();
  let chatV2OutboxReady = false;
  let chatV2OutboxDraining = false;

  const TIMELINE_WINDOW_TARGET_ROWS = TIMELINE_WINDOW_TARGET_ROWS_HELPER;
  const TIMELINE_WINDOW_PAGE_ROWS = TIMELINE_WINDOW_PAGE_ROWS_HELPER;
  const TIMELINE_WINDOW_MAX_ROWS = TIMELINE_WINDOW_MAX_ROWS_HELPER;
  // Single window object over renderable rows. `start` is the first rendered
  // index; `end` null means "render to the tail" (live). Kept in sync with the
  // pure helpers in chat-page.ts so the logic is unit-testable.
  let timelineWindow = $state<TimelineWindow>({ start: 0, end: null });
  let visibleStartIndex = $derived(timelineWindow.start);
  let visibleEndIndex = $derived(timelineWindow.end);
  let timelineViewportFillScheduled = false;
  let timelineViewportFillInFlight = false;
  let lastRenderableVisibleCount = 0;
  let activeConversationId = '';
  let routeConversationId = $derived(page.params.conversationId ?? '');
  let activeSessionLastSeq = 0;
  let conversationViewServerTimeMs = 0;
  let lastConversationViewRefreshAt = 0;
  let lastSidebarSyncAt = 0;
  let lastSidebarSyncTimestamp: string | null = null;
  let pendingNotificationEpoch = 0;
  const pendingNotificationEpochByConversation = new Map<string, number>();
  const missingConversationRecoveryAt = new Map<string, number>();
  let lastRuntimeSnapshotAt = 0;
  let lastStaleRuntimeRefreshAttemptAt = 0;
  let staleRuntimeRefreshAttempts = 0;
  let viewRefreshInFlight: Promise<void> | null = null;
  let staleRuntimeGuardTimer: number | null = null;
  const STALE_RUNTIME_REFRESH_MS = 30000;
  const CONVERSATION_SUBLOAD_TIMEOUT_MS = 35_000;
  let olderMessagesCursor = $state<string | null>(null);
  let hasOlderMessages = $state(false);
  const CONVERSATION_VIEW_CACHE_LIMIT = 8;
  const SIDEBAR_PROJECTION_CACHE_LIMIT = 8;
  const sidebarProjectionCache = new Map<string, SidebarProjection>();
  const escalationTimeoutSeconds = 300;
  let escalations = $state<Escalation[]>([]);
  let escalationBusyCallId = $state<string | null>(null);
  let escalationResolutionPending = $state<Escalation | null>(null);
  let escalationError = $state('');
  let controllerRecoveryPending = $state(false);
  let controllerRecoveryTimer: number | null = null;
  let controllerRecoveryAttempts = $state(0);
  let controllerRecoveryConversationId: string | null = null;
  let escalationCountdownTimer: number | null = null;
  let notificationRefreshTimer: number | null = null;
  let pushPromptDismissed = $state(hasDismissedWebPushPrompt());
  let pushSubscriptionKnownEnabled = $state(hasEnabledWebPush());
  let pushPromptBusy = $state(false);
  let pushPromptError = $state('');
  let pushDeliveryError = $state('');
  let awaitingAssistantStart = $state(false);
  let turnInProgress = $state(false);
  // Track the turn_id of the most recently settled/cancelled turn.
  let lastSettledTurnId = $state<string | null>(null);
  // Track the turn_id of the currently running turn (set on turn_started).
  let currentActiveTurnId = $state<string | null>(null);
  let activeTurnChatMode = $state<ChatModeTone>('default');
  let lastSubmittedMessage = '';
  let lastRecoverableMessage = $state('');
  let lastRecoverableTurnId = $state<string | null>(null);
  let suppressedRecoverableTurnId = $state<string | null>(null);
  let showNewChatModal = $state(false);
  let newChatAgentId = $state('');
  let newChatAgentProfileId = $state('');
  let newChatCreating = $state(false);
  let newChatError = $state('');
  let editingTitle = $state(false);
  let editTitleValue = $state('');
  let titleSuggestionLoading = $state(false);
  let ignoreNextTitleBlur = $state(false);
  let sessionIdCopied = $state(false);
  let showAgentProfile = $state(false);
  let subSessionPanelOpen = $state(false);
  let subSessionClosing = $state(false);
  let subSessionId = $state('');
  let subSessionUserScrolledUp = $state(false);
  let timelineEl = $state<HTMLDivElement | null>(null);
  let timelineContentEl = $state<HTMLDivElement | null>(null);
  let userScrolledUp = $state(false);
  let tailPinned = $state(false);
  let loadingOlderMessages = $state(false);
  let programmaticScroll = false;
  // The scrollTop value written by the most recent programmatic scroll. Used
  // to suppress the async scroll event that the browser delivers after a
  // programmatic scrollTop write — that event can arrive after programmaticScroll
  // has already been cleared (it is dispatched on the next animation frame, not
  // synchronously), causing handleTimelineScroll to treat the programmatic
  // position as a user gesture and falsely re-pin the tail.
  let programmaticScrollTarget: number | null = null;
  let bottomScrollPending = false;
  let pendingBottomScrollForce = false;
  let userScrollIntentUp = false;
  let lastTimelineTouchY: number | null = null;
  let lastTimelineScrollTop = $state(0);
  let footerChromeEl = $state<HTMLDivElement | null>(null);
  let selectedChannels = $state<string[]>([]);
  let chatSidebarCollapsed = $state(false);
  const chatV2CanonicalRecoveryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const chatV2CanonicalRecoveryInFlight = new Set<string>();
  // Set when a recovery is requested while one is already in flight. The
  // in-flight recovery re-runs once on completion instead of being silently
  // dropped. Without this, a debounced mid-turn sync (whose server-side event
  // read predates the assistant-message flush) swallows the immediate settle
  // recovery, and the just-streamed final message stays missing until reload.
  const chatV2CanonicalRecoveryRerun = new Set<string>();
  const CHAT_V2_CANONICAL_RECOVERY_DEBOUNCE_MS = 1_000;

  function applyChatV2Snapshot(snapshot: ChatSnapshot): void {
    chatV2Store.replaceFromSnapshot(snapshot);
    conversationViewServerTimeMs = Date.parse(snapshot.server_time) || Date.now();
    lastConversationViewRefreshAt = Date.now();
    lastRuntimeSnapshotAt = Date.now();
    applyContextUsageSnapshot(chatV2Store.snapshot.runtime?.context_usage);
    applyLastGenerationSnapshot(chatV2Store.snapshot.runtime?.last_generation);
    // Refresh the older-history cursor from the snapshot when we do not already
    // hold one. Without this, a snapshot recovery (reconnect, outbox drain,
    // cancel) left olderMessagesCursor stale/empty and subsequent scroll-up
    // backfills became no-ops. A deeper cursor from prior backfill is kept.
    if (olderMessagesCursor === null) {
      olderMessagesCursor = snapshot.timeline.before_cursor ?? null;
      hasOlderMessages = Boolean(snapshot.timeline.has_more_before && olderMessagesCursor);
    }
    applyChatV2StateToTimeline();
    chatV2Lifecycle.acceptSnapshot(
      conversationTimelineScope(snapshot.conversation.conversation_id),
      snapshot.cursor,
    );
  }

  function chatV2OwnsActiveConversation(conversationId: string | null = conversationIdFromRoute()): boolean {
    return Boolean(
      conversationId
      && chatV2Store.snapshot.cursor
      && chatV2Store.snapshot.conversationId === conversationId
    );
  }

  function chatV2RecoveryMatchesRoute(conversationId: string): boolean {
    return shouldApplyChatV2Recovery(conversationId, conversationIdFromRoute());
  }

  async function applyChatV2Frame(frame: ChatRealtimeFrame): Promise<void> {
    const previousStatus = chatV2Store.snapshot.syncStatus;
    const result = chatV2Store.applyRealtime(frame);
    if (result.outcome === 'cursor_mismatch' || result.outcome === 'reset_required') {
      await recoverChatV2Canonical(frame.conversation_id);
      return;
    }
    if (chatV2Store.snapshot.syncStatus === 'gapped' && previousStatus !== 'gapped') {
      await recoverChatV2Canonical(frame.conversation_id);
      return;
    }
    // A frame that actually advanced state is proof the stream is healthy.
    // Bump the runtime freshness marker so the 30s stale-runtime guard does not
    // fire a full refreshConversationView mid-stream (which would replace and
    // re-sort the live timeline) for a turn driven purely by chat_v2 frames.
    // Only 'applied' frames count: 'duplicate' frames carry no new state, so
    // counting them could mask a genuinely stalled stream that keeps receiving
    // duplicate/heartbeat frames while content stops advancing.
    if (result.outcome === 'applied') {
      lastRuntimeSnapshotAt = Date.now();
      // A genuine live frame proves the WebSocket works: re-arm the
      // stale-runtime refresh budget.
      staleRuntimeRefreshAttempts = 0;
      applyContextUsageSnapshot(chatV2Store.snapshot.runtime?.context_usage);
      applyLastGenerationSnapshot(chatV2Store.snapshot.runtime?.last_generation);
    }
    if (frame.ops.length === 0 && frame.runtime?.has_active_turn === true) {
      applyChatV2RuntimeToTimeline();
    } else {
      applyChatV2StateToTimeline();
    }
    if (chatV2Store.snapshot.conversationId === frame.conversation_id && chatV2Store.snapshot.cursor) {
      wsClient.updateChatV2Cursor(frame.conversation_id, chatV2Store.snapshot.cursor);
    }
  }

  async function recoverChatV2Canonical(conversationId: string): Promise<void> {
    if (!chatV2RecoveryMatchesRoute(conversationId)) return;
    if (chatV2CanonicalRecoveryInFlight.has(conversationId)) {
      // A recovery is already running. Mark a re-run so the in-flight call
      // fetches once more after it finishes — the current in-flight sync may
      // have started before the events we now need were flushed.
      chatV2CanonicalRecoveryRerun.add(conversationId);
      return;
    }
    chatV2CanonicalRecoveryInFlight.add(conversationId);
    try {
      const cursor = chatV2Store.snapshot.conversationId === conversationId
        ? chatV2Store.snapshot.cursor
        : null;
      if (!cursor) {
        await recoverChatV2Snapshot(conversationId);
        return;
      }
      const response = await chatV2Api.sync(conversationId, cursor);
      if (!chatV2RecoveryMatchesRoute(conversationId)) return;
      const result = chatV2Store.applySync(response);
      if (response.reset_required || result.outcome === 'reset_required' || result.outcome === 'cursor_mismatch') {
        await recoverChatV2Snapshot(conversationId);
        return;
      }
      applyContextUsageSnapshot(chatV2Store.snapshot.runtime?.context_usage);
      applyLastGenerationSnapshot(chatV2Store.snapshot.runtime?.last_generation);
      applyChatV2StateToTimeline();
      if (chatV2Store.snapshot.cursor) {
        wsClient.updateChatV2Cursor(conversationId, chatV2Store.snapshot.cursor);
      }
    } catch (error) {
      if (!chatV2RecoveryMatchesRoute(conversationId)) return;
      const apiError = asApiError(error);
      if (apiError.status === 503 || apiError.code === 'event_store_unavailable') {
        historyError = apiError.message;
        markControllerUnavailable(error);
        return;
      }
      await recoverChatV2Snapshot(conversationId);
    } finally {
      chatV2CanonicalRecoveryInFlight.delete(conversationId);
      if (chatV2CanonicalRecoveryRerun.delete(conversationId)) {
        // A recovery was requested while this one ran. Run it again so the
        // freshly flushed canonical events (e.g. the settle assistant message)
        // are pulled. Guard against unbounded loops: this only re-runs once per
        // in-flight completion, and each re-run advances the cursor watermark.
        if (chatV2OwnsActiveConversation(conversationId)) {
          void recoverChatV2Canonical(conversationId);
        }
      }
    }
  }

  async function recoverChatV2Snapshot(conversationId: string): Promise<void> {
    if (!chatV2RecoveryMatchesRoute(conversationId)) return;
    try {
      const snapshot = await chatV2Api.snapshot(conversationId);
      if (!chatV2RecoveryMatchesRoute(conversationId)) return;
      applyChatV2Snapshot(snapshot);
    } catch (error) {
      const failureMessage = asApiError(error).message;
      if (conversationIdFromRoute() !== conversationId) {
        reportError(error);
        return;
      }
      historyError = failureMessage;
      if (markControllerUnavailable(error)) {
        return;
      }
      if (chatV2Store.snapshot.conversationId === conversationId) {
        if (chatV2Store.visibleItems.length === 0) {
          chatV2Store.reset();
          applyChatV2StateToTimeline();
          wsClient.clearChatV2Cursor(conversationId);
          olderMessagesCursor = null;
          hasOlderMessages = false;
          queuedMessages = [];
          queuedCount = 0;
          awaitingAssistantStart = false;
          turnInProgress = false;
          currentActiveTurnId = null;
        }
      }
      reportError(error);
      addToast(failureMessage, 'error');
    }
  }

  function cancelActiveTurnWithChatV2(): void {
    if (!currentConversation || !turnInProgress) return;
    const cancelConversationId = currentConversation.conversation_id;
    void chatV2Api.cancelTurn(cancelConversationId, {
      client_txn_id: crypto.randomUUID()
    }).then(() => recoverChatV2Snapshot(cancelConversationId)).catch((caughtError) => {
      addToast(asApiError(caughtError).message, 'error');
    });
  }

  function scheduleChatV2CanonicalRecovery(
    conversationId: string,
    { immediate = false }: { immediate?: boolean } = {},
  ): void {
    if (!chatV2OwnsActiveConversation(conversationId)) return;
    // Terminal turn boundaries (message_complete, turn_settled, workflow
    // terminal states) settle the runtime overlay: the server clears volatile
    // items, so the client must pull canonical state right away rather than
    // after the debounce, otherwise the just-streamed content briefly
    // disappears until the debounced sync lands. Running an immediate recovery
    // reuses the single, cursor-correct REST /sync path (no parallel
    // server-side ops/cursor logic to keep in sync).
    if (immediate) {
      const pending = chatV2CanonicalRecoveryTimers.get(conversationId);
      if (pending) {
        clearTimeout(pending);
        chatV2CanonicalRecoveryTimers.delete(conversationId);
      }
      void recoverChatV2Canonical(conversationId);
      return;
    }
    if (chatV2CanonicalRecoveryTimers.has(conversationId)) return;
    const timer = setTimeout(() => {
      chatV2CanonicalRecoveryTimers.delete(conversationId);
      if (chatV2OwnsActiveConversation(conversationId)) {
        void recoverChatV2Canonical(conversationId);
      }
    }, CHAT_V2_CANONICAL_RECOVERY_DEBOUNCE_MS);
    chatV2CanonicalRecoveryTimers.set(conversationId, timer);
  }

  async function drainChatV2Outbox(conversationId: string): Promise<void> {
    if (chatV2OutboxDraining) return;
    chatV2OutboxDraining = true;
    let sentAny = false;
    try {
      const pendingEntries = (await chatV2Outbox.list())
        .filter((entry) => entry.conversation_id === conversationId && entry.status !== 'acked')
        .sort((a, b) => a.created_at.localeCompare(b.created_at));
      for (const entry of pendingEntries) {
        try {
          await chatV2Outbox.update(entry.client_txn_id, {
            status: 'sending',
            updated_at: new Date().toISOString(),
            last_error: undefined
          });
          await chatV2Api.sendMessage(entry.conversation_id, entry.client_txn_id, {
            content: entry.content,
            attachments: entry.attachments,
            client_message_id: entry.client_message_id,
            chat_mode: entry.chat_mode
          });
          await chatV2Outbox.update(entry.client_txn_id, {
            status: 'acked',
            updated_at: new Date().toISOString(),
            last_error: undefined
          });
          await chatV2Outbox.delete(entry.client_txn_id);
          sentAny = true;
        } catch (caughtError) {
          await chatV2Outbox.update(entry.client_txn_id, {
            status: 'failed',
            updated_at: new Date().toISOString(),
            last_error: asApiError(caughtError).message
          }).catch(() => undefined);
        }
      }
    } catch (caughtError) {
      reportError(caughtError);
    } finally {
      chatV2OutboxDraining = false;
    }
    if (sentAny) {
      await recoverChatV2Snapshot(conversationId);
    }
  }

  // Synchronize page-level state (queue, turn flags) from the Chat v2 store.
  // The rendered timeline reads `chatV2Store.visibleItems` reactively, so no
  // imperative timeline mutation is needed here. Scroll position is owned by
  // the Chat v2 viewport (anchor/follow), not by replacement side effects.
  function applyChatV2StateToTimeline(): void {
    const state = chatV2Store.snapshot;
    queuedMessages = (state.queue?.messages ?? []) as QueuedMessage[];
    queuedCount = state.queue?.queued_count ?? queuedMessages.length;
    turnInProgress = state.runtime?.has_active_turn === true;
    awaitingAssistantStart = false;
    currentActiveTurnId = state.runtime?.active_turn?.turn_id ?? null;
    if (!turnInProgress) currentActiveTurnId = null;
    shrinkTailWindowIfPinned();
    lastRenderableVisibleCount = renderableVisibleItems.length;
  }

  function applyChatV2RuntimeToTimeline(): void {
    // Runtime overlay items are part of `chatV2Store.visibleItems`; the native
    // renderer picks them up reactively. Only page-level flags are synced here.
    applyChatV2StateToTimeline();
  }

  let localCommandResultOrdinal = 0;

  function appendChatV2LocalSystemMessage(text: string, idHint: string, noticeId?: string | null): void {
    const content = text.trim();
    const targetConversationId = currentConversation?.conversation_id ?? conversationIdFromRoute();
    if (!content || !chatV2OwnsActiveConversation(targetConversationId)) return;
    localCommandResultOrdinal += 1;
    const idSafeHint = idHint.replace(/[^a-zA-Z0-9_.:-]/g, '_').slice(0, 80) || 'result';
    chatV2Store.addLocalSystemMessage({
      id: noticeId ? `system:${noticeId}` : `local-command:${idSafeHint}:${Date.now()}:${localCommandResultOrdinal}`,
      content,
      noticeId
    });
    applyChatV2StateToTimeline();
    saveCurrentConversationView();
    scrollToBottom(true);
  }

  function commandQueuedMessage(event: Extract<CognisWebSocketEvent, { type: 'queued' }>): string {
    const lines = [event.reason?.trim() || 'Working on that in the background.'];
    if (event.task_id) lines.push(`Task: ${event.task_id}`);
    return lines.join('\n');
  }

  function sessionCompactedMessage(event: Extract<CognisWebSocketEvent, { type: 'session_compacted' }>): string {
    const lines = [event.message?.trim() || 'Conversation history compacted.'];
    lines.push(`New session: ${event.session_id}`);
    if (event.previous_session_id) lines.push(`Previous session: ${event.previous_session_id}`);
    if (event.summary_preview) lines.push('', event.summary_preview);
    return lines.join('\n');
  }

  async function applyCommandResponse(
    response: import('$lib/chat-v2/types').CommandV2Response
  ): Promise<void> {
    const data = response.data;
    if (response.result_type === 'error') {
      addToast(response.text || 'Command failed', 'error', 4000);
      return;
    }
    if (response.result_type === 'conversation_created') {
      const createdConversationId = typeof data.conversation_id === 'string' ? data.conversation_id : '';
      if (createdConversationId) {
        await goto(`/chat/${createdConversationId}`);
        return;
      }
    }
    if (
      response.result_type === 'session_compacted'
      || response.result_type === 'session_reset'
      || response.result_type === 'history_rebased'
    ) {
      const sessionId = typeof data.session_id === 'string' ? data.session_id : '';
      if (sessionId) syncConversationActiveSession(sessionId);
      awaitingAssistantStart = false;
      turnInProgress = false;
      clearConversationTurnState(response.conversation_id);
      scheduleChatV2CanonicalRecovery(response.conversation_id, { immediate: true });
    }
    if (currentConversation && typeof data.resolved_agent_profile_id === 'string') {
      patchConversationInList(currentConversation.conversation_id, {
        agent_profile_id: data.resolved_agent_profile_id
      });
    }
    if (currentConversation && typeof data.executor_id === 'string') {
      patchConversationInList(currentConversation.conversation_id, {
        active_executor_id: data.executor_id,
        active_executor_source: 'user'
      });
    }
    appendChatV2LocalSystemMessage(
      response.text,
      `rest:${response.result_type}:${response.client_txn_id}`
    );
  }

  async function executeCommandWithRetry(conversationId: string, content: string) {
    const clientTxnId = crypto.randomUUID();
    try {
      return await chatV2Api.executeCommand(conversationId, clientTxnId, content);
    } catch {
      return chatV2Api.executeCommand(conversationId, clientTxnId, content);
    }
  }

  interface SessionInfoData {
    intaris_session_id: string;
    intention: string | null;
    summary: string | null;
    status: string;
    total_calls: number;
    approved_count: number;
    denied_count: number;
    escalated_count: number;
    context_usage?: ContextUsage | null;
    last_generation?: GenerationPerformanceSnapshot | null;
  }
  let sessionInfo = $state<SessionInfoData | null>(null);
  let sessionInfoLoading = $state(false);
  let sessionInfoRequestId = 0;
  let sessionNarrativeExpanded = $state(false);

  interface ConversationViewCacheEntry {
    conversation: Conversation;
    sessions: Session[];
    chatV2State: ChatV2ClientState;
    queuedCount: number;
    queuedMessages: QueuedMessage[];
    contextUsage: ContextUsage | null;
    sessionInfo: SessionInfoData | null;
    visibleStartIndex: number;
    scrollTop: number;
    userScrolledUp: boolean;
    turnInProgress: boolean;
    awaitingAssistantStart: boolean;
    activeSessionLastSeq: number;
    olderMessagesCursor: string | null;
    hasOlderMessages: boolean;
    activeTurnChatMode: ChatModeTone;
    fetchedAt: number;
  }

  const conversationViewCache = new Map<string, ConversationViewCacheEntry>();
  let conversationViewSaveTimer: number | null = null;
  const RETRYABLE_FAILED_TURN_NOTICE_MARKERS = [
    'Your tool results have been saved. Please try sending your message again.',
    'A model error occurred while generating the response.',
    'Turn failed: the model did not produce output',
  ];

  function isChatV2MessageStreaming(item: ChatV2TimelineItem): boolean {
    return item.kind === 'message'
      && item.role === 'assistant'
      && item.stable === false
      && (item.status === 'running' || item.partial === true);
  }

  function isChatV2ThinkingStreaming(item: ChatV2TimelineItem): boolean {
    return item.kind === 'thinking'
      && (
        item.status === 'running'
        || item.blocks.some((block) => (block.status ?? item.status) === 'running')
      );
  }

  function isChatV2ToolActive(item: ChatV2TimelineItem): boolean {
    return item.kind === 'tool_call' && !['complete', 'failed', 'cancelled', 'compacted', 'skipped'].includes(item.status ?? '');
  }

  function isChatV2DelegationActive(item: ChatV2TimelineItem): boolean {
    return item.kind === 'delegation' && ['pending', 'running', 'waiting'].includes(item.status ?? '');
  }

  function isInlineLiveChatV2Item(item: ChatV2TimelineItem): boolean {
    return isChatV2MessageStreaming(item) || isChatV2ThinkingStreaming(item) || isChatV2ToolActive(item);
  }

  function isRecoverableFailedTurnChatV2Notice(item: ChatV2TimelineItem): boolean {
    if (item.kind === 'error') {
      return item.recoverable === true;
    }
    if (item.kind === 'notice') {
      const text = `${item.title ?? ''}\n${item.message ?? ''}`;
      return RETRYABLE_FAILED_TURN_NOTICE_MARKERS.some((marker) => text.includes(marker));
    }
    if (item.kind === 'message' && item.role === 'system') {
      if (item.notice_kind === 'model_error') {
        return item.notice_scope == null || item.notice_scope === 'failed_turn';
      }
      return RETRYABLE_FAILED_TURN_NOTICE_MARKERS.some((marker) => item.content.includes(marker));
    }
    return false;
  }

  function retryableFailedTurnIdFromTailChatV2(items: ChatV2TimelineItem[]): string | null {
    let latestUserTurnId: string | null | undefined = null;
    let latestUserOrderKey: string | null = null;
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      if (item?.kind === 'message' && item.role === 'user') {
        latestUserTurnId = item.turn_id ?? null;
        latestUserOrderKey = item.sort_key ?? null;
        break;
      }
    }
    if (latestUserTurnId == null && latestUserOrderKey == null) return null;

    let sawCompletedAssistant = false;
    let sawFailureNotice = false;
    for (const item of items) {
      if (
        item.kind === 'message'
        && item.role === 'assistant'
        && item.partial !== true
        && (
          (latestUserTurnId != null && item.turn_id === latestUserTurnId)
          || (latestUserTurnId == null && latestUserOrderKey != null && (item.sort_key ?? '') > latestUserOrderKey)
        )
      ) {
        sawCompletedAssistant = true;
      }
      if (isRecoverableFailedTurnChatV2Notice(item)) {
        const noticeOrderKey = item.sort_key ?? null;
        if (latestUserOrderKey === null || noticeOrderKey === null || noticeOrderKey > latestUserOrderKey) {
          sawFailureNotice = true;
        }
      }
    }
    return !sawCompletedAssistant && sawFailureNotice ? (latestUserTurnId ?? null) : null;
  }

  function findPendingStepRequestInputCallFromChatV2(): ReturnType<typeof selectPendingInputToolCall> {
    return selectPendingInputToolCall(renderableVisibleItems);
  }

  function nextSessionInfoRequestId(): number {
    sessionInfoRequestId += 1;
    return sessionInfoRequestId;
  }

  function invalidateSessionInfo(): void {
    nextSessionInfoRequestId();
    sessionInfo = null;
    sessionInfoLoading = false;
    sessionNarrativeExpanded = false;
  }

  function isStaleSessionInfoLoad(requestId: number, conversationId: string, sessionId: string): boolean {
    return (
      requestId !== sessionInfoRequestId ||
      currentConversation?.conversation_id !== conversationId ||
      currentConversation?.active_session_id !== sessionId
    );
  }

  function formatTokenCount(value: number | null | undefined): string {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 'n/a';
    return value.toLocaleString();
  }

  function formatPercent(value: number | null | undefined): string {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 'n/a';
    return `${Math.round(value)}%`;
  }

  function clampPercent(value: number | null | undefined): number {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 0;
    return Math.min(100, Math.max(0, value));
  }

  function contextWindowUsagePercentage(usage: ContextUsage): number {
    return usage.max_context_tokens > 0 ? (usage.prompt_tokens / usage.max_context_tokens) * 100 : usage.percentage;
  }

  function contextUsageColor(usage: ContextUsage | null): string {
    if (!usage) return 'text-slate-400';
    const contextWindowPercentage = contextWindowUsagePercentage(usage);
    if (contextWindowPercentage > 95) return 'text-rose-300';
    if (contextWindowPercentage > 85) return 'text-orange-300';
    if (contextWindowPercentage > 65) return 'text-amber-300';
    return 'text-emerald-300';
  }

  function contextUsageStroke(usage: ContextUsage | null): string {
    if (!usage) return '#64748b';
    const contextWindowPercentage = contextWindowUsagePercentage(usage);
    if (contextWindowPercentage > 95) return '#fb7185';
    if (contextWindowPercentage > 85) return '#fb923c';
    if (contextWindowPercentage > 65) return '#fcd34d';
    return '#34d399';
  }

  function contextDonutDashOffset(value: number | null | undefined): number {
    const percentage = clampPercent(value);
    return CONTEXT_DONUT_CIRCUMFERENCE * (1 - percentage / 100);
  }

  function contextUsageTooltip(usage: ContextUsage | null): string {
    if (!usage) return 'Context usage is not available yet.';
    const policy = usage.projection_policy;
    const lines = [
      `Prompt usage: ${formatTokenCount(usage.prompt_tokens)} / ${formatTokenCount(usage.max_context_tokens)} tokens (${formatPercent(contextWindowUsagePercentage(usage))})`,
      usage.agent_id ? `Agent: ${usage.agent_id}` : null,
      contextUsageProfileLabel(usage) ? `Agent profile: ${contextUsageProfileLabel(usage)}` : null,
      `Model: ${usage.model}`,
      usage.reasoning_effort ? `Reasoning effort: ${usage.reasoning_effort}` : null,
      usage.max_input_tokens ? `Max input: ${formatTokenCount(usage.max_input_tokens)} tokens` : null,
      (usage.available_prompt_tokens ?? usage.effective_prompt_budget) ? `Effective prompt budget: ${formatTokenCount(usage.available_prompt_tokens ?? usage.effective_prompt_budget)} tokens` : null,
      usage.loop_pressure_threshold ? `Loop pressure threshold: ${formatTokenCount(usage.loop_pressure_threshold)} tokens` : null,
      policy?.phase ? `Projection phase: ${policy.phase}` : null,
      policy?.pressure_mode ? `Projection mode: ${policy.pressure_mode}` : null,
      policy?.steady_target_tokens ? `Steady target: ${formatTokenCount(policy.steady_target_tokens)} tokens` : null,
      policy?.burst_target_tokens ? `Within-turn burst target: ${formatTokenCount(policy.burst_target_tokens)} tokens` : null
    ].filter(Boolean);
    return lines.join('\n');
  }

  function contextUsageProfileLabel(usage: ContextUsage | null): string | null {
    if (!usage?.agent_profile_id) return null;
    if (usage.agent_profile_synthetic || usage.agent_profile_id === 'default') return null;
    return usage.agent_profile_id;
  }

  function activeContextUsage(): ContextUsage | null {
    return contextUsage ?? sessionInfo?.context_usage ?? null;
  }

  function activeLastGeneration(): GenerationPerformanceSnapshot | null {
    return lastGenerationPerformance ?? sessionInfo?.last_generation ?? null;
  }

  function applyContextUsageSnapshot(usage: ContextUsage | null | undefined): void {
    if (!usage) return;
    contextUsage = usage;
    if (sessionInfo) {
      sessionInfo = { ...sessionInfo, context_usage: usage };
    }
  }

  function applyLastGenerationSnapshot(
    performance: GenerationPerformanceSnapshot | null | undefined
  ): void {
    if (performance === undefined) return;
    lastGenerationPerformance = mergeLatestPerformance(lastGenerationPerformance, performance);
    if (sessionInfo) {
      sessionInfo = { ...sessionInfo, last_generation: performance };
    }
  }

  function sessionNarrativeText(info: SessionInfoData | null): string | null {
    if (!info) return null;
    if (info.summary) return info.summary;
    const intentionView = extractIntentionDisplay(info.intention);
    return intentionView.intention || intentionView.title || null;
  }

  function sessionNarrativeLabel(info: SessionInfoData | null): string {
    return info?.summary ? 'Summary' : 'Intention';
  }

  function contextMetricDescription(label: string): string {
    const descriptions: Record<string, string> = {
      Prompt: 'Tokens currently projected into this model request. High values increase latency/cost and can reduce model focus; if this stays high, split the work or compact the session.',
      'Provider window': 'Total context window reported by the selected model. Larger windows are safety margin, not a goal to fill.',
      'Max input': 'Hard prompt/input cap reported by the selected model. Some providers expose a larger total context window but a smaller input limit.',
      'Effective prompt budget': 'Prompt room after applying input caps and reserving output tokens. Near-full usage raises overflow risk.',
      'Loop pressure': 'Threshold where projection should adapt before a hard stop. Crossing it repeatedly means the turn is too broad or tool output is too large.',
      'Projection phase': 'within_turn keeps active evidence richer; cross_turn replays older context conservatively.',
      'Projection mode': 'normal, pressure, or critical projection selected for the latest call. Pressure/critical means Cognis is shrinking tool evidence to stay safe.',
      'Steady target': 'Preferred prompt size for quality and cost. Running far above it is allowed temporarily but can make answers slower and less focused.',
      'Within-turn burst': 'Temporary larger budget for active evidence in the current turn. If this fills, split the task or recover only specific tool details.',
      'Hard target': 'Selected upper prompt budget before Cognis must shrink more or stop. Near this value, manual compaction or a new conversation may help.',
      'Cross-turn tools': 'Budget for replaying older tool output. Older details remain recoverable by call_id instead of being kept raw in every prompt.',
      'Within-turn tools': 'Budget for active turn tool evidence to avoid repeated recovery calls. High pressure here means the current turn is tool-output heavy.',
      'Recent tool groups': 'Completed tool groups protected before older results become placeholders. More groups preserve continuity but cost more context.'
    };
    return descriptions[label] ?? '';
  }

  function contextMetric(label: string, value: string): { label: string; value: string; description: string } {
    return {
      label,
      value,
      description: contextMetricDescription(label)
    };
  }

  function contextMetricPercent(value: number | null | undefined, maximum: number | null | undefined): number {
    if (typeof value !== 'number' || typeof maximum !== 'number' || maximum <= 0) return 0;
    return clampPercent((value / maximum) * 100);
  }

  function contextMetricBarColor(label: string, percent: number): string {
    if (label === 'Steady target') {
      if (percent > 130) return 'bg-rose-400';
      if (percent > 110) return 'bg-orange-400';
      if (percent > 85) return 'bg-amber-300';
      return 'bg-emerald-400';
    }
    if (label === 'Within-turn burst') {
      if (percent > 100) return 'bg-rose-400';
      if (percent > 95) return 'bg-orange-400';
      if (percent > 80) return 'bg-amber-300';
      return 'bg-emerald-400';
    }
    if (label === 'Hard target') {
      if (percent > 100) return 'bg-rose-400';
      if (percent > 95) return 'bg-orange-400';
      if (percent > 85) return 'bg-amber-300';
      return 'bg-emerald-400';
    }
    if (label === 'Effective prompt budget' || label === 'Loop pressure') {
      if (percent > 98) return 'bg-rose-400';
      if (percent > 90) return 'bg-orange-400';
      if (percent > 75) return 'bg-amber-300';
      return 'bg-emerald-400';
    }
    if (percent > 95) return 'bg-rose-400';
    if (percent > 85) return 'bg-orange-400';
    if (percent > 65) return 'bg-amber-300';
    return 'bg-emerald-400';
  }

  function contextBudgetBars(usage: ContextUsage): Array<{
    label: string;
    value: string;
    description: string;
    percent: number;
    color: string;
  }> {
    const policy = usage.projection_policy;
    const effectiveBudget = usage.available_prompt_tokens ?? usage.effective_prompt_budget;
    const bars = [
      {
        label: 'Prompt usage',
        value: `${formatTokenCount(usage.prompt_tokens)} / ${formatTokenCount(usage.max_context_tokens)}`,
        description: contextMetricDescription('Prompt'),
        percent: clampPercent(contextWindowUsagePercentage(usage))
      },
      {
        label: 'Effective prompt budget',
        value: `${formatTokenCount(usage.prompt_tokens)} / ${formatTokenCount(effectiveBudget)}`,
        description: contextMetricDescription('Effective prompt budget'),
        percent: contextMetricPercent(usage.prompt_tokens, effectiveBudget)
      },
      {
        label: 'Loop pressure',
        value: `${formatTokenCount(usage.prompt_tokens)} / ${formatTokenCount(usage.loop_pressure_threshold)}`,
        description: contextMetricDescription('Loop pressure'),
        percent: contextMetricPercent(usage.prompt_tokens, usage.loop_pressure_threshold)
      },
      {
        label: 'Steady target',
        value: `${formatTokenCount(usage.prompt_tokens)} / ${formatTokenCount(policy?.steady_target_tokens)}`,
        description: contextMetricDescription('Steady target'),
        percent: contextMetricPercent(usage.prompt_tokens, policy?.steady_target_tokens)
      },
      {
        label: 'Within-turn burst',
        value: `${formatTokenCount(usage.prompt_tokens)} / ${formatTokenCount(policy?.burst_target_tokens)}`,
        description: contextMetricDescription('Within-turn burst'),
        percent: contextMetricPercent(usage.prompt_tokens, policy?.burst_target_tokens)
      },
      {
        label: 'Hard target',
        value: `${formatTokenCount(usage.prompt_tokens)} / ${formatTokenCount(policy?.hard_prompt_tokens)}`,
        description: contextMetricDescription('Hard target'),
        percent: contextMetricPercent(usage.prompt_tokens, policy?.hard_prompt_tokens)
      }
    ];
    return bars.map((bar) => ({ ...bar, color: contextMetricBarColor(bar.label, bar.percent) }));
  }

  function contextBudgetChips(usage: ContextUsage): Array<{ label: string; value: string; description: string }> {
    const policy = usage.projection_policy;
    return [
      contextMetric('Projection phase', policy?.phase ?? 'n/a'),
      contextMetric('Projection mode', policy?.pressure_mode ?? 'n/a'),
      contextMetric('Cross-turn tools', formatTokenCount(policy?.cross_turn_tool_budget_tokens)),
      contextMetric('Within-turn tools', formatTokenCount(policy?.within_turn_tool_budget_tokens)),
      contextMetric('Recent tool groups', formatTokenCount(policy?.preserved_recent_tool_groups))
    ];
  }

  function contextMetrics(usage: ContextUsage): Array<{ label: string; value: string; description: string }> {
    const policy = usage.projection_policy;
    return [
      contextMetric('Prompt', formatTokenCount(usage.prompt_tokens)),
      contextMetric('Provider window', formatTokenCount(usage.max_context_tokens)),
      contextMetric('Max input', usage.max_input_tokens ? formatTokenCount(usage.max_input_tokens) : 'n/a'),
      contextMetric('Effective prompt budget', formatTokenCount(usage.available_prompt_tokens ?? usage.effective_prompt_budget)),
      contextMetric('Loop pressure', formatTokenCount(usage.loop_pressure_threshold)),
      contextMetric('Projection phase', policy?.phase ?? 'n/a'),
      contextMetric('Projection mode', policy?.pressure_mode ?? 'n/a'),
      contextMetric('Steady target', formatTokenCount(policy?.steady_target_tokens)),
      contextMetric('Within-turn burst', formatTokenCount(policy?.burst_target_tokens)),
      contextMetric('Hard target', formatTokenCount(policy?.hard_prompt_tokens)),
      contextMetric('Cross-turn tools', formatTokenCount(policy?.cross_turn_tool_budget_tokens)),
      contextMetric('Within-turn tools', formatTokenCount(policy?.within_turn_tool_budget_tokens)),
      contextMetric('Recent tool groups', formatTokenCount(policy?.preserved_recent_tool_groups))
    ];
  }

  function toggleHeaderInfo(): void {
    headerInfoMode = 'full';
    headerInfoOpen = !headerInfoOpen;
    if (headerInfoOpen && !sessionInfo) {
      void loadSessionInfo();
    }
  }

  function toggleContextInfo(): void {
    if (headerInfoOpen && headerInfoMode === 'context') {
      headerInfoOpen = false;
      return;
    }
    headerInfoMode = 'context';
    headerInfoOpen = true;
    if (!sessionInfo) {
      void loadSessionInfo();
    }
  }

  function closeHeaderInfo(): void {
    headerInfoOpen = false;
  }

  function applyQueuedMessageSnapshot(messages: QueuedMessage[], count = messages.length): void {
    queuedMessages = messages;
    queuedCount = count;
    // Optimistic local user messages are reconciled by the Chat v2 store; only
    // the persisted optimistic drafts need clearing here.
    for (const message of messages) {
      if (message.client_message_id) {
        removeOptimisticUserMessageDraft(directQuestionDraftConversationId() ?? routeConversationId, message.client_message_id);
      }
    }
    const liveQueueIds = new Set(messages.map((message) => message.queue_id));
    queueExpandedIds = queueExpandedIds.filter((queueId) => liveQueueIds.has(queueId));
    if (queueEditingId && !liveQueueIds.has(queueEditingId)) {
      queueEditingId = null;
      queueEditContent = '';
      queueEditError = '';
    }
  }

  function applyConversationRuntimeSnapshot(
    event: Extract<CognisWebSocketEvent, { type: 'conversation_runtime_snapshot' }>
  ): void {
    if (isRuntimeSnapshotOlderThanView(event.server_time, conversationViewServerTimeMs)) {
      void refreshConversationView('stale-runtime-snapshot');
      return;
    }
    applyQueuedMessageSnapshot(event.queued_messages, event.queued_count);
    applyLastGenerationSnapshot(event.last_generation);
    lastRuntimeSnapshotAt = Date.now();
    // A genuine runtime snapshot proves the WebSocket works: re-arm the
    // stale-runtime refresh budget.
    staleRuntimeRefreshAttempts = 0;
    // Chat v2 runtime frames are the sole authority for turn state. This
    // compatibility event retains queue/performance metadata only.
  }

  async function refreshQueuedMessages(conversationId = currentConversation?.conversation_id ?? ''): Promise<void> {
    if (!conversationId) return;
    try {
      const response = await api.conversations.getQueue(conversationId);
      if (conversationId !== currentConversation?.conversation_id) return;
      applyQueuedMessageSnapshot(response.messages, response.queued_count);
    } catch {
      // Queue state is best-effort and will recover on the next websocket event.
    }
  }

  async function deleteQueuedMessage(queueId: string): Promise<void> {
    const conversation = mutableQueuedMessagesConversation();
    if (!conversation) return;
    const conversationId = conversation.conversation_id;
    const previous = queuedMessages;
    queueBusyId = queueId;
    applyQueuedMessageSnapshot(
      queuedMessages.filter((item) => item.queue_id !== queueId),
      Math.max(queuedCount - 1, 0),
    );
    try {
      await chatV2Api.deleteQueuedMessage(conversationId, queueId, {
        client_txn_id: crypto.randomUUID()
      });
      await recoverChatV2Snapshot(conversationId);
    } catch (caughtError) {
      applyQueuedMessageSnapshot(previous, previous.length);
      addToast(asApiError(caughtError).message, 'error');
      await recoverChatV2Snapshot(conversationId);
    } finally {
      queueBusyId = null;
    }
  }

  function startQueuedMessageEdit(message: QueuedMessage): void {
    queueEditingId = message.queue_id;
    queueEditContent = message.content;
    queueEditError = '';
    if (!queueIsExpanded(message.queue_id)) {
      queueExpandedIds = [...queueExpandedIds, message.queue_id];
    }
  }

  function cancelQueuedMessageEdit(): void {
    queueEditingId = null;
    queueEditContent = '';
    queueEditError = '';
  }

  async function saveQueuedMessageEdit(queueId: string): Promise<void> {
    const conversation = mutableQueuedMessagesConversation();
    if (!conversation) return;
    const content = queueEditContent.trim();
    if (!content) {
      queueEditError = 'Queued message cannot be empty.';
      return;
    }

    const conversationId = conversation.conversation_id;
    const previous = queuedMessages;
    queueBusyId = queueId;
    queueEditError = '';
    applyQueuedMessageSnapshot(
      queuedMessages.map((item) => (
        item.queue_id === queueId
          ? { ...item, content, updated_at: new Date().toISOString() }
          : item
      )),
      queuedCount,
    );

    try {
      await chatV2Api.updateQueuedMessage(conversationId, queueId, {
        client_txn_id: crypto.randomUUID(),
        content,
      });
      if (queueEditingId === queueId) {
        cancelQueuedMessageEdit();
      }
      await recoverChatV2Snapshot(conversationId);
    } catch (caughtError) {
      applyQueuedMessageSnapshot(previous, previous.length);
      queueEditingId = queueId;
      queueEditContent = content;
      queueEditError = asApiError(caughtError).message;
      addToast(queueEditError, 'error');
      await recoverChatV2Snapshot(conversationId);
    } finally {
      queueBusyId = null;
    }
  }

  function queueIsExpanded(queueId: string): boolean {
    return queueExpandedIds.includes(queueId);
  }

  function toggleQueuedMessage(queueId: string): void {
    queueExpandedIds = queueIsExpanded(queueId)
      ? queueExpandedIds.filter((id) => id !== queueId)
      : [...queueExpandedIds, queueId];
  }

  function canMutateQueuedMessages(): boolean {
    return mutableQueuedMessagesConversation() !== null;
  }

  function mutableQueuedMessagesConversation(): Conversation | null {
    if (!currentConversation || isReadOnly(currentConversation)) return null;
    return currentConversation;
  }
  let contextUsage = $state<ContextUsage | null>(null);
  let lastGenerationPerformance = $state<GenerationPerformanceSnapshot | null>(null);
  let subSessionInfoOpen = $state(false);
  let subSessionInfo = $state<SessionInfoData | null>(null);
  let subSessionInfoLoading = $state(false);
  const NON_TERMINAL_SESSION_STATES = new Set(['active', 'idle', 'running']);

  let pendingDirectQuestion = $state<PendingDirectQuestion | null>(null);
  let directQuestionAnswers = $state<QuestionDraftAnswers>({});
  let directQuestionPageIndex = $state(0);
  let directQuestionCollapsed = $state(false);
  let pendingCredentialRequest = $state<Notification | null>(null);
  let directQuestionSubmitting = $state(false);
  let chatTodoDrawerOpen = $state(false);
  let retainedChatTodos = $state<ChatTodo[]>([]);
  let backendConversationState = $state<ConversationStateEnvelope | null>(null);

  const sessionIds = new Set<string>();

  let unsubscribeWs: (() => void) | null = null;
  let unsubscribeComposerFocus: (() => void) | null = null;
  let unsubscribeCancelTurn: (() => void) | null = null;
  let visibilityHandler: (() => void) | null = null;
  let focusHandler: (() => void) | null = null;
  let pageShowHandler: ((event: PageTransitionEvent) => void) | null = null;
  let onlineHandler: (() => void) | null = null;
  let foregroundSyncTimer: number | null = null;
  let historySectionDayTimer: number | null = null;
  let conversationLoadRequestId = 0;
  let mobileDrawerPreviouslyFocused: HTMLElement | null = null;
  let initialLoadTimeoutTimer: number | null = null;
  let conversationSwitchTimeoutTimer: number | null = null;
  let conversationSwitchStartedAt = 0;

  const CHAT_INITIAL_LOAD_TIMEOUT_MS = 10000;
  const emptySearchMatchedIds = new Set<string>();

  function isLlmUnavailableForSetup(): boolean {
    const llmDetails = JSON.stringify($workspaceHealth.health?.providers?.llm ?? {}).toLowerCase();
    return llmDetails.includes('no llm model configured') || llmDetails.includes('not configured');
  }

  function isMemoryDegraded(): boolean {
    const status = String($workspaceHealth.health?.providers?.memory?.status ?? 'unknown');
    return status !== 'healthy' && status !== 'unknown';
  }

  function isWebConversation(conversation: Conversation | null): boolean {
    return conversation?.context?.type?.toLowerCase() === 'web';
  }

  function isManagedConversation(conversation: Conversation | null): boolean {
    const contextType = String(conversation?.context?.type ?? '').toLowerCase();
    const managedChannel = String(conversation?.managed_agent?.channel ?? '');
    return contextType === 'agent_work'
      || contextType === 'managed_agent_conversation'
      || managedChannel === 'agent_work'
      || managedChannel === 'managed_agent_conversation';
  }

  function managedConversationState(conversation: Conversation | null): string {
    return conversation?.managed_agent?.conversation_state ?? 'open';
  }

  function managedTurnState(conversation: Conversation | null): string {
    return managedConversationTurnState(conversation);
  }

  function managedConversationActive(conversation: Conversation | null): boolean {
    const state = managedTurnState(conversation);
    return state === 'running' || state === 'queued';
  }

  function applyManagedActionResponse(response: { managed_agent?: Conversation['managed_agent'] | null }): void {
    if (!currentConversation || !response.managed_agent) return;
    const updated = {
      ...currentConversation,
      managed_agent: response.managed_agent,
      has_active_turn: response.managed_agent.turn_state === 'running' || response.managed_agent.turn_state === 'queued',
    };
    currentConversation = updated;
    patchConversationInList(updated.conversation_id, updated);
  }

  let showPushPrompt = $derived.by(() => {
    if (!currentConversation || !isWebConversation(currentConversation)) return false;
    if (pushPromptDismissed || pushSubscriptionKnownEnabled) return false;
    if (permissionState() === 'denied') return false;
    return isWebPushSupported() || needsIosHomeScreenInstall();
  });

  function pushPromptText(): string {
    if (needsIosHomeScreenInstall()) {
      return 'Install Cognis to your Home Screen, then open it there to enable native iOS notifications.';
    }
    return 'Enable native notifications for new web chat replies, task updates, and approval prompts.';
  }

  async function enableChatNotifications(): Promise<void> {
    pushPromptBusy = true;
    pushPromptError = '';
    pushDeliveryError = '';
    try {
      const result = await enableWebPush();
      if (result.ok) {
        pushSubscriptionKnownEnabled = true;
        pushPromptDismissed = true;
        setWebPushPromptDismissed(true);
        addToast(result.message, 'success');
      } else {
        pushPromptError = result.message;
      }
    } finally {
      pushPromptBusy = false;
    }
  }

  async function reconcileChatNotifications(): Promise<void> {
    if (!pushSubscriptionKnownEnabled) return;
    pushSubscriptionKnownEnabled = await reconcileWebPushSubscription();
    const status = await api.push.status().catch(() => null);
    pushDeliveryError = status?.last_error ?? '';
  }

  function isReadOnly(conversation: Conversation | null): boolean {
    if (!conversation) return true;
    if (conversation.status !== 'active') return true;
    if (isManagedConversation(conversation)) return true;
    if (!isWebConversation(conversation)) return true;
    return false;
  }

  const BLOCKED_SESSION_STATES = new Set(['terminated', 'failed', 'cancelled', 'suspended']);
  function activeSessionStatus(): string | null {
    if (!currentConversation?.active_session_id) return null;
    const root = sessions.find((s) => s.session_id === currentConversation?.active_session_id);
    return root?.status ?? null;
  }

  function backingSessionCount(): number {
    if (!currentConversation?.active_session_id) return 0;

    const byId = new Map(sessions.map((session) => [session.session_id, session]));
    const visited = new Set<string>();
    let count = 0;
    let currentSessionId: string | null = currentConversation.active_session_id;

    while (currentSessionId && !visited.has(currentSessionId)) {
      visited.add(currentSessionId);
      const session = byId.get(currentSessionId);
      if (!session || session.parent_session_id) break;
      count += 1;
      currentSessionId = session.previous_session_id;
    }

    return count;
  }

  function backingSessionBadgeLabel(): string | null {
    const count = backingSessionCount();
    return count > 1 ? `${count} backing sessions` : null;
  }

  let backingSessionBadge = $derived(backingSessionBadgeLabel());

  /**
   * The set of session IDs in the active session's compaction lineage.
   * Walks the previous_session_id chain from the active session, stopping
   * at any session with parent_session_id set (sub-sessions are not lineage).
    * Used by the session filter to allow in-flight items from compaction
    * predecessors while rejecting sub-session items.
   */
  let activeSessionLineage: ReadonlySet<string> = $derived.by(() => {
    const activeSessionId = currentConversation?.active_session_id;
    if (!activeSessionId) return new Set<string>();
    const byId = new Map(sessions.map((s) => [s.session_id, s]));
    const lineage = new Set<string>();
    const visited = new Set<string>();
    let currentId: string | null = activeSessionId;
    while (currentId && !visited.has(currentId)) {
      visited.add(currentId);
      const s = byId.get(currentId);
      if (!s || s.parent_session_id) break; // stop at sub-sessions
      lineage.add(currentId);
      currentId = s.previous_session_id ?? null;
    }
    return lineage;
  });

  function isSessionBlocked(): boolean {
    const status = activeSessionStatus();
    return status !== null && BLOCKED_SESSION_STATES.has(status);
  }

  async function startNewConversationFromBlockedSession(): Promise<void> {
    if (!currentConversation || isReadOnly(currentConversation)) return;
    error = '';
    awaitingAssistantStart = false;
    turnInProgress = false;
    activeTurnChatMode = 'default';
    clearConversationTurnState(currentConversation.conversation_id);
    try {
      const response = await executeCommandWithRetry(currentConversation.conversation_id, '/new');
      await applyCommandResponse(response);
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to start a new conversation');
    }
  }

  function directQuestionOptions(options: unknown): string[] {
    if (!Array.isArray(options)) return [];
    return options
      .map((option) => {
        if (typeof option === 'string') return option;
        if (option && typeof option === 'object') {
          const label = (option as Record<string, unknown>).label;
          return typeof label === 'string' ? label : '';
        }
        return '';
      })
      .filter((option) => option.length > 0);
  }

  function directQuestionContext(context: unknown): string {
    if (typeof context === 'string') return context;
    if (context && typeof context === 'object') {
      const text = (context as Record<string, unknown>).context ?? (context as Record<string, unknown>).note;
      return typeof text === 'string' ? text : '';
    }
    return '';
  }

  function pendingDirectQuestionFromParts(
    notificationId: string,
    stepName: string | undefined,
    question: unknown,
    options: unknown,
    context: unknown,
    questionId?: string,
    kind: PendingDirectQuestion['kind'] = 'question',
  ): PendingDirectQuestion {
    return {
      notificationId,
      stepName,
      question: typeof question === 'string' && question.trim().length > 0
        ? question.trim()
        : kind === 'auth_challenge'
          ? 'Authentication is required to continue.'
          : 'The assistant needs more input to continue.',
      questionId,
      options: directQuestionOptions(options),
      context: directQuestionContext(context),
      kind
    };
  }

  function pendingDirectQuestionFromQuestionSet(
    notificationId: string,
    stepName: string | undefined,
    questions: unknown,
    context: unknown,
  ): PendingDirectQuestion {
    const items = Array.isArray(questions) ? (questions as QuestionSetQuestion[]) : [];
    const firstQuestion = items[0];
    const pending = pendingDirectQuestionFromParts(
      notificationId,
      stepName,
      firstQuestion?.question,
      firstQuestion?.options,
      context,
      typeof firstQuestion?.id === 'string' ? firstQuestion.id : undefined,
      'question',
    );
    pending.questions = items;
    pending.structured = items.length > 1 || items.some((item) => Array.isArray(item.options) && item.options.length > 0);
    return pending;
  }

  function directQuestionItems(): QuestionSetQuestion[] {
    if (pendingDirectQuestion?.questions && pendingDirectQuestion.questions.length > 0) {
      return pendingDirectQuestion.questions;
    }
    if (!pendingDirectQuestion) return [];
    return [{
      id: pendingDirectQuestion.questionId ?? 'q1',
      question: pendingDirectQuestion.question,
      header: null,
      options: pendingDirectQuestion.options.map((option, index) => ({
        id: option,
        label: option,
        description: null
      })),
      multiple: false,
      allow_custom: true,
      required: true
    }];
  }

  let directQuestionVisibleItems = $derived.by(() => directQuestionItems());
  let directQuestionCurrentIndex = $derived.by(() => directQuestionClampedPageIndex(directQuestionVisibleItems));
  let currentDirectQuestion = $derived.by(() => directQuestionVisibleItems[directQuestionCurrentIndex] ?? null);

  function directQuestionClampedPageIndex(questions = directQuestionItems()): number {
    if (questions.length === 0) return 0;
    return Math.min(Math.max(directQuestionPageIndex, 0), questions.length - 1);
  }

  function visibleDirectQuestion(): QuestionSetQuestion | null {
    return currentDirectQuestion;
  }

  function directQuestionState(questionId: string): { selected: string[]; custom: string } {
    return directQuestionAnswers[questionId] ?? { selected: [], custom: '' };
  }

  function directQuestionDraftConversationId(): string | null {
    return currentConversation?.conversation_id ?? routeConversationId ?? null;
  }

  function activeDirectQuestionNotificationId(): string | null {
    return pendingDirectQuestion?.notificationId ?? null;
  }

  function persistDirectQuestionDraft(): void {
    const conversationId = directQuestionDraftConversationId();
    const notificationId = activeDirectQuestionNotificationId();
    writeQuestionDraft(conversationId, notificationId, directQuestionAnswers);
  }

  function restoreDirectQuestionDraft(question: PendingDirectQuestion | null): QuestionDraftAnswers {
    if (!question) return {};
    const conversationId = directQuestionDraftConversationId();
    return readQuestionDraft(conversationId, question.notificationId);
  }

  function clearActiveDirectQuestionDraft(): void {
    const conversationId = directQuestionDraftConversationId();
    const notificationId = activeDirectQuestionNotificationId();
    clearQuestionDraft(conversationId, notificationId);
  }

  function reconcileOptimisticUserMessageDrafts(): void {
    const conversationId = directQuestionDraftConversationId();
    const queuedClientMessageIds = new Set<string>();
    for (const message of queuedMessages) {
      if (conversationId && message.client_message_id) {
        removeOptimisticUserMessageDraft(conversationId, message.client_message_id);
        queuedClientMessageIds.add(message.client_message_id);
      }
    }
    if (!conversationId) return;
    // Optimistic user messages are reconciled in the Chat v2 store. A draft is
    // settled once a canonical user message with the same client_message_id is
    // present in the visible timeline.
    const confirmedClientMessageIds = new Set<string>();
    for (const item of chatV2Store.visibleItems) {
      if (item.kind === 'message' && item.role === 'user' && item.client_message_id && item.stable !== false) {
        confirmedClientMessageIds.add(item.client_message_id);
      }
    }
    const drafts = readOptimisticUserMessageDrafts(conversationId).filter(
      (draft) => !queuedClientMessageIds.has(draft.clientMessageId)
    );
    for (const draft of drafts) {
      if (confirmedClientMessageIds.has(draft.clientMessageId)) {
        removeOptimisticUserMessageDraft(conversationId, draft.clientMessageId);
      }
    }
  }

  function setDirectQuestionCustom(questionId: string, value: string): void {
    const current = directQuestionState(questionId);
    directQuestionAnswers = {
      ...directQuestionAnswers,
      [questionId]: { ...current, custom: value }
    };
    persistDirectQuestionDraft();
  }

  function buildDirectQuestionReply(
    questions: QuestionSetQuestion[],
    composerCustomAnswer = '',
    override?: QuestionSetAnswer
  ): QuestionSetReply {
    const trimmedComposerAnswer = composerCustomAnswer.trim();
    const answers = questions.map((question, index) => {
      if (override && override.question_id === question.id) return override;
      const current = directQuestionState(question.id);
      const custom = current.custom.trim() || (questions.length === 1 && index === 0 ? trimmedComposerAnswer : '');
      return {
        question_id: question.id,
        selected_option_ids: current.selected,
        custom_answer: custom ? custom : null
      };
    });
    return { mode: 'structured', answers };
  }

  function directQuestionAnswerSummary(reply: QuestionSetReply, questions: QuestionSetQuestion[]): string {
    const labelsByQuestion = new Map(
      questions.map((question) => [
        question.id,
        new Map(question.options.map((option) => [option.id, option.label]))
      ])
    );
    return reply.answers
      .map((answer) => {
        const selected = answer.selected_option_ids
          .map((optionId) => labelsByQuestion.get(answer.question_id)?.get(optionId) ?? optionId)
          .filter(Boolean);
        const parts = [...selected];
        if (answer.custom_answer) parts.push(answer.custom_answer);
        return parts.join(', ');
      })
      .filter((part) => part.length > 0)
      .join('\n');
  }

  function directQuestionReplyHasAnswer(reply: QuestionSetReply): boolean {
    return reply.answers.some((answer) => answer.selected_option_ids.length > 0 || Boolean(answer.custom_answer?.trim()));
  }

  function directQuestionReplySatisfiesRequired(questions: QuestionSetQuestion[], reply: QuestionSetReply): boolean {
    const answersById = new Map(reply.answers.map((answer) => [answer.question_id, answer]));
    return questions.every((question) => {
      if (!question.required) return true;
      const answer = answersById.get(question.id);
      return Boolean(answer && (answer.selected_option_ids.length > 0 || answer.custom_answer?.trim()));
    });
  }

  function directQuestionAnswerSatisfies(question: QuestionSetQuestion): boolean {
    if (!question.required) return true;
    const current = directQuestionState(question.id);
    return current.selected.length > 0 || Boolean(current.custom.trim());
  }

  function directQuestionOptionSelected(questionId: string, optionId: string): boolean {
    return directQuestionState(questionId).selected.includes(optionId);
  }

  function currentDirectQuestionReplyHasAnswer(): boolean {
    const questions = directQuestionItems();
    const reply = buildDirectQuestionReply(questions);
    return directQuestionReplyHasAnswer(reply) && directQuestionReplySatisfiesRequired(questions, reply);
  }

  function pendingInputToolKind(pendingStepTool: ReturnType<typeof selectPendingInputToolCall>): PendingDirectQuestion['kind'] {
    return pendingInputRequestKind({
      pendingStepTool,
      pendingDirectKind: pendingDirectQuestion?.kind ?? null
    });
  }

  function canGoToNextDirectQuestion(): boolean {
    const question = visibleDirectQuestion();
    return question !== null && directQuestionAnswerSatisfies(question);
  }

  function goToPreviousDirectQuestion(): void {
    directQuestionPageIndex = Math.max(0, directQuestionCurrentIndex - 1);
  }

  function goToNextDirectQuestion(): void {
    if (!canGoToNextDirectQuestion()) {
      addToast('Answer this required question before continuing.', 'error');
      return;
    }
    directQuestionPageIndex = Math.min(directQuestionCurrentIndex + 1, Math.max(0, directQuestionVisibleItems.length - 1));
  }

  async function submitCurrentDirectQuestionReply(): Promise<void> {
    const questions = directQuestionItems();
    const reply = buildDirectQuestionReply(questions);
    if (!directQuestionReplySatisfiesRequired(questions, reply)) {
      addToast('Answer all required questions before sending.', 'error');
      return;
    }
    await submitDirectQuestionReply(reply);
  }

  function pendingDirectQuestionFromNotification(notification: Notification): PendingDirectQuestion | null {
    if (!['step_question', 'auth_challenge'].includes(notification.notification_type) || notification.task_id || notification.status !== 'pending') {
      return null;
    }
    if (notification.notification_type === 'auth_challenge') {
      return pendingDirectQuestionFromParts(
        notification.notification_id,
        notification.step_name ?? undefined,
        notification.payload.message ?? notification.payload.label,
        [],
      notification.payload.metadata,
        undefined,
      'auth_challenge',
      );
    }
    return pendingDirectQuestionFromQuestionSet(
      notification.notification_id,
      notification.step_name ?? undefined,
      notification.payload.questions,
      notification.payload.context,
    );
  }

  function sortEscalations(items: Escalation[]): Escalation[] {
    return [...items].sort((left, right) => (left.received_at ?? 0) - (right.received_at ?? 0));
  }

  function isEscalationExpired(item: Escalation, now = Date.now()): boolean {
    const timeout = item.timeout_seconds ?? escalationTimeoutSeconds;
    const receivedAt = item.received_at ?? now;
    return now - receivedAt >= timeout * 1000;
  }

  function persistLastOpenedConversation(conversation: Conversation): void {
    if (typeof window === 'undefined') return;
    if (conversation.status === 'active' && isWebConversation(conversation) && !isAgentDirectConversation(conversation)) {
      const entry = serializeLastOpenedConversationEntry(lastOpenedConversationEntry(conversation));
      window.sessionStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, entry);
      window.sessionStorage.setItem(lastOpenedConversationStorageKey(conversation.agent_id), entry);
      window.localStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, entry);
      window.localStorage.setItem(lastOpenedConversationStorageKey(conversation.agent_id), entry);
      void api.conversations.rememberOpened(conversation.conversation_id).catch((error) => {
        console.debug('Failed to persist opened conversation', error);
      });
    }
  }

  function clearLastOpenedConversation(conversationId: string | null | undefined = null): void {
    if (typeof window === 'undefined') return;
    const stores = [window.sessionStorage, window.localStorage];
    if (!conversationId) {
      for (const storage of stores) {
        for (let index = storage.length - 1; index >= 0; index -= 1) {
          const key = storage.key(index);
          if (key && isLastOpenedConversationStorageKey(key)) {
            storage.removeItem(key);
          }
        }
      }
      return;
    }
    for (const storage of stores) {
      for (let index = storage.length - 1; index >= 0; index -= 1) {
        const key = storage.key(index);
        if (!key || !isLastOpenedConversationStorageKey(key)) continue;
        if (parseLastOpenedConversationEntry(storage.getItem(key))?.conversation_id === conversationId) {
          storage.removeItem(key);
        }
      }
    }
  }

  function nextVisibleConversationId(excludingConversationId: string): string | null {
    return conversations.find((conversation) => (
      conversation.conversation_id !== excludingConversationId
      && !isAgentDirectConversation(conversation)
    ))?.conversation_id ?? null;
  }

  let chatTodos = $derived.by(() => {
    const backendTodos = backendConversationState?.conversation_id === currentConversation?.conversation_id
      ? backendTodoSnapshot(backendConversationState)
      : null;
    if (backendTodos !== null) {
      return backendTodos;
    }
    const latestTodos = selectLatestTodoState(chatV2Store.visibleItems, currentConversation?.context?.type === 'web');
    if (latestTodos.length > 0) {
      return latestTodos;
    }
    if (pendingDirectQuestion !== null || directQuestionSubmitting) {
      return retainedChatTodos;
    }
    return [];
  });
  let activeChatTodos = $derived.by(() => incompleteTodos(chatTodos));
  let visibleChatProgressTodos = $derived(activeVisibleTodos(chatTodos));
  let shouldShowChatTodoProgress = $derived(turnInProgress && visibleChatProgressTodos.length > 0);
  // Keep the latest todo snapshot visible even after everything is
  // completed so the user can still inspect what just finished.
  let shouldShowChatTodoDrawer = $derived(chatTodos.length > 0);
  let chatTodoCounts = $derived.by(() => ({
    inProgress: activeChatTodos.filter((todo) => todo.status === 'in_progress').length,
    pending: activeChatTodos.filter((todo) => todo.status === 'pending').length,
  }));

  function visibleTodoSnapshot(todos: ChatTodo[] | undefined): ChatTodo[] {
    return activeVisibleTodos(todos);
  }

  function hasIncompleteTodo(todos: ChatTodo[]): boolean {
    return todos.some((todo) => todo.status !== 'completed' && todo.status !== 'cancelled');
  }

  function setConversationTodoSnapshot(conversationId: string | null | undefined, todos: ChatTodo[]): void {
    if (!conversationId) return;
    const current = conversationTodoSnapshots[conversationId];
    if (
      current?.length === todos.length
      && current.every((item, index) => {
        const next = todos[index];
        return item.content === next.content
          && item.status === next.status
          && item.priority === next.priority;
      })
    ) {
      return;
    }
    conversationTodoSnapshots = {
      ...conversationTodoSnapshots,
      [conversationId]: todos,
    };
  }

  function conversationTodoProgressTodos(conversation: Conversation): ChatTodo[] {
    if (conversation.conversation_id === currentConversation?.conversation_id) {
      return visibleChatProgressTodos.length > 0
        ? visibleChatProgressTodos
        : visibleTodoSnapshot(conversationTodoSnapshots[conversation.conversation_id]);
    }
    return visibleTodoSnapshot(conversationTodoSnapshots[conversation.conversation_id]);
  }

  function shouldShowConversationTodoProgress(conversation: Conversation): boolean {
    const todos = conversationTodoProgressTodos(conversation);
    return Boolean(conversation.has_active_turn || (conversation.conversation_id === currentConversation?.conversation_id && turnInProgress))
      && todos.length > 0
      && hasIncompleteTodo(todos);
  }

  function backendTodoSnapshot(state: ConversationStateEnvelope | null): ChatTodo[] | null {
    if (!state) return null;
    const activeSessionTodos = state.active_session?.todos;
    const taskTodos = (state.task?.relevant_step ?? state.task?.current_step ?? null)?.todos;
    const sourceTodos = Array.isArray(activeSessionTodos)
      ? activeSessionTodos
      : taskTodos;
    if (!Array.isArray(sourceTodos)) return null;
    return sourceTodos.map((todo) => ({
      content: todo.content,
      status: todo.status,
      priority: todo.priority ?? 'normal',
    }));
  }

  function conversationStateConversationPatch(state: ConversationStateEnvelope): Partial<Conversation> {
    const pendingInput = conversationPendingSnapshotFlags(state).hasAnyPendingInput;
    const patch: Partial<Conversation> = {
      pending_notification_types: state.pending?.notification_types ?? [],
      // A turn that is waiting for a question, credential, auth challenge, or
      // escalation is paused from the user's perspective. Do not render it as
      // active and trigger stale-runtime recovery refreshes that remount the
      // input form.
      has_active_turn: Boolean(state.active_turn?.has_active_turn) && !pendingInput,
      active_session_status: state.active_session?.status ?? null,
      active_session_completion_reason: state.active_session?.completion_reason ?? null,
      active_turn_chat_mode: typeof state.active_turn?.chat_mode === 'string'
        ? normalizeChatModeTone(state.active_turn.chat_mode)
        : null,
      active_turn_chat_mode_source: typeof state.active_turn?.chat_mode_source === 'string'
        ? state.active_turn.chat_mode_source as import('$lib/types/api').ChatModeSource
        : null,
    };
    return patch;
  }

  function applyConversationStateSnapshot(
    state: ConversationStateEnvelope | null | undefined,
    options: { patchConversationRows?: boolean } = {},
  ): void {
    if (!state) return;
    const { patchConversationRows = true } = options;
    const conversationId = state.conversation_id;
    const chatV2OwnsConversation = chatV2OwnsActiveConversation(conversationId);
    if (conversationId === currentConversation?.conversation_id) {
      backendConversationState = state;
      const pendingFlags = conversationPendingSnapshotFlags(state);
      turnInProgress = conversationStateTurnActivity({
        currentActive: turnInProgress,
        snapshotActive: Boolean(state.active_turn?.has_active_turn),
        hasPendingInput: pendingFlags.hasAnyPendingInput,
        chatV2OwnsConversation,
      });
      if (!pendingFlags.hasDirectQuestion) {
        pendingDirectQuestion = null;
      }
      if (!pendingFlags.hasCredentialRequest) {
        pendingCredentialRequest = null;
      }
      if (!pendingFlags.hasEscalation) {
        escalations = [];
        escalationBusyCallId = null;
        escalationError = '';
        escalationResolutionPending = null;
      }
    }
    if (patchConversationRows) {
      patchConversationInList(
        conversationId,
        conversationStatePatchForAuthority(
          conversationStateConversationPatch(state) as Record<string, unknown>,
          chatV2OwnsConversation,
        ) as Partial<Conversation>,
        { pendingNotificationSource: 'server' },
      );
    }
    const todos = backendTodoSnapshot(state);
    if (todos !== null) {
      setConversationTodoSnapshot(conversationId, todos);
    }
  }

  function applyConversationStateDelta(event: Extract<import('$lib/types/api').CognisWebSocketEvent, { type: 'conversation_state_delta' }>): void {
    const replacement = event.replace?.state;
    if (replacement && typeof replacement === 'object') {
      applyConversationStateSnapshot(replacement as ConversationStateEnvelope);
    } else if (event.snapshot_required && currentConversation?.conversation_id === event.conversation_id) {
      void reloadConversationSubloads(event.conversation_id, beginConversationLoad(), {
        reloadSessions: false,
        reloadHistory: true,
        resubscribe: false,
      });
    }
  }
  $effect(() => {
    if (currentConversation && chatTodos.length > 0) {
      setConversationTodoSnapshot(currentConversation.conversation_id, chatTodos);
    }
  });

  let isPreSessionConversation = $derived.by(() =>
    isPreSessionChatConversation(currentConversation, sessions.length)
  );

  function contextTypeBadge(conversation: Conversation): string {
    if (isManagedConversation(conversation)) return 'Agent-managed';
    const t = conversation.context?.type ?? 'unknown';
    return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase();
  }

  function conversationTaskId(conversation: Conversation | null): string | null {
    if (!conversation || conversation.context?.type !== 'task') return null;
    return typeof conversation.context?.ref === 'string' && conversation.context.ref.length > 0
      ? conversation.context.ref
      : null;
  }

  function socketErrorMessage(event: import('$lib/types/api').WebSocketErrorEvent): string {
    if (event.code === 'provider_unreachable:guardrails') {
      return 'Guardrails service is unreachable — tool calls are blocked until it recovers. Check that Intaris is running.';
    }
    if (event.code === 'provider_unreachable:memory') {
      return "Memory is currently unavailable — this conversation won't have access to past context.";
    }
    if (event.code === 'provider_not_configured:llm') {
      return 'No LLM provider is configured. Go to Settings → Providers to add one.';
    }
    if (event.code === 'provider_error:llm') {
      const detail = event.detail && typeof event.detail.error_detail === 'string' ? ` ${event.detail.error_detail}` : '';
      return `LLM provider returned an error.${detail}`.trim();
    }
    if (event.code === 'session_creation_failed') {
      return 'Could not create or recover the conversation session. Try again or check the diagnostics page.';
    }
    if (event.code === 'session_ended') {
      return 'This session has ended. Use /new or Start new conversation to continue.';
    }
    if (event.code === 'turn_cancelled') {
      return 'The current turn was cancelled.';
    }
    if (event.code === 'pending_question') {
      return 'Answer the pending clarification request to continue.';
    }
    if (event.code === 'pending_input_request') {
      return 'Resolve or cancel the pending credential/auth request to continue.';
    }
    return event.message;
  }

  function timestampValue(value: string | null | undefined): number {
    if (!value) return 0;
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function millisecondsUntilNextLocalDay(now = new Date()): number {
    const nextDay = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    return Math.max(1000, nextDay.getTime() - now.getTime() + 1000);
  }

  function stopHistorySectionDayTimer(): void {
    if (historySectionDayTimer === null || typeof window === 'undefined') return;
    window.clearTimeout(historySectionDayTimer);
    historySectionDayTimer = null;
  }

  function scheduleHistorySectionDayRefresh(): void {
    if (typeof window === 'undefined') return;
    stopHistorySectionDayTimer();
    historySectionDayTimer = window.setTimeout(() => {
      historySectionNow = new Date();
      scheduleHistorySectionDayRefresh();
    }, millisecondsUntilNextLocalDay());
  }

  function isAgentDirectConversation(conversation: Conversation | null | undefined): boolean {
    return isAgentDirectConversationSummary(conversation);
  }

  function canStarConversation(conversation: Conversation): boolean {
    return !isAgentDirectConversation(conversation);
  }

  function sortConversationsByActivity(items: Conversation[]): Conversation[] {
    return [...items].sort((left, right) => {
      const activityDelta = conversationActivityValue(right) - conversationActivityValue(left);
      if (activityDelta !== 0) return activityDelta;
      const createdDelta = timestampValue(right.created_at) - timestampValue(left.created_at);
      if (createdDelta !== 0) return createdDelta;
      return left.conversation_id.localeCompare(right.conversation_id);
    });
  }

  function sortAgentDirectChats(items: AgentDirectChat[]): AgentDirectChat[] {
    return [...items].sort((left, right) => {
      const activityDelta = conversationActivityValue(right.conversation) - conversationActivityValue(left.conversation);
      if (activityDelta !== 0) return activityDelta;
      return left.agent.agent_id.localeCompare(right.agent.agent_id);
    });
  }

  function mergeConversationList(items: Conversation[], { reset = false }: { reset?: boolean } = {}): void {
    for (const conversation of items) {
      observePendingNotificationServerPush(conversation.conversation_id);
      applyConversationStateSnapshot(conversation.conversation_state, { patchConversationRows: false });
      if (currentConversation?.conversation_id === conversation.conversation_id) {
        currentConversation = mergeConversationPreservingActivity(currentConversation, conversation);
        turnInProgress = conversation.has_active_turn;
        activeTurnChatMode = conversation.has_active_turn
          ? normalizeChatModeTone(conversation.active_turn_chat_mode)
          : 'default';
      }
      if (isAgentDirectConversation(conversation)) {
        patchAgentDirectChat(conversation);
      }
    }
    conversations = mergeSidebarConversationRows(conversations, items, { reset });
  }

  function patchAgentDirectChat(conversation: Conversation): void {
    if (!isAgentDirectConversation(conversation)) return;
    const agent = agents.find((candidate) => candidate.agent_id === conversation.agent_id);
    if (!agent) return;
    mergeAgentDirectChats([{ agent, conversation }]);
  }

  function mergeAgentDirectChats(items: AgentDirectChat[]): void {
    const merged = new Map(agentDirectChats.map((item) => [item.agent.agent_id, item]));
    for (const item of items) {
      applyConversationStateSnapshot(item.conversation.conversation_state, { patchConversationRows: false });
      const existing = merged.get(item.agent.agent_id);
      merged.set(item.agent.agent_id, existing
        ? {
            agent: item.agent,
            conversation: mergeConversationPreservingActivity(existing.conversation, item.conversation),
          }
        : item);
    }
    agentDirectChats = sortAgentDirectChats([...merged.values()]);
  }

  function observePendingNotificationServerPush(conversationId: string | null | undefined): void {
    if (!conversationId) return;
    pendingNotificationEpoch += 1;
    pendingNotificationEpochByConversation.set(conversationId, pendingNotificationEpoch);
  }

  function beginPendingNotificationRefresh(conversationId: string): number {
    pendingNotificationEpoch += 1;
    pendingNotificationEpochByConversation.set(conversationId, pendingNotificationEpoch);
    return pendingNotificationEpoch;
  }

  function maybeRecoverMissingConversationRow(conversationId: string, patch: Partial<Conversation>): void {
    const now = Date.now();
    if (!shouldRecoverMissingConversationRow({
      conversationId,
      patch,
      lastAttemptByConversation: missingConversationRecoveryAt,
      now,
    })) {
      return;
    }
    missingConversationRecoveryAt.set(conversationId, now);
    void resyncSidebarData('missing-attention-row', { force: true });
  }

  function patchConversationInList(
    conversationId: string,
    patch: Partial<Conversation>,
    options: {
      touchUpdatedAt?: boolean;
      touchLastMessageAt?: boolean;
      pendingNotificationSource?: 'server' | 'refetch';
    } = {}
  ): void {
    if (Object.prototype.hasOwnProperty.call(patch, 'pending_notification_types') && options.pendingNotificationSource === 'server') {
      observePendingNotificationServerPush(conversationId);
    }
    const index = conversations.findIndex((conversation) => conversation.conversation_id === conversationId);
    const now = new Date().toISOString();
    if (index < 0) {
      const direct = agentDirectChats.find((item) => item.conversation.conversation_id === conversationId);
      if (direct) {
        const effectivePatch: Partial<Conversation> = {
          ...patch,
          ...(options.touchUpdatedAt ? { updated_at: patch.updated_at ?? now } : {}),
          ...(options.touchLastMessageAt ? { last_message_at: patch.last_message_at ?? now } : {}),
        };
        const updatedDirectConversation = mergeConversationRowPatch(direct.conversation, effectivePatch);
        patchAgentDirectChat(updatedDirectConversation);
        if (currentConversation?.conversation_id === conversationId) {
          currentConversation = { ...currentConversation, ...updatedDirectConversation };
        }
        rememberSidebarProjection();
        return;
      }
      maybeRecoverMissingConversationRow(conversationId, patch);
      return;
    }
    const existing = conversations[index];
    const effectivePatch: Partial<Conversation> = {
      ...patch,
      ...(options.touchUpdatedAt ? { updated_at: patch.updated_at ?? now } : {}),
      ...(options.touchLastMessageAt ? { last_message_at: patch.last_message_at ?? now } : {}),
    };
    const updated = mergeConversationRowPatch(existing, effectivePatch);
    const next = [...conversations];
    next[index] = updated;
    conversations = sortConversationsByActivity(next);
    patchAgentDirectChat(updated);
    if (currentConversation?.conversation_id === conversationId) {
      currentConversation = { ...currentConversation, ...updated };
    }
    rememberSidebarProjection();
  }

  function removeConversationFromSidebar(
    conversationId: string,
    { markCurrentDeleted = false }: { markCurrentDeleted?: boolean } = {}
  ): void {
    const previousConversationCount = conversations.length;
    const previousDirectCount = agentDirectChats.length;
    conversations = removeSidebarConversationRow(conversations, conversationId);
    agentDirectChats = agentDirectChats.filter(
      (item) => item.conversation.conversation_id !== conversationId
    );
    conversationViewCache.delete(conversationId);
    clearLastOpenedConversation(conversationId);
    if (currentConversation?.conversation_id === conversationId && markCurrentDeleted) {
      currentConversation = { ...currentConversation, status: 'deleted' };
      turnInProgress = false;
      activeTurnChatMode = 'default';
    }
    if (previousConversationCount !== conversations.length || previousDirectCount !== agentDirectChats.length) {
      rememberSidebarProjection();
    }
  }

  function applySidebarConversationUpsert(conversation: Conversation): void {
    const conversationId = conversation.conversation_id;
    if (currentConversation?.conversation_id === conversationId) {
      currentConversation = mergeConversationPreservingActivity(currentConversation, conversation);
      turnInProgress = currentConversation.has_active_turn;
      activeTurnChatMode = currentConversation.has_active_turn
        ? normalizeChatModeTone(currentConversation.active_turn_chat_mode)
        : 'default';
    }
    if (conversationMatchesSidebarProjectionFilter(conversation, currentSidebarProjectionFilter())) {
      mergeConversationList([conversation]);
      patchAgentDirectChat(conversation);
      rememberSidebarProjection();
    } else {
      removeConversationFromSidebar(conversationId);
    }
  }

  function syncConversationActiveSession(activeSessionId: string | null | undefined): void {
    if (!currentConversation || !activeSessionId || currentConversation.active_session_id === activeSessionId) {
      return;
    }
    const previousSessionId = currentConversation.active_session_id;
    currentConversation = {
      ...currentConversation,
      active_session_id: activeSessionId,
      active_session_status: null,
      active_session_completion_reason: null,
    };
    patchConversationInList(
      currentConversation.conversation_id,
      {
        active_session_id: activeSessionId,
        active_session_status: null,
        active_session_completion_reason: null,
      },
      { touchUpdatedAt: true }
    );
    if (previousSessionId !== activeSessionId) {
      invalidateSessionInfo();
      if (headerInfoOpen) {
        void loadSessionInfo();
      } else {
        void refreshSessionContextUsage();
      }
    }
  }

  function setConversationTurnIndicator(
    conversationId: string | null | undefined,
    active: boolean,
    options: { touchLastMessageAt?: boolean } = {}
  ): void {
    if (!conversationId) return;
    patchConversationInList(
      conversationId,
      {
        has_active_turn: active,
        ...(options.touchLastMessageAt ? { last_message_at: new Date().toISOString() } : {}),
      },
      { touchUpdatedAt: false }
    );
  }

  function touchConversationViewCache(conversationId: string, entry: ConversationViewCacheEntry): void {
    conversationViewCache.delete(conversationId);
    conversationViewCache.set(conversationId, entry);
    while (conversationViewCache.size > CONVERSATION_VIEW_CACHE_LIMIT) {
      const oldestKey = conversationViewCache.keys().next().value;
      if (!oldestKey) break;
      conversationViewCache.delete(oldestKey);
    }
  }

  function saveCurrentConversationView(): void {
    try {
      if (!currentConversation) return;
      if (conversationViewSaveTimer !== null && typeof window !== 'undefined') {
        window.clearTimeout(conversationViewSaveTimer);
        conversationViewSaveTimer = null;
      }
      touchConversationViewCache(currentConversation.conversation_id, {
        conversation: { ...currentConversation },
        sessions: sessions.map((session) => ({ ...session })),
        // Keep the live Chat v2 overlay when switching within this tab. A
        // snapshot only carries scheduler state, not an idle-checkpoint
        // compaction item, so discarding this overlay made the compaction card
        // and the optimistic user message disappear until canonical history
        // eventually caught up.
        chatV2State: chatV2Store.serializeState(),
        queuedCount,
        queuedMessages: queuedMessages.map((message) => ({ ...message })),
        contextUsage,
        sessionInfo: sessionInfo ? { ...sessionInfo } : null,
        visibleStartIndex,
        scrollTop: timelineEl?.scrollTop ?? lastTimelineScrollTop,
        userScrolledUp,
        // The cached runtime is reconciled immediately by snapshots and
        // runtime frames, but preserving it avoids a blank active turn while
        // the conversation is briefly viewed elsewhere in the same tab.
        turnInProgress,
        awaitingAssistantStart,
        activeSessionLastSeq,
        olderMessagesCursor,
        hasOlderMessages,
        activeTurnChatMode,
        fetchedAt: Date.now(),
      });
    } catch (error) {
      console.warn('Failed to save conversation view cache', error);
    }
  }

  function scheduleCurrentConversationViewSave(): void {
    if (typeof window === 'undefined') {
      saveCurrentConversationView();
      return;
    }
    if (conversationViewSaveTimer !== null) {
      window.clearTimeout(conversationViewSaveTimer);
    }
    conversationViewSaveTimer = window.setTimeout(() => {
      conversationViewSaveTimer = null;
      saveCurrentConversationView();
    }, 1000);
  }

  function restoreConversationView(conversationId: string): ConversationViewCacheEntry | null {
    const entry = conversationViewCache.get(conversationId);
    if (!entry) return null;
    touchConversationViewCache(conversationId, entry);
    activeConversationId = conversationId;
    currentConversation = { ...entry.conversation };
    sessions = entry.sessions.map((session) => ({ ...session }));
    chatV2Store.restoreState(entry.chatV2State);
    queuedCount = entry.queuedCount;
    queuedMessages = entry.queuedMessages.map((message) => ({ ...message }));
    contextUsage = entry.contextUsage;
    sessionInfo = entry.sessionInfo ? { ...entry.sessionInfo } : null;
    turnInProgress = entry.turnInProgress;
    awaitingAssistantStart = entry.awaitingAssistantStart;
    activeSessionLastSeq = entry.activeSessionLastSeq;
    olderMessagesCursor = entry.olderMessagesCursor ?? null;
    hasOlderMessages = entry.hasOlderMessages ?? false;
    activeTurnChatMode = entry.activeTurnChatMode ?? 'default';
    // Opening a conversation always lands at the latest message (auto-tail),
    // regardless of where the user was scrolled when they last left it. The
    // cached mid-history scroll position is intentionally not restored on a
    // switch — a chat should show the newest content when opened.
    syncVisibleWindow();
    userScrolledUp = false;
    tailPinned = true;
    initialLoadTimedOut = false;
    mergeConversationList([entry.conversation]);
    requestAnimationFrame(() => {
      scrollToBottom(true);
    });
    return entry;
  }

  function clearConversationViewState(): void {
    sessions = [];
    sessionIds.clear();
    // Reset the canonical Chat v2 store immediately so the outgoing
    // conversation's content cannot flash into the incoming conversation.
    chatV2Store.reset();
    timelineWindow = { start: 0, end: null };
    lastRenderableVisibleCount = 0;
    activeSessionLastSeq = 0;
    olderMessagesCursor = null;
    hasOlderMessages = false;
    queuedCount = 0;
    queuedMessages = [];
    contextUsage = null;
    lastGenerationPerformance = null;
    sessionInfo = null;
    turnInProgress = false;
    awaitingAssistantStart = false;
    pendingDirectQuestion = null;
    pendingCredentialRequest = null;
    directQuestionPageIndex = 0;
    directQuestionCollapsed = false;
    directQuestionSubmitting = false;
    escalations = [];
    escalationError = '';
    escalationResolutionPending = null;
    lastRecoverableMessage = '';
    lastRecoverableTurnId = null;
    suppressedRecoverableTurnId = null;
    editingTitle = false;
    subSessionPanelOpen = false;
    // Opening/switching a conversation lands at the tail (latest message).
    // Pinning here makes the post-load refresh auto-scroll to bottom.
    userScrolledUp = false;
    tailPinned = true;
    lastTimelineScrollTop = 0;
  }

  function clearConversationTurnState(conversationId: string | null | undefined, lastMessageAt?: string | null): void {
    if (!conversationId) return;
    const patch: Partial<Conversation> = { has_active_turn: false };
    if (lastMessageAt !== undefined) {
      patch.last_message_at = lastMessageAt ?? undefined;
    }
    patchConversationInList(conversationId, patch, { touchLastMessageAt: lastMessageAt !== undefined });
    if (currentConversation?.conversation_id === conversationId) {
      turnInProgress = false;
      activeTurnChatMode = 'default';
    }
  }

  function sidebarConversationById(conversationId: string): Conversation | null {
    if (currentConversation?.conversation_id === conversationId) return currentConversation;
    return (
      conversations.find((conversation) => conversation.conversation_id === conversationId)
      ?? agentDirectChats.find((item) => item.conversation.conversation_id === conversationId)?.conversation
      ?? null
    );
  }

  function markConversationReadLocally(conversationId: string | null | undefined, readAt?: string | null): void {
    if (!conversationId) return;
    const conversation = sidebarConversationById(conversationId);
    const readFloor = readAt ?? conversation?.last_message_at ?? conversation?.last_read_at ?? null;
    patchConversationInList(conversationId, {
      has_unread: false,
      ...(readFloor ? { last_read_at: readFloor } : {}),
    });
  }

  async function loadConversationPage(reset = false): Promise<void> {
    conversationListLoadCount += 1;
    conversationListLoading = true;
    try {
      const response = await api.conversations.list(reset ? null : conversationCursor, {
        contextTypes: selectedChannels.length > 0 ? selectedChannels : null,
        agentIds: selectedAgentIds.length > 0 ? selectedAgentIds : null,
        status: selectedConversationStatus,
      });
      mergeConversationList(response.items, { reset });
      conversationCursor = response.cursor;
      conversationsHasMore = response.has_more;
    } finally {
      conversationListLoadCount = Math.max(0, conversationListLoadCount - 1);
      conversationListLoading = conversationListLoadCount > 0;
    }
  }

  function sidebarProjectionCacheKey(): string {
    return JSON.stringify({
      channels: selectedChannels,
      agents: selectedAgentIds,
      status: selectedConversationStatus,
    });
  }

  function currentSidebarProjectionFilter() {
    return {
      selectedChannels,
      selectedAgentIds,
      selectedConversationStatus,
    };
  }

  function rememberSidebarProjection(key = sidebarProjectionCacheKey()): void {
    rememberSidebarProjectionSnapshot(
      sidebarProjectionCache,
      key,
      {
        agents,
        agent_direct_chats: agentDirectChats,
        conversations: {
          items: conversations,
          cursor: conversationCursor,
          has_more: conversationsHasMore,
        },
        context_types: availableChannelTypes,
      },
      SIDEBAR_PROJECTION_CACHE_LIMIT,
    );
  }

  function applySidebarProjection(projection: SidebarProjection, options: { reset?: boolean } = {}): void {
    const reset = options.reset ?? true;
    if (reset || projection.agents.length > 0) agents = projection.agents;
    if (reset) {
      agentDirectChats = projection.agent_direct_chats;
    } else if (projection.agent_direct_chats.length > 0) {
      mergeAgentDirectChats(projection.agent_direct_chats);
    }
    if (reset || projection.context_types.length > 0) applyAvailableChannelTypes(projection.context_types);
    mergeConversationList(projection.conversations.items, { reset });
    for (const removedId of projection.removed_conversation_ids ?? []) {
      removeConversationFromSidebar(removedId);
    }
    if (reset) {
      conversationCursor = projection.conversations.cursor;
      conversationsHasMore = projection.conversations.has_more;
    }
    sidebarProjectionLoaded = true;
    sidebarProjectionFailed = false;
    if (projection.sync_timestamp) {
      lastSidebarSyncTimestamp = projection.sync_timestamp;
      lastSidebarSyncAt = Date.now();
    }
  }

  function applyCachedSidebarProjection(key = sidebarProjectionCacheKey()): boolean {
    const cached = sidebarProjectionCache.get(key);
    if (!cached) return false;
    applySidebarProjection(cached);
    return true;
  }

  function applyAvailableChannelTypes(contextTypes: string[]): void {
    const types = new Set(contextTypes.map((contextType) => contextType.toLowerCase()));
    for (const selectedChannel of selectedChannels) {
      types.add(selectedChannel.toLowerCase());
    }
    availableChannelTypes = [...types].sort();
  }

  async function loadSidebarProjection(): Promise<void> {
    conversationListLoadCount += 1;
    const initialKey = sidebarProjectionCacheKey();
    const hadCachedProjection = applyCachedSidebarProjection(initialKey);
    conversationListLoading = conversationListLoadCount > 1 || !hadCachedProjection;
    const load = () => api.conversations.sidebar(null, {
      contextTypes: selectedChannels.length > 0 ? selectedChannels : null,
      agentIds: selectedAgentIds.length > 0 ? selectedAgentIds : null,
      status: selectedConversationStatus,
    });
    try {
      let response = await load();
      if (initialKey !== sidebarProjectionCacheKey()) return;
      agents = response.agents;
      if (restoreSelectedAgent()) {
        const restoredKey = sidebarProjectionCacheKey();
        response = await load();
        if (restoredKey !== sidebarProjectionCacheKey()) return;
        agents = response.agents;
        restoreSelectedAgent();
      }
      applySidebarProjection(response);
      rememberSidebarProjection();
    } catch (caughtError) {
      sidebarProjectionFailed = true;
      throw caughtError;
    } finally {
      conversationListLoadCount = Math.max(0, conversationListLoadCount - 1);
      conversationListLoading = conversationListLoadCount > 0;
    }
  }

  async function loadAgentDirectChats(): Promise<void> {
    if (selectedChannels.length > 0 && !selectedChannels.includes('web')) {
      agentDirectChats = [];
      return;
    }
    agentDirectChats = await api.conversations.agentDirect({
      agentIds: selectedAgentIds.length > 0 ? selectedAgentIds : null,
      status: 'active'
    });
  }

  function persistEnterToSendPreference(): void {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(CHAT_STORAGE_KEYS.enterToSend, enterToSend ? '1' : '0');
  }

  function restoreEnterToSendPreference(): void {
    if (typeof window === 'undefined') return;
    // Only opt into Enter-to-send when the stored value is explicitly "1".
    // Absence (new users) or "0" both default to Enter-as-newline.
    enterToSend = window.localStorage.getItem(CHAT_STORAGE_KEYS.enterToSend) === '1';
  }

  function restoreSelectedAgent(): boolean {
    const previous = selectedAgentIds.join('\0');
    if (typeof window === 'undefined') return false;
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
    const validAgentIds = new Set(
      agents
        .filter((agent) => agent.status === 'active' && agent.agent_type === 'primary')
        .map((agent) => agent.agent_id)
    );
    selectedAgentIds = parseStoredMultiFilter(stored).filter((agentId) => validAgentIds.has(agentId));
    return selectedAgentIds.join('\0') !== previous;
  }

  function persistSelectedAgent(): void {
    if (typeof window === 'undefined') return;
    if (selectedAgentIds.length === 0) {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.selectedAgent);
      return;
    }
    window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedAgent, JSON.stringify(selectedAgentIds));
  }

  function clearSelectedAgentFilter(): boolean {
    const hadFilter = selectedAgentIds.length > 0
      || (typeof window !== 'undefined' && window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent) !== null);
    selectedAgentIds = [];
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.selectedAgent);
    }
    return hadFilter;
  }

  async function clearSelectedAgentFilterForDirectChat(): Promise<void> {
    if (!clearSelectedAgentFilter()) return;
    resetConversationSearchResults();
    await Promise.all([loadAgentDirectChats(), loadConversationPage(true)]);
  }

  function conversationIdFromRoute(): string {
    return routeConversationId;
  }

  function conversationUrl(conversationId: string, extraParams?: URLSearchParams | Record<string, string>): string {
    return buildConversationUrl(conversationId, selectedConversationStatus, extraParams);
  }

  async function replaceConversationStatusUrl(status: ConversationStatusFilter): Promise<void> {
    if (typeof window === 'undefined') return;
    const url = new URL(page.url);
    setConversationStatusSearchParam(url.searchParams, status);
    await goto(`${url.pathname}${url.search}${url.hash}`, {
      replaceState: true,
      noScroll: true,
      keepFocus: true,
    });
  }

  function syncServiceWorkerActiveConversation(active = true): void {
    if (typeof navigator === 'undefined' || !navigator.serviceWorker?.controller) return;
    const conversationId = switchingConversationId ?? currentConversation?.conversation_id ?? conversationIdFromRoute();
    const focused = typeof document !== 'undefined'
      && document.visibilityState === 'visible'
      && document.hasFocus();
    const shouldMarkActive = active && Boolean(conversationId) && (focused || conversationModeOpen);
    navigator.serviceWorker.controller.postMessage({
      type: 'ACTIVE_CONVERSATION',
      conversation_id: shouldMarkActive ? conversationId : null,
      active: shouldMarkActive,
    });
  }

  function isMobileViewport(): boolean {
    return typeof window !== 'undefined' && window.innerWidth < 1024;
  }

  function beginInitialLoadTimeout(): void {
    if (initialLoadTimeoutTimer !== null) {
      window.clearTimeout(initialLoadTimeoutTimer);
    }
    initialLoadTimedOut = false;
    initialLoadTimeoutTimer = window.setTimeout(() => {
      if (initializing || (conversationIdFromRoute() && !currentConversation && !error)) {
        initialLoadTimedOut = true;
      }
    }, CHAT_INITIAL_LOAD_TIMEOUT_MS);
  }

  function stopInitialLoadTimeout(): void {
    if (initialLoadTimeoutTimer !== null) {
      window.clearTimeout(initialLoadTimeoutTimer);
      initialLoadTimeoutTimer = null;
    }
    initialLoadTimedOut = false;
  }

  function conversationTitle(conversation: Conversation): string {
    if (isAgentDirectConversation(conversation)) {
      return agentLabel(conversationAgentForDisplay(conversation));
    }
    return conversation.title?.trim() || 'Untitled conversation';
  }

  function beginConversationLoad(): number {
    conversationLoadRequestId = nextConversationLoadId(conversationLoadRequestId);
    return conversationLoadRequestId;
  }

  function isStaleConversationLoad(requestId: number): boolean {
    return !isCurrentConversationLoad(requestId, conversationLoadRequestId);
  }

  let conversationSwitchRequestId = 0;

  function beginConversationSwitch(conversationId: string, visible: boolean): number {
    conversationSwitchRequestId = nextConversationLoadId(conversationSwitchRequestId);
    switchingConversationId = visible ? conversationId : null;
    conversationSwitchStartedAt = visible ? Date.now() : 0;
    backendConversationState = null;
    scheduleConversationSwitchTimeout(conversationSwitchRequestId);
    return conversationSwitchRequestId;
  }

  function resetConversationSwitch(): void {
    conversationSwitchRequestId = nextConversationLoadId(conversationSwitchRequestId);
    clearConversationSwitch();
  }

  function finishConversationSwitch(conversationId: string, switchRequestId: number): void {
    if (
      isCurrentConversationLoad(switchRequestId, conversationSwitchRequestId)
      && switchingConversationId === conversationId
    ) {
      clearConversationSwitch();
    }
  }

  function clearConversationSwitchTimeout(): void {
    if (conversationSwitchTimeoutTimer !== null) {
      window.clearTimeout(conversationSwitchTimeoutTimer);
      conversationSwitchTimeoutTimer = null;
    }
  }

  function clearConversationSwitch(): void {
    clearConversationSwitchTimeout();
    switchingConversationId = null;
    conversationSwitchStartedAt = 0;
  }

  function expireStaleConversationSwitch(now = Date.now()): boolean {
    const expiredConversationId = switchingConversationId;
    if (!expiredConversationId) return false;
    if (!isConversationSwitchStale({ startedAt: conversationSwitchStartedAt, now })) {
      const elapsedMs = now - conversationSwitchStartedAt;
      scheduleConversationSwitchTimeout(
        conversationSwitchRequestId,
        Math.max(1, CONVERSATION_SWITCH_TIMEOUT_MS - elapsedMs)
      );
      return false;
    }
    try {
      if (currentConversation?.conversation_id !== expiredConversationId) {
        currentConversation = null;
        activeConversationId = '';
        clearConversationViewState();
        conversationSubloadsLoading = false;
        cachedConversationRefreshing = false;
        error = 'Conversation switch is taking longer than expected. Retry or reload if it does not recover.';
        syncServiceWorkerActiveConversation(false);
      }
    } finally {
      clearConversationSwitch();
    }
    return true;
  }

  function scheduleConversationSwitchTimeout(
    switchRequestId: number,
    delayMs = CONVERSATION_SWITCH_TIMEOUT_MS
  ): void {
    clearConversationSwitchTimeout();
    if (!switchingConversationId) return;
    conversationSwitchTimeoutTimer = window.setTimeout(() => {
      conversationSwitchTimeoutTimer = null;
      if (isCurrentConversationLoad(switchRequestId, conversationSwitchRequestId)) {
        expireStaleConversationSwitch();
      }
    }, delayMs);
  }

  function conversationAgent(conversation: Conversation): Agent | undefined {
    return agents.find((agent) => agent.agent_id === conversation.agent_id);
  }

  function fallbackAgentForConversation(conversation: Conversation): Agent {
    const agentId = conversation.agent_id || 'agent';
    return {
      agent_id: agentId,
      owner_email: conversation.user_email,
      name: agentId,
      display_name: agentId,
      description: null,
      system_prompt: null,
      personality: null,
      skills: null,
      tools: null,
      permissions: null,
      llm_config: null,
      execution: null,
      personality_synced: false,
      personality_sync_error: null,
      personality_sync_checked_at: null,
      avatar_url: null,
      avatar_image_id: null,
      agent_type: 'primary',
      is_system: false,
      hidden: false,
      editable_fields: [],
      has_overrides: false,
      disabled: false,
      disableable: false,
      sync_metadata: null,
      is_shared_with_me: false,
      shared_by_email: null,
      granted_permission: null,
      executor_scope: null,
      is_readonly_for_caller: false,
      status: 'active',
      created_at: null,
      updated_at: null,
    };
  }

  function conversationAgentForDisplay(conversation: Conversation): Agent {
    return conversationAgent(conversation) ?? fallbackAgentForConversation(conversation);
  }

  async function refreshCurrentConversationMetadata(): Promise<void> {
    if (!currentConversation) return;
    const updated = await api.conversations.detail(currentConversation.conversation_id, { includeState: false });
    currentConversation = updated;
    patchConversationInList(updated.conversation_id, updated);
  }

  async function stopManagedConversation(): Promise<void> {
    if (!currentConversation || managedActionBusy) return;
    managedActionBusy = 'stop';
    managedActionError = '';
    try {
      const response = await api.conversations.managedAction(
        currentConversation.conversation_id,
        'stop',
        { reason: 'Stopped by user from managed conversation UI' },
      );
      applyManagedActionResponse(response);
      addToast(response.status === 'idle' ? 'No active managed turn to stop.' : 'Managed turn stopped.', 'success');
      await refreshCurrentConversationMetadata();
    } catch (caughtError) {
      managedActionError = asApiError(caughtError).message;
      addToast(managedActionError, 'error', 4_000, 'Unable to stop managed turn');
    } finally {
      managedActionBusy = null;
    }
  }

  async function sendManagedInstruction(): Promise<void> {
    if (!currentConversation || managedActionBusy) return;
    const message = managedInstruction.trim();
    if (!message) {
      managedActionError = 'Message is required.';
      return;
    }
    if (managedConversationActive(currentConversation)) {
      managedActionError = 'Stop the current managed turn before sending a new message.';
      return;
    }
    managedActionBusy = 'send';
    managedActionError = '';
    try {
      const response = await api.conversations.managedAction(
        currentConversation.conversation_id,
        'send',
        { message, wait: false },
      );
      applyManagedActionResponse(response);
      managedInstruction = '';
      managedInstructionOpen = false;
      addToast('Instruction sent to managed conversation.', 'success');
      await refreshCurrentConversationMetadata();
    } catch (caughtError) {
      managedActionError = asApiError(caughtError).message;
      addToast(managedActionError, 'error', 4_000, 'Unable to send instruction');
    } finally {
      managedActionBusy = null;
    }
  }

  async function takeControlOfManagedConversation(): Promise<void> {
    if (!currentConversation || managedActionBusy) return;
    if (managedConversationActive(currentConversation)) {
      managedActionError = 'Stop the current managed turn before taking control.';
      return;
    }
    const confirmed = await confirmAction({
      title: 'Take control of managed conversation?',
      message: 'The managed conversation will be closed read-only and forked into a normal conversation you can continue directly.',
      confirmLabel: 'Take control'
    });
    if (!confirmed || !currentConversation) return;

    managedActionBusy = 'take-control';
    managedActionError = '';
    try {
      const response = await api.conversations.managedAction(
        currentConversation.conversation_id,
        'take-control',
      );
      applyManagedActionResponse(response);
      const followUpConversationId = typeof response.result?.conversation_id === 'string'
        ? response.result.conversation_id
        : response.managed_agent?.follow_up_conversation_id;
      addToast('Managed conversation forked for direct control.', 'success');
      await refreshCurrentConversationMetadata();
      if (followUpConversationId) {
        await goto(conversationUrl(followUpConversationId));
      }
    } catch (caughtError) {
      managedActionError = asApiError(caughtError).message;
      addToast(managedActionError, 'error', 4_000, 'Unable to take control');
    } finally {
      managedActionBusy = null;
    }
  }

  function agentLabel(agent: Agent | undefined): string {
    return agent?.display_name ?? agent?.name ?? agent?.agent_id ?? 'Agent';
  }

  function subSessionAgent(): Agent | undefined {
    if (!subSessionId) return undefined;
    const session = sessions.find((session) => session.session_id === subSessionId);
    if (!session) return undefined;
    return agents.find((agent) => agent.agent_id === session.agent_id);
  }

  function subSessionStatus(): string | null {
    if (!subSessionId) return null;
    return sessions.find((session) => session.session_id === subSessionId)?.status ?? subSessionInfo?.status ?? null;
  }

  function shouldPollSubSession(): boolean {
    const status = subSessionStatus();
    return Boolean(subSessionPanelOpen && subSessionId && (!status || NON_TERMINAL_SESSION_STATES.has(status)));
  }

  function searchResultAgent(result: ConversationSearchMatch): Agent | undefined {
    return agents.find((agent) => agent.agent_id === result.agent_id);
  }

  function searchResultConversationTitle(result: ConversationSearchMatch): string {
    return result.conversation_title?.trim() || result.title?.trim() || 'Untitled conversation';
  }

  function searchResultSnippet(result: ConversationSearchMatch): string {
    return searchMatchSnippet(result.top_match);
  }

  function searchMatchSnippet(match: ConversationSearchMatch['top_match']): string {
    return cleanSearchSnippet(match.snippet);
  }

  function searchMatchScore(match: ConversationSearchMatch['top_match']): string {
    return `${Math.round(Math.max(0, Math.min(1, match.score)) * 100)}%`;
  }

  function searchMatchTime(match: ConversationSearchMatch['top_match']): string | null {
    if (!match.ts) return null;
    const value = Date.parse(match.ts);
    if (!Number.isFinite(value)) return null;
    return new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    }).format(value);
  }

  function clearConversationSearch(): void {
    conversationSearch = '';
    conversationSearchSubmitted = '';
    conversationSearchError = '';
    conversationSearchResults = [];
    expandedSearchSessionIds = [];
  }

  function resetConversationSearchResults(): void {
    conversationSearchSubmitted = '';
    conversationSearchError = '';
    conversationSearchResults = [];
    expandedSearchSessionIds = [];
  }

  function toggleSearchResultExpanded(sessionId: string): void {
    expandedSearchSessionIds = expandedSearchSessionIds.includes(sessionId)
      ? expandedSearchSessionIds.filter((item) => item !== sessionId)
      : [...expandedSearchSessionIds, sessionId];
  }

  function searchResultExpanded(sessionId: string): boolean {
    return expandedSearchSessionIds.includes(sessionId);
  }

  function syncSeededSearchFromUrl(): void {
    const seed = page.url.searchParams.get('search')?.trim() ?? '';
    const session = page.url.searchParams.get('searchSession');
    const ref = page.url.searchParams.get('searchRef');
    const key = `${conversationIdFromRoute() ?? ''}|${seed}|${session ?? ''}|${ref ?? ''}`;
    if (!seed) {
      seededChatSearchSession = null;
      seededChatSearchRef = null;
      lastSeededSearchKey = '';
      return;
    }
    if (key === lastSeededSearchKey) return;
    lastSeededSearchKey = key;
    chatSearchOpen = true;
    chatSearchQuery = seed;
    seededChatSearchSession = session;
    seededChatSearchRef = ref;
    seededChatSearchRan = false;
  }

  function localResultId(result: LocalChatMatch): string {
    return result.id;
  }

  async function refreshSearchHealth(): Promise<void> {
    try {
      const health = await api.search.health();
      searchEnabled = health.enabled;
    } catch {
      searchEnabled = false;
    }
  }

  async function submitConversationSearch(): Promise<void> {
    const q = conversationSearch.trim();
    conversationSearchSubmitted = q;
    conversationSearchError = '';
    conversationSearchResults = [];
    expandedSearchSessionIds = [];
    if (!q || !searchEnabled) return;
    conversationSearchLoading = true;
    try {
      const response = await api.search.conversations({
        q,
        filters: {
          agent_ids: selectedAgentIds.length > 0 ? selectedAgentIds : null,
          status: selectedConversationStatus,
          context_types: selectedChannels.length > 0 ? selectedChannels : null,
        },
        kinds: ['reasoning', 'intention', 'summary'],
        limit: 25
      });
      conversationSearchResults = response.matches;
    } catch (err) {
      conversationSearchError = err instanceof Error ? err.message : 'Search failed';
      addToast(conversationSearchError, 'error');
    } finally {
      conversationSearchLoading = false;
    }
  }

  async function openConversationSearchResult(
    result: ConversationSearchMatch,
    match: ConversationSearchMatch['top_match'] | null = result.top_match
  ): Promise<void> {
    const q = conversationSearchSubmitted || conversationSearch.trim();
    const params = new URLSearchParams();
    if (q) {
      params.set('search', q);
      params.set('searchSession', result.intaris_session_id);
      if (match?.ref_id) params.set('searchRef', match.ref_id);
    }
    closeMobileList();
    await goto(conversationUrl(result.conversation_id, params));
  }

  function scrollToTimelineItem(id: string): void {
    const total = renderableVisibleItems.length;
    const fullIndex = renderableVisibleItems.findIndex((item) => item.id === id);
    if (fullIndex >= 0 && fullIndex < visibleStartIndex) {
      const start = Math.max(0, fullIndex - 5);
      const end = Math.min(total, start + TIMELINE_WINDOW_MAX_ROWS);
      timelineWindow = { start, end: end >= total ? null : end };
    } else if (fullIndex >= 0 && fullIndex >= timelineWindowEndIndex()) {
      const end = Math.min(total, fullIndex + 6);
      const start = Math.max(0, end - TIMELINE_WINDOW_MAX_ROWS);
      timelineWindow = { start, end: end >= total ? null : end };
    }
    void tick().then(() => {
      const node = timelineEl?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(id)}"]`);
      node?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }

  function nearestMessageIdForTimestamp(timestamp: string | null): string | null {
    if (!timestamp) return null;
    const targetTime = Date.parse(timestamp);
    if (!Number.isFinite(targetTime)) return null;
    let best: { id: string; delta: number } | null = null;
    for (const item of renderableVisibleItems) {
      if (item.kind !== 'message') continue;
      const timestamp = item.created_at ?? item.updated_at ?? null;
      if (!timestamp) continue;
      const value = Date.parse(timestamp);
      if (!Number.isFinite(value)) continue;
      const delta = Math.abs(value - targetTime);
      if (best === null || delta < best.delta) best = { id: item.id, delta };
    }
    return best?.id ?? null;
  }

  function scrollToNearestTimestamp(timestamp: string | null): void {
    const targetId = nearestMessageIdForTimestamp(timestamp);
    if (targetId) scrollToTimelineItem(targetId);
  }

  function selectChatSearchResult(index: number): void {
    if (chatSearchResults.length === 0) return;
    chatSearchSelectedIndex = (index + chatSearchResults.length) % chatSearchResults.length;
    const result = chatSearchResults[chatSearchSelectedIndex];
    scrollToTimelineItem(result.targetId);
  }

  async function runChatSearch(): Promise<void> {
    const q = chatSearchQuery.trim();
    if (!q || !currentConversation) {
      chatSearchResults = [];
      return;
    }
    const conversationId = currentConversation.conversation_id;
    const local = findLocalChatMatches(renderableVisibleItems, q).map((match) => ({
      source: 'local' as const,
      local: match,
      targetId: localResultId(match)
    }));
    chatSearchLoading = true;
    try {
      const response = searchEnabled
        ? await api.search.conversation(conversationId, {
            q,
            kinds: ['reasoning', 'intention', 'summary'],
            limit: 50
          })
        : { matches: [] };
      const server = response.matches.map((match) => ({
        source: 'server' as const,
        server: match,
        targetId: findVisibleServerSearchTarget(renderableVisibleItems, match) ?? serverSearchFallbackTargetId(match)
      }));
      const rawResults = [
        ...local,
        ...server
      ];
      chatSearchResults = mergeSearchResultsByTarget(rawResults);
      let seededIndex = -1;
      if (seededChatSearchSession && seededChatSearchRef) {
        const seededRaw = rawResults.find((result) => result.source === 'server' && (
          result.server.intaris_session_id === seededChatSearchSession || result.server.session_id === seededChatSearchSession
        ) && result.server.match.ref_id === seededChatSearchRef);
        seededIndex = seededRaw
          ? chatSearchResults.findIndex((result) => result.targetId === seededRaw.targetId)
          : -1;
      }
      if (seededChatSearchSession && seededIndex < 0) {
        const seededRaw = rawResults.find((result) => result.source === 'server' && (
          result.server.intaris_session_id === seededChatSearchSession || result.server.session_id === seededChatSearchSession
        ));
        seededIndex = seededRaw
          ? chatSearchResults.findIndex((result) => result.targetId === seededRaw.targetId)
          : -1;
      }
      chatSearchSelectedIndex = seededIndex >= 0 ? seededIndex : 0;
      lastChatSearchConversationId = conversationId;
      if (chatSearchResults.length > 0) selectChatSearchResult(chatSearchSelectedIndex);
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Conversation search failed', 'error');
      chatSearchResults = local;
    } finally {
      chatSearchLoading = false;
    }
  }

  function openChatSearch(seed = ''): void {
    chatSearchOpen = true;
    seededChatSearchSession = null;
    seededChatSearchRef = null;
    if (seed) chatSearchQuery = seed;
    seededChatSearchRan = true;
    void tick().then(() => void runChatSearch());
  }

  function openCurrentConversationInSeparateWindow(): void {
    if (!currentConversation || typeof window === 'undefined') return;
    const width = Math.min(980, Math.max(720, Math.round(window.screen.availWidth * 0.62)));
    const height = Math.min(1100, Math.max(760, Math.round(window.screen.availHeight * 0.86)));
    const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - width) / 2));
    const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - height) / 2));
    const features = [
      'popup=yes',
      'noopener',
      'noreferrer',
      `width=${width}`,
      `height=${height}`,
      `left=${left}`,
      `top=${top}`,
      'menubar=no',
      'toolbar=no',
      'location=no',
      'status=no',
      'scrollbars=yes',
      'resizable=yes'
    ].join(',');
    const params = new URLSearchParams({ window: '1' });
    const child = window.open(conversationUrl(currentConversation.conversation_id, params), `cognis-chat-${currentConversation.conversation_id}`, features);
    if (!child) {
      addToast('This device does not support separate chat windows.', 'info', 3_000);
    }
  }

  async function toggleConversationStar(conversation: Conversation): Promise<void> {
    if (starringConversationId) return;
    const conversationId = conversation.conversation_id;
    const previousConversation = { ...conversation };
    const previousStarredAt = conversation.starred_at ?? null;
    const nextStarredAt = previousStarredAt ? null : new Date().toISOString();
    starringConversationId = conversationId;
    patchConversationInList(conversationId, { starred_at: nextStarredAt });
    try {
      const updated = await api.conversations.update(conversationId, { starred_at: nextStarredAt });
      mergeConversationList([updated]);
      if (currentConversation?.conversation_id === conversationId) {
        currentConversation = updated;
      }
      if (selectedConversationStatus === 'starred' && !updated.starred_at) {
        conversations = conversations.filter((item) => item.conversation_id !== conversationId);
        rememberSidebarProjection();
      }
    } catch (caughtError) {
      if (selectedConversationStatus === 'starred' && previousStarredAt) {
        mergeConversationList([{ ...previousConversation, starred_at: previousStarredAt }]);
      } else {
        patchConversationInList(conversationId, { starred_at: previousStarredAt });
      }
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to update star');
    } finally {
      starringConversationId = null;
    }
  }

  // Display name of the current conversation's agent for composer placeholders
  // and any other in-page prompts. Falls back to "Cognis" only while the
  // conversation and agent list have not been resolved yet.
  const currentAgentDisplayName = $derived.by(() => {
    if (!currentConversation) return 'Cognis';
    const agent = conversationAgent(currentConversation);
    return agent?.display_name ?? agent?.name ?? currentConversation.agent_id ?? 'Cognis';
  });

  const persistentChatMode = $derived.by(() => {
    const value = currentConversation?.context?.platform_data?.chat_mode;
    return value === 'plan' || value === 'build' ? value : 'default';
  });

  function conversationChatMode(conversation: Conversation): 'default' | 'plan' | 'build' {
    const explicitMode = conversation.context?.platform_data?.chat_mode
      ?? conversation.context?.platform_data?.chatMode
      ?? conversation.context?.platform_data?.mode;
    let fallbackMode: unknown = explicitMode;
    if (fallbackMode !== 'plan' && fallbackMode !== 'build') {
      const execution = conversationAgent(conversation)?.execution;
      const agentDefaultMode = execution && typeof execution === 'object'
        ? (execution.default_chat_mode ?? execution.defaultChatMode ?? execution.chat_mode)
        : null;
      fallbackMode = agentDefaultMode;
    }
    return conversationTurnModeTone(conversation, fallbackMode);
  }

  function turnOrbitClass(mode: 'default' | 'plan' | 'build'): string {
    if (mode === 'plan') {
      return 'conversation-turn-orbit--plan';
    }
    if (mode === 'build') {
      return 'conversation-turn-orbit--build';
    }
    return '';
  }

  function conversationOrbitClass(conversation: Conversation, mode: 'default' | 'plan' | 'build'): string {
    const attentionClass = conversationAttentionOrbitClass(conversationAttentionTone(conversation));
    return attentionClass || turnOrbitClass(mode);
  }

  function conversationDotClass(conversation: Conversation): string {
    return conversationAttentionDotClass(conversationAttentionTone(conversation));
  }

  function conversationAttentionDescription(conversation: Conversation): string {
    return conversationAttentionLabel(conversationAttentionTone(conversation));
  }

  function syncActiveSessionAttention(conversationId: string | null | undefined, sessionList: Session[]): void {
    if (!conversationId) return;
    const conversation = conversations.find((candidate) => candidate.conversation_id === conversationId)
      ?? agentDirectChats.find((item) => item.conversation.conversation_id === conversationId)?.conversation
      ?? (currentConversation?.conversation_id === conversationId ? currentConversation : null);
    if (!conversation?.active_session_id) return;
    const activeSession = sessionList.find((session) => session.session_id === conversation.active_session_id);
    if (!activeSession) return;
    patchConversationInList(conversationId, {
      active_session_status: activeSession.status,
      active_session_completion_reason: activeSession.completion_reason,
    });
  }

  function patchConversationPendingNotificationType(
    conversationId: string | null | undefined,
    notificationType: string,
    present: boolean
  ): void {
    if (!conversationId) return;
    const conversation = conversations.find((candidate) => candidate.conversation_id === conversationId)
      ?? agentDirectChats.find((item) => item.conversation.conversation_id === conversationId)?.conversation
      ?? (currentConversation?.conversation_id === conversationId ? currentConversation : null);
    if (!conversation) {
      if (present) {
        patchConversationInList(
          conversationId,
          { pending_notification_types: [notificationType] },
          { pendingNotificationSource: 'server' },
        );
      }
      return;
    }
    const currentTypes = conversation.pending_notification_types ?? [];
    const nextTypes = present
      ? Array.from(new Set([...currentTypes, notificationType]))
      : currentTypes.filter((type) => type !== notificationType);
    patchConversationInList(
      conversationId,
      { pending_notification_types: nextTypes },
      { pendingNotificationSource: 'server' },
    );
  }

  function refreshConversationPendingNotificationTypes(conversationId: string | null | undefined): void {
    if (!conversationId) return;
    const requestEpoch = beginPendingNotificationRefresh(conversationId);
    void api.notifications
      .list(conversationId)
      .then((notifications) => {
        if (!shouldApplyPendingNotificationRefresh({
          requestEpoch,
          currentEpoch: pendingNotificationEpochByConversation.get(conversationId),
        })) {
          return;
        }
        patchConversationInList(
          conversationId,
          { pending_notification_types: pendingNotificationTypesFromNotifications(notifications) },
          { pendingNotificationSource: 'refetch' },
        );
      })
      .catch(() => {});
  }

  function notificationTypeForEvent(eventType: string): string | null {
    if (eventType === 'workflow_gate' || eventType === 'workflow_gate_resolved') return 'gate';
    if (eventType === 'workflow_step_question' || eventType === 'workflow_step_question_resolved') return 'step_question';
    if (eventType === 'auth_challenge' || eventType === 'auth_challenge_resolved') return 'auth_challenge';
    if (eventType === 'credential_request' || eventType === 'credential_request_resolved') return 'credential_request';
    if (eventType === 'escalation' || eventType === 'escalation_resolved') return 'escalation';
    return null;
  }

  // Enable the composer's send button only when there is something to send
  // and the conversation is usable. Computed once so the template doesn't
  // duplicate the disabled checks that previously sat on the Send button.
  const canSendNow = $derived(
    (composer.trim().length > 0 || composerAttachments.length > 0) &&
      currentConversation !== null &&
      !isReadOnly(currentConversation) &&
      !isLlmUnavailableForSetup() &&
      !directQuestionSubmitting,
  );

  async function refreshSidebarData(): Promise<void> {
    await loadSidebarProjection();
  }

  async function resyncSidebarData(reason: string, options: { force?: boolean } = {}): Promise<void> {
    if (
      !options.force &&
      shouldDebounceSidebarResync({
        lastSuccessfulSyncAt: lastSidebarSyncAt,
        now: Date.now(),
      })
    ) {
      return;
    }
    const filters = {
      contextTypes: selectedChannels.length > 0 ? selectedChannels : null,
      agentIds: selectedAgentIds.length > 0 ? selectedAgentIds : null,
      status: selectedConversationStatus,
    };
    try {
      const isDeltaRequest = Boolean(lastSidebarSyncTimestamp);
      const response = isDeltaRequest
        ? await api.conversations.sidebar(null, filters, { changedSince: lastSidebarSyncTimestamp })
        : await api.conversations.sidebar(null, filters);
      if (isDeltaRequest && (response.agents.length > 0 || response.context_types.length > 0)) {
        applySidebarProjection(response, { reset: true });
        rememberSidebarProjection();
        return;
      }
      if (response.full_resync_required) {
        await loadSidebarProjection();
        return;
      }
      applySidebarProjection(response, { reset: !isDeltaRequest });
      rememberSidebarProjection();
    } catch (error) {
      console.debug('Sidebar resync failed', { reason, error });
      await loadSidebarProjection();
    }
  }

  async function forceRefreshConversationHistory(): Promise<void> {
    if (conversationListRefreshing) return;
    conversationListRefreshing = true;
    resetConversationSearchResults();
    try {
      await refreshSidebarData();
    } finally {
      conversationListRefreshing = false;
    }
  }

  async function refreshAvailableChannelTypes(): Promise<void> {
    const contextTypes = await api.conversations.contextTypes({
      status: selectedConversationStatus
    });
    applyAvailableChannelTypes(contextTypes);
  }

  function resetSessionFilter(): void {
    sessionIds.clear();
    for (const session of sessions) {
      sessionIds.add(session.session_id);
      if (session.intaris_session_id) {
        sessionIds.add(session.intaris_session_id);
      }
    }
  }

  /** Seed escalation list from Intaris on conversation open (catch up). */
  function applyEscalationNotifications(notifications: Notification[], taskId: string | null): void {
    const filtered = notifications
      .filter((item) => item.notification_type === 'escalation')
      .filter((item) => (taskId ? item.task_id === taskId : true))
      .map((item) => ({
        call_id: item.notification_id,
        session_id: item.session_id,
        tool_name: typeof item.payload.tool_name === 'string' ? item.payload.tool_name : null,
        decision: 'escalate',
        resolved: false,
        reasoning: typeof item.payload.reasoning === 'string' ? item.payload.reasoning : null,
        risk: typeof item.payload.risk === 'string' ? item.payload.risk : null,
        timeout_seconds:
          typeof item.payload.timeout_seconds === 'number'
            ? item.payload.timeout_seconds
            : escalationTimeoutSeconds,
        received_at: item.created_at ? Date.parse(item.created_at) : Date.now()
      }) satisfies Escalation);
    const pendingEscalations = filtered.filter((item) => !isEscalationExpired(item));
    for (const item of pendingEscalations) {
      if (!escalations.some((e) => e.call_id === item.call_id)) {
        item.timeout_seconds = item.timeout_seconds ?? escalationTimeoutSeconds;
      }
    }
    const pendingStillExists = escalationResolutionPending
      ? pendingEscalations.some((item) => item.call_id === escalationResolutionPending?.call_id)
      : false;
    if (escalationResolutionPending && !pendingStillExists) {
      if (escalationBusyCallId === escalationResolutionPending.call_id) {
        escalationBusyCallId = null;
      }
      escalationResolutionPending = null;
    }
    escalations = sortEscalations(
      escalationResolutionPending
        ? pendingEscalations.filter((item) => item.call_id !== escalationResolutionPending?.call_id)
        : pendingEscalations
    );
    escalationError = '';
    startEscalationCountdown();
  }

  function applyPendingDirectQuestionNotifications(notifications: Notification[]): void {
    const pendingStepNotifications = notifications.filter(
      (item) => ['step_question', 'auth_challenge'].includes(item.notification_type) && item.status === 'pending',
    );
    pendingCredentialRequest = notifications.find(
      (item) => item.notification_type === 'credential_request' && item.status === 'pending',
    ) ?? null;
    // Send routing resolves the notification id from pendingDirectQuestion
    // or a fresh notifications fetch (resolveDirectQuestionNotificationId),
    // so no per-tool annotation on the canonical timeline is required.
    const directQuestion = pendingStepNotifications
      .filter((item) => item.task_id === null)
      .map((item) => pendingDirectQuestionFromNotification(item))
      .find((item): item is PendingDirectQuestion => item !== null) ?? null;
    if (shouldResetPendingDirectQuestionForm(
      pendingDirectQuestion?.notificationId,
      directQuestion?.notificationId,
    )) {
      directQuestionAnswers = restoreDirectQuestionDraft(directQuestion);
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
    }
    pendingDirectQuestion = directQuestion;
    if (directQuestion === null) {
      directQuestionSubmitting = false;
      directQuestionAnswers = {};
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
    }
  }

  async function refreshEscalations(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    try {
      const taskId = conversationTaskId(currentConversation);
      const notifications = taskId
        ? await api.notifications.list(null, { taskId })
        : await api.notifications.list(currentConversation.conversation_id);
      applyEscalationNotifications(notifications, taskId);
      escalationError = '';
    } catch (caughtError) {
      escalationError = asApiError(caughtError).message;
      markControllerUnavailable(caughtError);
    }
  }

  async function refreshPendingDirectQuestion(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    try {
      applyPendingDirectQuestionNotifications(await api.notifications.list(currentConversation.conversation_id));
    } catch {
      // Ignore notification refresh failures here — they should not block chat.
    }
  }

  async function refreshPendingNotifications(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    try {
      const taskId = conversationTaskId(currentConversation);
      const conversationNotifications = await api.notifications.list(currentConversation.conversation_id);
      const escalationNotifications = taskId
        ? await api.notifications.list(null, { taskId })
        : conversationNotifications;
      applyEscalationNotifications(escalationNotifications, taskId);
      applyPendingDirectQuestionNotifications(conversationNotifications);
    } catch (caughtError) {
      escalationError = asApiError(caughtError).message;
      markControllerUnavailable(caughtError);
    }
  }

  function clearControllerRecovery(): void {
    if (controllerRecoveryTimer !== null && typeof window !== 'undefined') {
      window.clearTimeout(controllerRecoveryTimer);
    }
    controllerRecoveryTimer = null;
    controllerRecoveryAttempts = 0;
    controllerRecoveryConversationId = null;
    controllerRecoveryPending = false;
  }

  function markControllerUnavailable(caughtError: unknown, conversationId?: string): boolean {
    if (asApiError(caughtError).status !== 503) return false;
    controllerRecoveryConversationId = conversationId
      ?? currentConversation?.conversation_id
      ?? conversationIdFromRoute()
      ?? null;
    controllerRecoveryPending = true;
    scheduleControllerRecovery();
    return true;
  }

  function scheduleControllerRecovery(): void {
    if (
      typeof window === 'undefined'
      || controllerRecoveryTimer !== null
      || !controllerRecoveryConversationId
      || document.hidden
      || !shouldContinueControllerRecovery(controllerRecoveryAttempts)
    ) {
      return;
    }
    const conversationId = controllerRecoveryConversationId;
    const delayMs = nextControllerRecoveryDelayMs(controllerRecoveryAttempts);
    controllerRecoveryAttempts += 1;
    controllerRecoveryTimer = window.setTimeout(() => {
      controllerRecoveryTimer = null;
      void (async () => {
        await retryControllerRecovery(conversationId);
        if (controllerRecoveryPending && (sessionsError || historyError || escalationError)) {
          scheduleControllerRecovery();
        } else {
          clearControllerRecovery();
        }
      })();
    }, delayMs);
  }

  async function retryControllerRecovery(conversationId: string): Promise<void> {
    if (currentConversation?.conversation_id !== conversationId) {
      await openConversation(conversationId);
      return;
    }
    await retryConversationSubloads();
    await refreshEscalations();
  }

  async function retryControllerRecoveryNow(): Promise<void> {
    const conversationId = controllerRecoveryConversationId ?? conversationIdFromRoute();
    if (!conversationId) return;
    controllerRecoveryAttempts = 0;
    await retryControllerRecovery(conversationId);
    if (sessionsError || historyError || escalationError) {
      scheduleControllerRecovery();
    } else {
      clearControllerRecovery();
    }
  }

  function resumeControllerRecoveryIfPending(): void {
    if (controllerRecoveryPending) {
      scheduleControllerRecovery();
    }
  }

  function startEscalationCountdown(): void {
    stopEscalationCountdown();
    if (escalations.length === 0) return;
    escalationCountdownTimer = window.setInterval(() => {
      // Force reactivity so countdown timers re-render
      escalations = [...escalations];
      escalations = escalations.filter((item) => !isEscalationExpired(item));
      if (escalations.length === 0) stopEscalationCountdown();
    }, 1000);
  }

  function stopEscalationCountdown(): void {
    if (escalationCountdownTimer !== null) {
      window.clearInterval(escalationCountdownTimer);
      escalationCountdownTimer = null;
    }
  }

  function stopNotificationRefreshPolling(): void {
    if (notificationRefreshTimer !== null) {
      window.clearInterval(notificationRefreshTimer);
      notificationRefreshTimer = null;
    }
  }

  function startNotificationRefreshPolling(): void {
    stopNotificationRefreshPolling();
    notificationRefreshTimer = window.setInterval(() => {
      if (document.hidden || !currentConversation) return;
      void refreshPendingNotifications();
    }, 30000);
  }

  function syncVisibleWindow(): void {
    // Window over renderable rows only (matches displayedTimeline slicing).
    // Live tail: no upper bound, start a target-sized page from the end.
    timelineWindow = {
      start: Math.max(0, renderableVisibleItems.length - TIMELINE_WINDOW_TARGET_ROWS),
      end: null,
    };
  }

  function timelineWindowEndIndex(total = renderableVisibleItems.length): number {
    return timelineWindowEnd(timelineWindow, total);
  }

  function clampVisibleWindow(total = renderableVisibleItems.length): void {
    timelineWindow = clampWindow(timelineWindow, total);
  }

  function visibleWindowSize(total = renderableVisibleItems.length): number {
    return timelineWindowSize(timelineWindow, total);
  }

  function shrinkTailWindowIfPinned(): void {
    const total = renderableVisibleItems.length;
    if (userScrolledUp) {
      // Freeze the tail so items arriving while the user reads older history do
      // not remount the tail. freezeTailWindow anchors to the prior count, so
      // appends are hidden; prepends are absorbed by windowAfterPrepend before
      // this runs (never an index freeze under a prepend).
      timelineWindow = freezeTailWindow(timelineWindow, total, lastRenderableVisibleCount);
      return;
    }
    if (timelineWindow.end !== null || visibleWindowSize(total) > TIMELINE_WINDOW_TARGET_ROWS) {
      syncVisibleWindow();
    } else {
      clampVisibleWindow(total);
    }
  }

  function timelineDistanceFromBottom(): number {
    if (!timelineEl) return 0;
    return distanceFromScrollBottom({
      scrollHeight: timelineEl.scrollHeight,
      scrollTop: timelineEl.scrollTop,
      clientHeight: timelineEl.clientHeight,
    });
  }

  function markUserScrollIntentUp(): void {
    if (!timelineEl) return;
    userScrolledUp = true;
    userScrollIntentUp = true;
    tailPinned = false;
  }

  function clearUserScrollIntentSoon(): void {
    // 300ms covers typical trackpad inertia/momentum scroll bursts. The
    // previous 120ms window was too short: sparse wheel events during a
    // momentum scroll could let the intent expire between notches, leaving
    // only userScrolledUp as the "scrolling up" signal — which the near-bottom
    // branch could then clear on a reflow-induced position change.
    window.setTimeout(() => {
      userScrollIntentUp = false;
    }, 300);
  }

  function scheduleScrollToBottom(force = false, frames = 2): void {
    if (frames <= 0) {
      scrollToBottom(force);
      return;
    }
    requestAnimationFrame(() => scheduleScrollToBottom(force, frames - 1));
  }

  function scrollToBottom(force = false): void {
    // Idempotent auto-scroll: if the user is pinned to the tail (or force),
    // re-pin to scrollHeight. Never infer userScrolledUp from a distance
    // measurement — only genuine user gestures (handleTimelineScroll,
    // wheel/touch/key intent) may un-pin. This is the baseline behavior that
    // correctly handles messages taller than the viewport: as streaming content
    // grows, each call simply re-pins to the new scrollHeight without any
    // position-gate that could race the DOM growth and falsely dismount the tail.
    if (!timelineEl) return;
    if (!force && userScrolledUp) return;
    if (force) {
      userScrolledUp = false;
      tailPinned = true;
    }
    pendingBottomScrollForce = pendingBottomScrollForce || force;
    if (bottomScrollPending) return;
    bottomScrollPending = true;
    requestAnimationFrame(() => {
      const shouldForce = pendingBottomScrollForce;
      pendingBottomScrollForce = false;
      bottomScrollPending = false;
      if (!shouldForce && userScrolledUp) return;
      if (shouldForce) {
        userScrolledUp = false;
        tailPinned = true;
      }
      if (timelineEl) {
        programmaticScroll = true;
        timelineEl.scrollTop = timelineEl.scrollHeight;
        lastTimelineScrollTop = timelineEl.scrollTop;
        // Record the exact scrollTop we wrote so handleTimelineScroll can
        // suppress the async scroll event the browser delivers for this write.
        // The event is dispatched on the next animation frame (not synchronously),
        // so it can arrive after programmaticScroll has been cleared — without
        // this guard it would be processed as a user gesture.
        programmaticScrollTarget = timelineEl.scrollTop;
        tailPinned = true;
      }
      requestAnimationFrame(() => {
        programmaticScroll = false;
        if (timelineEl) {
          lastTimelineScrollTop = timelineEl.scrollTop;
        }
        // Keep the target alive for one more frame to catch late-delivered
        // scroll events, then clear it.
        requestAnimationFrame(() => {
          programmaticScrollTarget = null;
        });
      });
    });
  }

  /**
   * Write a programmatic scrollTop value with the same guards scrollToBottom
   * uses: the boolean flag for same-frame scroll events plus the written-value
   * target for the async scroll event the browser delivers on a later frame.
   * Every programmatic scrollTop write on the timeline MUST go through this
   * (or scrollToBottom) — an unguarded write is processed by
   * handleTimelineScroll as a user gesture and corrupts the scroll state.
   */
  interface TimelineScrollAnchor {
    key: string;
    top: number;
    scrollTop: number;
  }

  /**
   * Capture an element-anchored scroll reference: the first row whose top edge
   * is at or below the viewport top, plus its viewport-relative top offset.
   * Element anchoring survives simultaneous add-above / remove-below commits
   * (the DOM-window prepend + tail-cut case) that a scrollHeight delta cannot.
   */
  function captureTimelineAnchor(): TimelineScrollAnchor | null {
    if (!timelineEl) return null;
    const viewportTop = timelineEl.getBoundingClientRect().top;
    const rows = timelineEl.querySelectorAll<HTMLElement>('[data-timeline-row-key]');
    for (const row of rows) {
      const key = row.getAttribute('data-timeline-row-key');
      if (!key) continue;
      const top = row.getBoundingClientRect().top - viewportTop;
      // First row whose top is within (or just above) the viewport top edge.
      if (top >= -1) {
        return { key, top, scrollTop: timelineEl.scrollTop };
      }
    }
    return null;
  }

  function restoreTimelineAnchor(anchor: TimelineScrollAnchor | null): void {
    if (!timelineEl || !anchor) return;
    const viewportTop = timelineEl.getBoundingClientRect().top;
    const row = timelineEl.querySelector<HTMLElement>(
      `[data-timeline-row-key="${cssEscape(anchor.key)}"]`
    );
    if (!row) return;
    const topAfter = row.getBoundingClientRect().top - viewportTop;
    const target = anchoredScrollTop({
      currentScrollTop: timelineEl.scrollTop,
      anchorTopBefore: anchor.top,
      anchorTopAfter: topAfter,
    });
    if (Math.abs(target - timelineEl.scrollTop) < 1) return;
    writeProgrammaticScrollTop(target);
  }

  function cssEscape(value: string): string {
    if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
      return CSS.escape(value);
    }
    return value.replace(/["\\]/g, '\\$&');
  }

  function writeProgrammaticScrollTop(value: number): void {
    if (!timelineEl) return;
    programmaticScroll = true;
    timelineEl.scrollTop = value;
    programmaticScrollTarget = timelineEl.scrollTop;
    lastTimelineScrollTop = timelineEl.scrollTop;
    requestAnimationFrame(() => {
      programmaticScroll = false;
      requestAnimationFrame(() => {
        programmaticScrollTarget = null;
      });
    });
  }

  function handleTimelineScroll(): void {
    if (!timelineEl || programmaticScroll) return;
    const currentScrollTop = timelineEl.scrollTop;
    // Suppress scroll events whose position matches a recent programmatic write.
    // The browser delivers the scroll event for a scrollTop assignment
    // asynchronously (on the next animation frame), so it can arrive after
    // programmaticScroll has been cleared. Without this guard, the event would
    // be processed as a user gesture and could falsely clear userScrolledUp.
    if (programmaticScrollTarget !== null && currentScrollTop === programmaticScrollTarget) {
      lastTimelineScrollTop = currentScrollTop;
      return;
    }
    const distanceFromBottom = timelineDistanceFromBottom();
    const hasHiddenNewerRows = timelineWindowEndIndex() < renderableVisibleItems.length;
    const scrollStateDistanceFromBottom = hasHiddenNewerRows
      ? CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX + 1
      : distanceFromBottom;

    const nextState = nextChatScrollState({
      currentScrollTop,
      lastScrollTop: lastTimelineScrollTop,
      distanceFromBottom: scrollStateDistanceFromBottom,
      userScrolledUp,
      userScrollIntentUp,
      bottomThresholdPx: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
      scrollDeltaThresholdPx: CHAT_USER_SCROLL_DELTA_THRESHOLD_PX,
    });
    userScrolledUp = nextState.userScrolledUp;
    // Keep tailPinned as a strict mirror of !userScrolledUp so there is a
    // single source of truth. Previously tailPinned could diverge from
    // userScrolledUp, causing the ResizeObserver and shouldPreserveLiveTailOnResize
    // to use stale state.
    tailPinned = !userScrolledUp;
    if (!hasHiddenNewerRows && isNearScrollBottom(nextState.distanceFromBottom, CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX)) {
      userScrollIntentUp = false;
      lastTimelineTouchY = null;
    }

    lastTimelineScrollTop = currentScrollTop;

    if (currentScrollTop <= 24 && (visibleStartIndex > 0 || hasOlderMessages)) {
      void loadOlder();
    }

    // Downward re-expansion: when the user scrolls back toward the tail and
    // newer rows are still unmounted, grow the window so they remount in place.
    // Without this the tail stays hidden until an explicit jump-to-bottom.
    if (hasHiddenNewerRows && distanceFromBottom <= CHAT_TIMELINE_EXPAND_DOWN_THRESHOLD_PX) {
      const anchor = captureTimelineAnchor();
      timelineWindow = expandWindowDown(timelineWindow, renderableVisibleItems.length);
      void tick().then(() => restoreTimelineAnchor(anchor));
    }
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    userScrollIntentUp = false;
    lastTimelineTouchY = null;
    tailPinned = true;
    syncVisibleWindow();
    void tick().then(() => scrollToBottom(true));
  }

  function jumpToActiveTimelineStart(request: { scrollTop: number; rowKey: string | null }): void {
    if (!timelineEl) return;
    // Treat contextual navigation as explicit upward intent and update every
    // live-tail guard before the programmatic write. Resize/stream growth then
    // preserves this reading position instead of scheduling a bottom re-pin.
    userScrolledUp = true;
    userScrollIntentUp = true;
    tailPinned = false;
    bottomScrollPending = false;
    pendingBottomScrollForce = false;
    writeProgrammaticScrollTop(request.scrollTop);
  }

  function handleTimelineWheel(event: WheelEvent): void {
    if (event.deltaY < -CHAT_USER_SCROLL_DELTA_THRESHOLD_PX) {
      markUserScrollIntentUp();
      clearUserScrollIntentSoon();
    }
  }

  function handleTimelineTouchStart(event: TouchEvent): void {
    lastTimelineTouchY = event.touches[0]?.clientY ?? null;
  }

  function handleTimelineTouchMove(event: TouchEvent): void {
    const currentY = event.touches[0]?.clientY;
    if (currentY == null || lastTimelineTouchY == null) return;
    if (currentY > lastTimelineTouchY + CHAT_USER_SCROLL_DELTA_THRESHOLD_PX) {
      markUserScrollIntentUp();
      clearUserScrollIntentSoon();
    }
    lastTimelineTouchY = currentY;
  }

  function handleTimelineTouchEnd(): void {
    lastTimelineTouchY = null;
    clearUserScrollIntentSoon();
  }

  function handleTimelineKeydown(event: KeyboardEvent): void {
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) {
      markUserScrollIntentUp();
      clearUserScrollIntentSoon();
    }
  }

  function channelTypes(): string[] {
    return availableChannelTypes;
  }

  function channelDisplayLabel(channel: string): string {
    if (channel === 'agent_work' || channel === 'managed_agent_conversation') return 'Agent work';
    return channel.charAt(0).toUpperCase() + channel.slice(1);
  }

  function selectedChannelFilterLabel(): string {
    if (selectedChannels.length === 0) return 'All channels';
    const labels = selectedChannels.map(channelDisplayLabel);
    if (labels.length <= 2) return labels.join(' + ');
    return `${labels.slice(0, 2).join(' + ')} +${labels.length - 2}`;
  }

  function activePrimaryAgents(): Agent[] {
    return agents.filter((agent) => agent.status === 'active' && agent.agent_type === 'primary');
  }

  function selectedAgentFilterLabel(): string {
    if (selectedAgentIds.length === 0) return 'All agents';
    const selectedAgents = activePrimaryAgents().filter((agent) => selectedAgentIds.includes(agent.agent_id));
    const labels = selectedAgents.map((agent) => agent.display_name ?? agent.name ?? agent.agent_id);
    const fallbackCount = selectedAgentIds.length - labels.length;
    for (let index = 0; index < fallbackCount; index += 1) labels.push('Unknown agent');
    if (labels.length <= 2) return labels.join(' + ');
    return `${labels.slice(0, 2).join(' + ')} +${labels.length - 2}`;
  }

  function parseStoredMultiFilter(stored: string | null): string[] {
    if (!stored || stored === 'all') return [];
    try {
      const parsed = JSON.parse(stored);
      if (Array.isArray(parsed)) {
        return parsed.filter((item): item is string => typeof item === 'string' && item.length > 0);
      }
    } catch {
      // Backwards-compatible single-value storage from the old select.
    }
    return [stored];
  }

  async function persistSelectedChannels(): Promise<void> {
    if (typeof window === 'undefined') return;
    if (selectedChannels.length === 0) {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.selectedChannel);
    } else {
      window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedChannel, JSON.stringify(selectedChannels));
    }
    resetConversationSearchResults();
    await Promise.all([loadAgentDirectChats(), loadConversationPage(true)]);
  }

  function restoreSelectedChannel(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedChannel);
    selectedChannels = parseStoredMultiFilter(stored);
  }

  async function toggleSelectedChannel(channel: string): Promise<void> {
    selectedChannels = selectedChannels.includes(channel)
      ? selectedChannels.filter((item) => item !== channel)
      : [...selectedChannels, channel];
    await persistSelectedChannels();
  }

  async function toggleSelectedAgent(agentId: string): Promise<void> {
    selectedAgentIds = selectedAgentIds.includes(agentId)
      ? selectedAgentIds.filter((item) => item !== agentId)
      : [...selectedAgentIds, agentId];
    await handleAgentFilterChange();
  }

  function restoreChatSidebarState(): void {
    if (typeof window === 'undefined') return;
    if (isWindowMode) {
      chatSidebarCollapsed = true;
      return;
    }
    chatSidebarCollapsed = window.localStorage.getItem(CHAT_STORAGE_KEYS.sidebarCollapsed) === '1';
  }

  function toggleChatSidebar(): void {
    if (isWindowMode) return;
    chatSidebarCollapsed = !chatSidebarCollapsed;
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(CHAT_STORAGE_KEYS.sidebarCollapsed, chatSidebarCollapsed ? '1' : '0');
    }
  }

  function openMobileList(): void {
    if (isWindowMode) return;
    mobileDrawerPreviouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    mobileListOpen = true;
  }

  // Edge-swipe handlers for the chat detail viewport. The gestures
  // cascade so users can stack drawers without lifting their finger:
  //   * Left swipe with nothing open → open the conversation list.
  //   * Left swipe with the list open → open the global nav menu on
  //     top of the list.
  //   * Right swipe with the global nav open → no-op here; the layout
  //     closes the nav (its right-edge handler is enabled via the
  //     shared `mobileNavOpenStore`).
  //   * Right swipe with the list open → close the list.
  //   * Right swipe with nothing open → no-op (we used to toggle the
  //     conversation info panel, but the user wants right-edge to
  //     mean "close" exclusively).
  // Desktop (>=lg) bypasses both handlers because the conversation
  // list is permanently visible there.
  function handleChatLeftEdgeSwipe(): void {
    if (isWindowMode) return;
    if (!isMobileViewport()) return;
    if (get(mobileNavOpenStore)) return; // already at the top of the stack
    if (mobileListOpen) {
      requestOpenMobileNav();
      return;
    }
    openMobileList();
  }

  function handleChatRightEdgeSwipe(): void {
    if (isWindowMode) return;
    if (!isMobileViewport()) return;
    if (get(mobileNavOpenStore)) return; // layout handler closes the nav
    if (mobileListOpen) {
      closeMobileList();
    }
  }

  function closeMobileList(): void {
    mobileListOpen = false;
    mobileDrawerPreviouslyFocused?.focus();
    mobileDrawerPreviouslyFocused = null;
  }

  /**
   * Intaris stores session intention either as a plain string or as a
   * JSON object with at least `{intention, title?}`. The backend ships
   * it through to the UI verbatim, which showed up as raw JSON in the
   * Info panel. Pick the most useful display form:
   *   - string: use it directly
   *   - object with `intention`: use that field (drop surrounding
   *     structure)
   *   - fallback: pretty-print the JSON so at least it's readable
   */
  function extractIntentionDisplay(raw: unknown): { title: string | null; intention: string | null } {
    if (raw == null) return { title: null, intention: null };
    if (typeof raw !== 'string') {
      if (typeof raw === 'object') {
        const obj = raw as Record<string, unknown>;
        const intention = typeof obj.intention === 'string' ? obj.intention : null;
        const title = typeof obj.title === 'string' ? obj.title : null;
        return { title, intention };
      }
      return { title: null, intention: String(raw) };
    }
    const trimmed = raw.trim();
    if (!trimmed) return { title: null, intention: null };
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          const intention = typeof parsed.intention === 'string' ? parsed.intention : null;
          const title = typeof parsed.title === 'string' ? parsed.title : null;
          if (intention || title) {
            return { title, intention };
          }
          return { title: null, intention: JSON.stringify(parsed, null, 2) };
        }
      } catch {
        // fall through to raw display
      }
    }
    return { title: null, intention: trimmed };
  }

  async function loadSessionInfo(): Promise<void> {
    const conversationId = currentConversation?.conversation_id;
    const sid = currentConversation?.active_session_id;
    if (!conversationId || !sid) {
      invalidateSessionInfo();
      return;
    }
    const requestId = nextSessionInfoRequestId();
    sessionInfoLoading = true;
    sessionInfo = null;
    try {
      const detail = await api.sessions.intarisDetail(sid);
      if (isStaleSessionInfoLoad(requestId, conversationId, sid)) {
        return;
      }
      sessionInfo = {
        intaris_session_id: detail.intaris_session_id,
        intention: detail.intention,
        summary: detail.summary,
        status: detail.status,
        total_calls: detail.total_calls,
        approved_count: detail.approved_count,
        denied_count: detail.denied_count,
        escalated_count: detail.escalated_count,
        context_usage: detail.context_usage ?? null,
        last_generation: detail.last_generation ?? null
      };
      applyContextUsageSnapshot(detail.context_usage);
      applyLastGenerationSnapshot(detail.last_generation);
    } catch {
      if (!isStaleSessionInfoLoad(requestId, conversationId, sid)) {
        sessionInfo = null;
      }
    } finally {
      if (!isStaleSessionInfoLoad(requestId, conversationId, sid)) {
        sessionInfoLoading = false;
      }
    }
  }

  async function refreshSessionContextUsage(): Promise<void> {
    const conversationId = currentConversation?.conversation_id;
    const sid = currentConversation?.active_session_id;
    if (!conversationId || !sid) {
      contextUsage = null;
      return;
    }
    const requestId = nextSessionInfoRequestId();
    try {
      const detail = await api.sessions.intarisDetail(sid);
      if (isStaleSessionInfoLoad(requestId, conversationId, sid)) {
        return;
      }
      applyContextUsageSnapshot(detail.context_usage);
      applyLastGenerationSnapshot(detail.last_generation);
    } catch {
      // Context diagnostics are opportunistic; the Info panel can still load details on demand.
    }
  }

  async function loadSubSessionInfo(): Promise<void> {
    if (!subSessionId) return;
    subSessionInfoLoading = true;
    try {
      const detail = await api.sessions.intarisDetail(subSessionId);
      subSessionInfo = {
        intaris_session_id: detail.intaris_session_id,
        intention: detail.intention,
        summary: detail.summary,
        status: detail.status,
        total_calls: detail.total_calls,
        approved_count: detail.approved_count,
        denied_count: detail.denied_count,
        escalated_count: detail.escalated_count,
        context_usage: detail.context_usage ?? null,
        last_generation: detail.last_generation ?? null
      };
    } catch {
      subSessionInfo = null;
    } finally {
      subSessionInfoLoading = false;
    }
  }

  async function reloadConversationSubloads(
    conversationId: string,
    requestId: number,
    options: {
      reloadSessions?: boolean;
      reloadHistory?: boolean;
      resubscribe?: boolean;
      preserveScroll?: boolean;
    } = {},
  ): Promise<void> {
    const reloadSessions = options.reloadSessions ?? true;
    const reloadHistory = options.reloadHistory ?? true;
    const shouldResubscribe = options.resubscribe ?? false;
    // The scoped ChatV2 store reconciles live items with the canonical snapshot.
    // NOTE: the scroll position itself is captured at APPLY time (right before
    // the snapshot mutates the DOM), never here before the async fetch — a
    // pre-fetch capture restored a stale position and yanked actively-scrolling
    // users back to where they were when the refresh started.
    const wantsPreserveScroll = options.preserveScroll === true;
    let preservedScrollTop: number | null = null;
    const preservedVisibleStartIndex = visibleStartIndex;
    // Capture tail-pinned state before the async fetch so the post-refresh
    // scroll decision reflects where the user was, not where they ended up
    // during the network round-trip. A refresh must never yank a reading user
    // to the bottom.
    const preTailPinned = tailPinned;
    const initialLoadPolicy = conversationInitialLoadPolicy(currentConversation);
    const [conversationResult, sessionResult, chatV2SnapshotResult] = await Promise.all([
      settleWithTimeout(
        reloadSessions ? api.conversations.detail(conversationId, { includeState: false }) : Promise.resolve(null),
        CONVERSATION_SUBLOAD_TIMEOUT_MS,
        'Conversation detail',
      ),
      settleWithTimeout(
        reloadSessions
          ? api.conversations.sessions(conversationId, initialLoadPolicy.sessionOptions)
          : Promise.resolve(sessions),
        CONVERSATION_SUBLOAD_TIMEOUT_MS,
        'Conversation sessions',
      ),
      settleWithTimeout(
        reloadHistory ? chatV2Api.snapshot(conversationId) : Promise.resolve(null),
        CONVERSATION_SUBLOAD_TIMEOUT_MS,
        'Chat snapshot',
      ),
    ]);

    if (isStaleConversationLoad(requestId)) {
      return;
    }

    sessionsError = '';
    historyError = '';

    const previousConversation = currentConversation;
    const previousActiveSessionId = previousConversation?.active_session_id ?? null;
    let nextSessions = reloadSessions ? [] : sessions;
    let nextActiveSessionId = previousActiveSessionId;

    const conversationDetail = conversationResult.status === 'fulfilled' ? conversationResult.value : null;
    const conversationFailure = reloadSessions && conversationResult.status === 'rejected'
      ? conversationResult.reason
      : null;
    if (reloadSessions && conversationDetail) {
      currentConversation = conversationDetail;
      patchConversationInList(conversationDetail.conversation_id, conversationDetail);
      if (conversationDetail.active_session_id) {
        nextActiveSessionId = conversationDetail.active_session_id;
      }
    }

    const chatV2Snapshot = chatV2SnapshotResult.status === 'fulfilled'
      ? chatV2SnapshotResult.value
      : null;
    if (reloadHistory && !chatV2Snapshot) {
      const failure = chatV2SnapshotResult.status === 'rejected'
        ? chatV2SnapshotResult.reason
        : new Error('Chat v2 snapshot unavailable');
      const failureMessage = asApiError(failure).message;
      historyError = failureMessage;
      if (markControllerUnavailable(failure)) {
        return;
      }
      if (!chatV2OwnsActiveConversation(conversationId) || chatV2Store.visibleItems.length === 0) {
        chatV2Store.reset();
        applyChatV2StateToTimeline();
        olderMessagesCursor = null;
        hasOlderMessages = false;
        queuedMessages = [];
        queuedCount = 0;
        awaitingAssistantStart = false;
        turnInProgress = false;
        currentActiveTurnId = null;
        wsClient.clearChatV2Cursor(conversationId);
      }
      reportError(failure);
      addToast(failureMessage, 'error');
      return;
    }
    if (reloadHistory && chatV2Snapshot) {
      // Capture the scroll position immediately before the snapshot mutates
      // the timeline — the user's position NOW, not where they were when the
      // network fetch started.
      if (wantsPreserveScroll && userScrolledUp && timelineEl) {
        preservedScrollTop = timelineEl.scrollTop;
      }
      applyChatV2Snapshot(chatV2Snapshot);
      void drainChatV2Outbox(conversationId);
      olderMessagesCursor = chatV2Snapshot.timeline.before_cursor ?? null;
      hasOlderMessages = chatV2Snapshot.timeline.has_more_before;
      nextActiveSessionId = chatV2Snapshot.conversation.active_session_id ?? nextActiveSessionId;
      setConversationTurnIndicator(currentConversation?.conversation_id, turnInProgress);
    }

    if (reloadSessions && sessionResult.status === 'fulfilled') {
      nextSessions = sessionResult.value;
      sessions = nextSessions;
      resetSessionFilter();
    } else if (reloadSessions && sessionResult.status === 'rejected') {
      sessions = [];
      nextSessions = [];
      sessionIds.clear();
      const nextError = asApiError(sessionResult.reason).message;
      if (!(isPreSessionChatConversation(previousConversation, 0) && isMissingSessionError(nextError))) {
        sessionsError = nextError;
        markControllerUnavailable(sessionResult.reason);
      }
    }
    if (conversationFailure && !sessionsError) {
      const nextError = asApiError(conversationFailure).message;
      sessionsError = nextError;
      if (!markControllerUnavailable(conversationFailure)) {
        reportError(conversationFailure);
      }
    }

    if (reloadHistory && chatV2Snapshot) {
      if (preservedScrollTop !== null) {
        timelineWindow = {
          start: Math.min(preservedVisibleStartIndex, Math.max(0, renderableVisibleItems.length - TIMELINE_WINDOW_TARGET_ROWS)),
          end: null,
        };
      } else {
        syncVisibleWindow();
        userScrolledUp = false;
        tailPinned = true;
      }
      syncConversationActiveSession(nextActiveSessionId);
      if (!headerInfoOpen) {
        void refreshSessionContextUsage();
      }
      if (currentConversation) {
        saveCurrentConversationView();
      }
    }
    if (reloadHistory) {
      cachedConversationRefreshing = false;
    }

    if (shouldResubscribe) {
      const subscribeSeq = nextActiveSessionId === previousActiveSessionId ? activeSessionLastSeq : 0;
      activeSessionLastSeq = subscribeSeq;
      wsClient.subscribeConversation(
        conversationId,
        subscribeSeq,
        nextActiveSessionId,
      );
    }

    const nextConversation = currentConversation ?? previousConversation;
    const skipNotificationRefresh = isPreSessionChatConversation(
      nextConversation
        ? { ...nextConversation, active_session_id: nextActiveSessionId }
        : null,
      nextSessions.length,
    );

    if (!sessionsError && !skipNotificationRefresh) {
      const notificationResult = await settleWithTimeout(
        refreshPendingNotifications(),
        CONVERSATION_SUBLOAD_TIMEOUT_MS,
        'Pending notifications',
      );
      if (isStaleConversationLoad(requestId)) {
        return;
      }
      if (notificationResult.status === 'rejected') {
        reportError(notificationResult.reason);
      }
    } else {
      escalations = [];
      escalationError = '';
      pendingDirectQuestion = null;
      pendingCredentialRequest = null;
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
      directQuestionSubmitting = false;
    }

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (preservedScrollTop !== null && timelineEl) {
          // Restore only if the viewport has not moved materially since the
          // capture (taken right before the snapshot apply). If it has, the
          // user kept scrolling while the refresh settled — never fight an
          // active scroll gesture with a stale restore. The write goes through
          // the programmatic guard so the resulting async scroll event is not
          // misread as a user gesture (which could falsely re-attach the tail
          // and trigger a jump to the bottom).
          if (
            // Never restore an old scrolled-up position over a user who is now
            // pinned to the tail: if they jumped to the bottom while the refresh
            // settled, restoring would yank them back up and re-show the jump
            // button. Their current pinned state wins.
            !tailPinned &&
            userScrolledUp &&
            shouldApplyScrollRestore({
              capturedScrollTop: preservedScrollTop,
              currentScrollTop: timelineEl.scrollTop,
            })
          ) {
            writeProgrammaticScrollTop(preservedScrollTop);
            userScrolledUp = true;
            tailPinned = false;
          }
          // Restore skipped: the user moved during the refresh (scrolled
          // further, or jumped back to the bottom). Their gesture state
          // (userScrolledUp/tailPinned) already reflects where they are now —
          // forcing userScrolledUp=true here would detach a user who
          // deliberately returned to the tail from live auto-scroll.
          return;
        }
        // Only auto-scroll if the user was pinned to the tail before the
        // refresh. A background refresh must never yank a reading user to the
        // bottom. scrollToBottom() is idempotent: it re-pins iff !userScrolledUp,
        // so if the user scrolled up during the async fetch this is a no-op.
        if (preTailPinned) {
          scrollToBottom(false);
        }
      });
    });
  }

  async function retryConversationSubloads(): Promise<void> {
    if (!currentConversation) return;
    const requestId = beginConversationLoad();
    error = '';
    const retryScope = getConversationRetryScope({
      sessionsError,
      historyError,
    });
    conversationSubloadsLoading = true;
    try {
      await reloadConversationSubloads(currentConversation.conversation_id, requestId, {
        reloadSessions: retryScope.sessions,
        reloadHistory: retryScope.history,
        resubscribe: false,
      });
    } finally {
      if (!isStaleConversationLoad(requestId)) {
        conversationSubloadsLoading = false;
      }
    }
  }

  function appendLocalSystemMessage(text: string): void {
    // Chat v2 owns the canonical timeline; local-only notices are surfaced as
    // a transient toast instead of being injected into the timeline store.
    addToast(text, 'warning');
  }

  function hasActiveTurnTimelineItem(): boolean {
    const lastItem = renderableVisibleItems[renderableVisibleItems.length - 1];
    return Boolean(lastItem && isInlineLiveChatV2Item(lastItem));
  }

  function hasAnyActiveTurnTimelineItem(): boolean {
    return renderableVisibleItems.some((item) => (
      isInlineLiveChatV2Item(item) || isChatV2DelegationActive(item)
    ));
  }

  async function refreshConversationView(reason: string): Promise<void> {
    if (switchingConversation) {
      expireStaleConversationSwitch();
    }
    if (!currentConversation || document.hidden || initializing || switchingConversation) {
      return;
    }
    // Debounce opportunistic refreshes (focus/visibility/online/stale-*): each
    // one replaces the timeline from a fresh snapshot and restores scroll,
    // which is disruptive while the user is reading or scrolling. Gap-driven
    // refreshes (websocket-reconnect-gap) are never debounced.
    if (
      shouldDebounceConversationViewRefresh({
        reason,
        lastRefreshAt: lastConversationViewRefreshAt,
        now: Date.now(),
      })
    ) {
      return;
    }
    if (viewRefreshInFlight) {
      await viewRefreshInFlight;
      return;
    }

    const requestId = beginConversationLoad();
    conversationSubloadsLoading = true;
    viewRefreshInFlight = (async () => {
      wsClient.connect();
      await reloadConversationSubloads(currentConversation.conversation_id, requestId, {
        reloadSessions: true,
        reloadHistory: true,
        resubscribe: true,
        preserveScroll: true,
      });

      if (!isStaleConversationLoad(requestId) && (historyError || sessionsError)) {
        appendLocalSystemMessage(`Chat refresh failed (${reason}). History may be stale.`);
      }
    })();
    try {
      await viewRefreshInFlight;
    } finally {
      viewRefreshInFlight = null;
      if (!isStaleConversationLoad(requestId)) {
        conversationSubloadsLoading = false;
      }
    }
  }

  async function reconcileActiveConversation(): Promise<void> {
    await refreshConversationView('foreground');
  }

  function scheduleForegroundReconcile(delayMs = 150): void {
    if (typeof window === 'undefined') return;
    if (foregroundSyncTimer !== null) {
      window.clearTimeout(foregroundSyncTimer);
    }
    foregroundSyncTimer = window.setTimeout(() => {
      foregroundSyncTimer = null;
      void refreshConversationView('foreground');
    }, delayMs);
  }

  async function openConversation(conversationId: string): Promise<void> {
    if (!conversationId) {
      initializing = false;
      resetConversationSwitch();
      initialConversationResolved = true;
      return;
    }

    if (conversationId === activeConversationId && currentConversation) {
      initializing = false;
      resetConversationSwitch();
      initialConversationResolved = true;
      return;
    }

    const requestId = beginConversationLoad();
    const previousConversationId = activeConversationId;
    if (
      controllerRecoveryConversationId !== null
      && controllerRecoveryConversationId !== conversationId
    ) {
      clearControllerRecovery();
    }
    const isInitialLoad = !initialConversationResolved && !currentConversation;
    const switchRequestId = beginConversationSwitch(conversationId, !isInitialLoad);
    saveCurrentConversationView();

    showAgentProfile = false;
    error = '';
    historyError = '';
    sessionsError = '';
    escalationError = '';
    escalationResolutionPending = null;
    cachedConversationRefreshing = false;
    headerInfoOpen = false;
    invalidateSessionInfo();
    mobileListOpen = false;

    chatV2Lifecycle.release();
    if (previousConversationId) {
      wsClient.unsubscribeConversation(previousConversationId);
    }

    const cachedEntry = conversationViewCache.get(conversationId);
    if (cachedEntry && isAgentDirectConversation(cachedEntry.conversation)) {
      conversationViewCache.delete(conversationId);
    }

    const cachedView = restoreConversationView(conversationId);
    if (cachedView) {
      if (isAgentDirectConversation(cachedView.conversation)) {
        await clearSelectedAgentFilterForDirectChat();
        if (isStaleConversationLoad(requestId)) {
          return;
        }
      }
      resetConversationSwitch();
      initializing = false;
      initialConversationResolved = true;
      stopInitialLoadTimeout();
      error = '';
      historyError = '';
      sessionsError = '';
      mobileListOpen = false;
      cachedConversationRefreshing = true;
      wsClient.subscribeConversation(
        conversationId,
        cachedView.activeSessionLastSeq,
        cachedView.conversation.active_session_id ?? null,
        { replaceCursor: true },
      );
      markConversationReadLocally(conversationId);
      api.conversations.markRead(conversationId).catch(() => {});
      conversationSubloadsLoading = true;
      try {
        const conversation = await api.conversations.detail(conversationId, { includeState: false });
        if (isStaleConversationLoad(requestId)) {
          return;
        }
        currentConversation = { ...conversation, has_unread: false };
        mergeConversationList([currentConversation]);
        patchAgentDirectChat(currentConversation);
        persistLastOpenedConversation(currentConversation);
        void refreshQueuedMessages(conversationId);
        await reloadConversationSubloads(conversationId, requestId, {
          reloadSessions: true,
          reloadHistory: true,
          resubscribe: true,
          preserveScroll: true,
        });
        if (isStaleConversationLoad(requestId)) {
          return;
        }
        markConversationReadLocally(conversationId);
        patchConversationInList(conversationId, {
          active_session_id: currentConversation?.active_session_id,
        });
      } catch (caughtError) {
        if (!isStaleConversationLoad(requestId)) {
          if (markControllerUnavailable(caughtError, conversationId)) {
            error = '';
            return;
          }
          historyError = asApiError(caughtError).message;
        }
      } finally {
        if (!isStaleConversationLoad(requestId)) {
          conversationSubloadsLoading = false;
          cachedConversationRefreshing = false;
        }
      }
      return;
    }

    clearConversationViewState();

    try {
      const conversation = await api.conversations.detail(conversationId, { includeState: false });
      if (isStaleConversationLoad(requestId)) {
        return;
      }

      const desiredStatusFilter = conversationStatusFilterForConversation(conversation, selectedConversationStatus);
      if (selectedConversationStatus !== desiredStatusFilter) {
        selectedConversationStatus = desiredStatusFilter;
        await replaceConversationStatusUrl(desiredStatusFilter);
        await refreshAvailableChannelTypes();
        await loadConversationPage(true);
        if (isStaleConversationLoad(requestId)) {
          return;
        }
      }
      if (isAgentDirectConversation(conversation)) {
        await clearSelectedAgentFilterForDirectChat();
        if (isStaleConversationLoad(requestId)) {
          return;
        }
      }

      activeConversationId = conversationId;
      currentConversation = conversation;
      error = '';
      initialLoadTimedOut = false;
      persistLastOpenedConversation(conversation);
      mergeConversationList([conversation]);
      patchAgentDirectChat(conversation);
      void refreshQueuedMessages(conversationId);
      wsClient.subscribeConversation(
        conversationId,
        0,
        conversation.active_session_id ?? null,
        { replaceCursor: true },
      );

      conversationSubloadsLoading = true;
      try {
        await reloadConversationSubloads(conversationId, requestId, {
          reloadSessions: true,
          reloadHistory: true,
          resubscribe: true,
        });
      } finally {
        if (!isStaleConversationLoad(requestId)) {
          conversationSubloadsLoading = false;
        }
      }
      if (isStaleConversationLoad(requestId)) {
        return;
      }

      markConversationReadLocally(conversationId);
      api.conversations.markRead(conversationId).catch(() => {});
      currentConversation = {
        ...(currentConversation ?? conversation),
        has_unread: false,
      };
      markConversationReadLocally(conversationId);
      patchConversationInList(conversationId, {
        active_session_id: currentConversation.active_session_id,
      });
    } catch (caughtError) {
      if (isStaleConversationLoad(requestId)) {
        return;
      }
      if (markControllerUnavailable(caughtError, conversationId)) {
        error = '';
        return;
      }
      error = asApiError(caughtError).message;
      clearLastOpenedConversation(conversationId);
      currentConversation = null;
      sessions = [];
      chatV2Store.reset();
      olderMessagesCursor = null;
      hasOlderMessages = false;
      escalations = [];
      escalationResolutionPending = null;
      pendingDirectQuestion = null;
      pendingCredentialRequest = null;
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
      directQuestionSubmitting = false;
      sessionIds.clear();
      conversationSubloadsLoading = false;
    } finally {
      if (!isStaleConversationLoad(requestId)) {
        initializing = false;
        initialConversationResolved = true;
        if (currentConversation || error) {
          stopInitialLoadTimeout();
        }
      }
      finishConversationSwitch(conversationId, switchRequestId);
    }
  }

  async function initialize(): Promise<void> {
    initializing = true;
    error = '';
    beginInitialLoadTimeout();
    const hasConversationRoute = Boolean(conversationIdFromRoute());

    // Render the chat shell immediately for deep links so mobile users don't
    // stare at a full-screen blocking card while the sidebar and history load.
    if (hasConversationRoute) {
      initializing = false;
    }

    try {
      await refreshSidebarData();
      if (!hasConversationRoute) {
        initializing = false;
        initialConversationResolved = true;
        stopInitialLoadTimeout();
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      initializing = false;
      initialConversationResolved = true;
      stopInitialLoadTimeout();
    }

  }

  function preferredNewConversationAgentId(): string {
    const primaryAgents = agents.filter((agent) => agent.status === 'active' && agent.agent_type === 'primary');
    if (selectedAgentIds.length === 1 && primaryAgents.some((agent) => agent.agent_id === selectedAgentIds[0])) {
      return selectedAgentIds[0];
    }
    const currentAgentId = currentConversation?.agent_id ?? '';
    if (currentAgentId && primaryAgents.some((agent) => agent.agent_id === currentAgentId)) {
      return currentAgentId;
    }
    if (typeof window !== 'undefined') {
      const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
      const storedAgents = parseStoredMultiFilter(stored);
      if (storedAgents.length === 1 && primaryAgents.some((agent) => agent.agent_id === storedAgents[0])) {
        return storedAgents[0];
      }
    }
    return primaryAgents[0]?.agent_id ?? '';
  }

  function openNewConversationModal(): void {
    newChatError = '';
    newChatAgentId = preferredNewConversationAgentId();
    newChatAgentProfileId = '';
    // On mobile the conversation list is a sliding overlay. Hide it before
    // showing the modal or the modal is obscured by the list drawer.
    mobileListOpen = false;
    showNewChatModal = true;
  }

  async function openNewConversationForSelectedAgent(): Promise<void> {
    newChatError = '';
    newChatAgentId = preferredNewConversationAgentId();
    newChatAgentProfileId = '';
    mobileListOpen = false;
    await tick();
    await createNewConversation();
  }

  function closeNewConversationModal(): void {
    if (newChatCreating) return;
    showNewChatModal = false;
    newChatError = '';
  }

  async function createNewConversation(): Promise<void> {
    const agentId = newChatAgentId || preferredNewConversationAgentId();

    if (!agentId) {
      newChatError = 'Create or activate an agent before starting a conversation.';
      return;
    }

    newChatCreating = true;
    newChatError = '';

    try {
      const conversation = await api.conversations.create({
        agent_id: agentId,
        agent_profile_id: newChatAgentProfileId || null,
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {}
        }
      });
      mergeConversationList([conversation]);
      showNewChatModal = false;
      addToast('Conversation created.', 'success');
      await goto(conversationUrl(conversation.conversation_id));
    } catch (caughtError) {
      newChatError = asApiError(caughtError).message;
      addToast(newChatError, 'error', 4_000, 'Unable to create conversation');
    } finally {
      newChatCreating = false;
    }
  }

  async function archiveConversation(): Promise<void> {
    if (!currentConversation) return;

    const confirmed = await confirmAction({
      title: 'Archive conversation?',
      message: 'The conversation will become read-only until you manually reactivate it.',
      confirmLabel: 'Archive conversation'
    });
    if (!confirmed) return;

    archivingConversation = true;
    try {
      const archivedConversation = await api.conversations.update(currentConversation.conversation_id, { archived: true });
      clearLastOpenedConversation(archivedConversation.conversation_id);
      selectedConversationStatus = 'active';
      await replaceConversationStatusUrl('active');
      await refreshSidebarData();
      const nextConversationId = nextVisibleConversationId(archivedConversation.conversation_id);
      addToast('Conversation archived.', 'success');
      await goto(nextConversationId ? conversationUrl(nextConversationId) : '/chat/new');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to archive conversation');
    } finally {
      archivingConversation = false;
    }
  }

  async function deleteConversation(): Promise<void> {
    if (!currentConversation) return;

    const confirmed = await confirmAction({
      title: 'Delete conversation?',
      message: 'This removes the conversation from the workspace. Use purge from the API only when you need permanent deletion.',
      confirmLabel: 'Delete conversation'
    });
    if (!confirmed) return;

    deletingConversation = true;
    try {
      const deletedConversationId = currentConversation.conversation_id;
      await api.conversations.remove(deletedConversationId);
      clearLastOpenedConversation(deletedConversationId);
      selectedConversationStatus = 'active';
      await replaceConversationStatusUrl('active');
      await refreshSidebarData();
      const nextConversationId = nextVisibleConversationId(deletedConversationId);
      addToast('Conversation deleted.', 'success');
      await goto(nextConversationId ? conversationUrl(nextConversationId) : '/chat/new');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete conversation');
    } finally {
      deletingConversation = false;
    }
  }

  async function restoreConversation(): Promise<void> {
    if (!currentConversation || currentConversation.status !== 'archived') return;

    archivingConversation = true;
    try {
      currentConversation = await api.conversations.update(currentConversation.conversation_id, { archived: false });
      selectedConversationStatus = 'active';
      await replaceConversationStatusUrl('active');
      await refreshSidebarData();
      if (currentConversation) {
        persistLastOpenedConversation(currentConversation);
      }
      addToast('Conversation restored.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to restore conversation');
    } finally {
      archivingConversation = false;
    }
  }

  async function setConversationStatusFilter(status: ConversationStatusFilter): Promise<void> {
    if (selectedConversationStatus === status) return;
    selectedConversationStatus = status;
    await replaceConversationStatusUrl(status);
    resetConversationSearchResults();
    await refreshAvailableChannelTypes();
    await Promise.all([loadAgentDirectChats(), loadConversationPage(true)]);
  }

  async function saveTitle(): Promise<void> {
    if (!currentConversation) return;
    const newTitle = editTitleValue.trim();
    editingTitle = false;
    if (!newTitle || newTitle === (currentConversation.title ?? '').trim()) return;

    try {
      currentConversation = await api.conversations.update(currentConversation.conversation_id, { title: newTitle });
      if (currentConversation) {
        mergeConversationList([currentConversation]);
      }
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to update title');
    }
  }

  async function refreshTitleSuggestion(): Promise<void> {
    if (!currentConversation || titleSuggestionLoading) return;
    titleSuggestionLoading = true;
    try {
      const suggestion = await api.conversations.titleSuggestion(currentConversation.conversation_id);
      if (suggestion.available && suggestion.title?.trim()) {
        editTitleValue = suggestion.title.trim();
      } else {
        addToast(suggestion.reason ?? 'No Intaris title suggestion is available yet.', 'info', 3_000);
      }
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to load title suggestion');
    } finally {
      titleSuggestionLoading = false;
    }
  }

  function startEditTitle(): void {
    if (!currentConversation) return;
    editTitleValue = currentConversation.title ?? '';
    editingTitle = true;
  }

  function handleTitleRefreshPointerDown(event: PointerEvent): void {
    event.preventDefault();
    ignoreNextTitleBlur = true;
  }

  function handleTitleBlur(): void {
    if (ignoreNextTitleBlur) {
      ignoreNextTitleBlur = false;
      return;
    }
    void saveTitle();
  }

  function handleTitleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      void saveTitle();
    }
    if (event.key === 'Escape') {
      editingTitle = false;
    }
  }

  async function copySessionId(): Promise<void> {
    const sid = currentConversation?.active_session_id;
    if (!sid) return;
    try {
      await navigator.clipboard.writeText(sid);
      sessionIdCopied = true;
      setTimeout(() => { sessionIdCopied = false; }, 2000);
    } catch {
      addToast('Failed to copy session ID', 'error');
    }
  }

  async function openIntarisSession(sessionId: string): Promise<void> {
    if (!sessionId) return;
    try {
      const exchange = await api.auth.exchangeToken('intaris');
      openUrlInNewTab(
        buildLinkedServiceUrl('intaris', {
          token: exchange.token,
          tab: 'sessions',
          session_id: sessionId
        }, exchange.ui_url)
      );
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to open Intaris');
    }
  }

  let slashSuggestionsVisible = $state(false);
  let slashFilteredSuggestions = $state<SlashCommandSuggestion[]>([]);
  let slashSelectedIndex = $state(0);
  let slashSuggestionRequestSeq = 0;
  let slashSuggestionTimer: ReturnType<typeof setTimeout> | null = null;

  function updateSlashSuggestions(): void {
    if (slashSuggestionTimer) {
      clearTimeout(slashSuggestionTimer);
      slashSuggestionTimer = null;
    }
    const currentInput = composer;
    const parameterCommand = slashParameterSuggestionCommand(currentInput);
    if (parameterCommand && currentConversation) {
      const conversationId = currentConversation.conversation_id;
      const requestSeq = slashSuggestionRequestSeq + 1;
      slashSuggestionRequestSeq = requestSeq;
      slashFilteredSuggestions = [];
      slashSuggestionsVisible = false;
      const delayMs = /\s$/.test(currentInput) ? 0 : 100;
      slashSuggestionTimer = setTimeout(() => {
        void (async () => {
          try {
            const response = await api.conversations.slashCommandSuggestions(
              conversationId,
              currentInput,
              12
            );
            if (requestSeq !== slashSuggestionRequestSeq || composer !== currentInput) return;
            slashFilteredSuggestions = response.items;
            slashSuggestionsVisible = response.items.length > 0;
            slashSelectedIndex = 0;
          } catch {
            if (requestSeq !== slashSuggestionRequestSeq) return;
            slashSuggestionsVisible = false;
            slashFilteredSuggestions = [];
          }
        })();
      }, delayMs);
      return;
    }

    const localSuggestions = localSlashCommandSuggestions(currentInput);
    slashSuggestionRequestSeq += 1;
    slashFilteredSuggestions = localSuggestions;
    slashSuggestionsVisible = slashFilteredSuggestions.length > 0;
    slashSelectedIndex = 0;
  }

  function acceptSlashSuggestion(index: number): void {
    const suggestion = slashFilteredSuggestions[index];
    if (!suggestion) return;
    composer = applySlashSuggestion(suggestion);
    if (suggestion.kind === 'command' && slashParameterSuggestionCommand(composer)) {
      updateSlashSuggestions();
    } else {
      slashSuggestionsVisible = false;
    }
    focusActiveComposer();
  }

  function handleSlashSuggestionPointerDown(event: Event, index: number): void {
    event.preventDefault();
    acceptSlashSuggestion(index);
  }

  function handleComposerFileInputChange(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const files = input.files;
    if (!files || files.length === 0) return;
    void uploadFiles(Array.from(files));
    input.value = '';
  }

  async function resolveDirectQuestionNotificationId(pendingStepTool: ReturnType<typeof selectPendingInputToolCall>): Promise<string> {
    const argumentNotificationId = pendingStepTool?.arguments?.notification_id;
    const argumentRequestId = pendingStepTool?.arguments?.request_id;
    let notificationId =
      (typeof argumentNotificationId === 'string' ? argumentNotificationId : '')
      || (typeof argumentRequestId === 'string' ? argumentRequestId : '')
      || pendingDirectQuestion?.notificationId
      || '';
    if (!notificationId && currentConversation) {
      try {
        const list = await api.notifications.list(currentConversation.conversation_id);
        const match = list.find(
          (item) => ['step_question', 'auth_challenge'].includes(item.notification_type) && item.status === 'pending',
        );
        if (match) notificationId = match.notification_id;
      } catch {
        // Fall through — caller decides whether to fall back to a normal message.
      }
    }
    return notificationId;
  }

  async function submitDirectQuestionReply(reply: QuestionSetReply): Promise<boolean> {
    if (!pendingDirectQuestion) return false;
    const pendingStepTool = findPendingStepRequestInputCallFromChatV2();
    const notificationId = await resolveDirectQuestionNotificationId(pendingStepTool);
    if (!notificationId) return false;
    const inputKind = pendingInputToolKind(pendingStepTool);
    const responseText = inputKind === 'auth_challenge' ? questionSetReplyText(reply) : '';
    if (inputKind === 'auth_challenge' && !responseText) {
      addToast('Enter a response before sending.', 'error');
      return false;
    }

    directQuestionSubmitting = true;
    // The tool card's resolved state is driven by the canonical Chat v2 frame
    // that follows the respond call; no optimistic timeline mutation here.
    syncVisibleWindow();
    userScrolledUp = false;
    tailPinned = true;
    scrollToBottom(true);
    try {
      if (inputKind === 'auth_challenge') {
        await api.notifications.resolve(notificationId, {
          decision: 'continue',
          response: responseText
        });
      } else {
        await api.notifications.resolve(notificationId, {
          decision: 'continue',
          response_payload: reply as unknown as Record<string, unknown>
        });
      }
      haptic.success();
      clearActiveDirectQuestionDraft();
      return true;
    } catch (caughtError) {
      directQuestionSubmitting = false;
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to submit response');
      return false;
    }
  }

  async function cancelDirectQuestionRequest(): Promise<void> {
    if (!pendingDirectQuestion || directQuestionSubmitting) return;
    const pendingStepTool = findPendingStepRequestInputCallFromChatV2();
    const notificationId = await resolveDirectQuestionNotificationId(pendingStepTool);
    if (!notificationId) {
      addToast('Unable to find the pending input request to cancel.', 'error');
      return;
    }
    directQuestionSubmitting = true;
    try {
      await api.notifications.resolve(notificationId, { decision: 'cancel' });
      if (pendingStepTool) {
        syncVisibleWindow();
      }
      pendingDirectQuestion = null;
      clearActiveDirectQuestionDraft();
      directQuestionAnswers = {};
      directQuestionPageIndex = 0;
      haptic.success();
      await refreshPendingDirectQuestion();
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to cancel input request');
    } finally {
      directQuestionSubmitting = false;
    }
  }

  function submitDirectQuestionOption(question: QuestionSetQuestion, optionId: string): void {
    if (!pendingDirectQuestion || directQuestionSubmitting) return;
    toggleDirectQuestionOption(question, optionId);
  }

  function toggleDirectQuestionOption(question: QuestionSetQuestion, optionId: string): void {
    const current = directQuestionState(question.id);
    const selected = new Set(current.selected);
    if (question.multiple) {
      if (selected.has(optionId)) {
        selected.delete(optionId);
      } else {
        selected.add(optionId);
      }
    } else {
      selected.clear();
      selected.add(optionId);
    }
    directQuestionAnswers = {
      ...directQuestionAnswers,
      [question.id]: { ...current, selected: Array.from(selected) }
    };
    persistDirectQuestionDraft();
  }

  function focusActiveComposer(): void {
    composerElement?.focus();
  }

  async function transcribeVoiceRecording(attachment: AttachmentRef): Promise<string> {
    const result = await api.stt.transcribeArtifact(attachment.artifact_id);
    return result.text.trim();
  }

  async function handleSend(): Promise<void> {
    let content = composer.trim();
    if ((!content && composerAttachments.length === 0) || !currentConversation || isReadOnly(currentConversation)) return;
    const sendConversationId = currentConversation.conversation_id;
    if (voiceTranscribing) return;
    if (pendingDirectQuestion && directQuestionSubmitting) return;

    // STT-first: transcribe any voice recordings, replace the composer body
    // with (or append to) the transcript, and drop the audio from the
    // outgoing turn payload. Matches the channel inbound behavior.
    const voiceRecordings = composerAttachments.filter((a) => a.voice_recording);
    if (voiceRecordings.length > 0) {
      voiceTranscribing = true;
      haptic.light();
      try {
        const transcripts: string[] = [];
        for (const recording of voiceRecordings) {
          const text = await transcribeVoiceRecording(recording);
          if (text) transcripts.push(text);
        }
        const transcript = transcripts.join(' ').trim();
        if (transcript) {
          content = content ? `${content}\n\n${transcript}` : transcript;
        }
        composerAttachments = composerAttachments.filter((a) => !a.voice_recording);
        for (const recording of voiceRecordings) {
          if (recording.blob_url) URL.revokeObjectURL(recording.blob_url);
        }
        if (!content && composerAttachments.length === 0) {
          addToast("Couldn't transcribe the recording. Try again or type a message.", 'error');
          haptic.error();
          return;
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Transcription failed';
        addToast(message, 'error', 4_000, 'Voice transcription failed');
        haptic.error();
        return;
      } finally {
        voiceTranscribing = false;
      }
    }

    const normalizedSlashCommand = normalizeSlashCommandInput(content);
    const isSlashCommand = isSystemSlashCommand(content);
    const outboundContent = isSlashCommand ? normalizedSlashCommand : content;
    const chatModeDirective = parseChatModeDirectiveInput(content);
    const optimisticContent = chatModeDirective?.oneShot && chatModeDirective.content
      ? chatModeDirective.content
      : content;
    const optimisticTurnMode = chatModeDirective?.oneShot ? chatModeDirective.mode : persistentChatMode;

    // Detect a pending input tool call sitting in the timeline
    // waiting for a reply. This is the source-of-truth signal that the
    // agent loop is paused for user input. `pendingDirectQuestion` is a
    // mirror of the same state but can drift (stale fetch, missed WS
    // event, reload race) — the tool-call entry does not.
    const pendingStepTool = findPendingStepRequestInputCallFromChatV2();
    const inputKind = pendingInputToolKind(pendingStepTool);
    const isStepInputReply =
      !isSlashCommand && (pendingDirectQuestion !== null || pendingStepTool !== null);

    if (isStepInputReply && composerAttachments.length > 0) {
      addToast('Attachments are not supported for clarification responses.', 'error');
      return;
    }
    if (isStepInputReply && inputKind !== 'auth_challenge') {
      const questions = directQuestionItems();
      const reply = buildDirectQuestionReply(questions, content);
      if (!directQuestionReplySatisfiesRequired(questions, reply)) {
        addToast('Answer all required questions before sending.', 'error');
        return;
      }
    }
    // Optimistic UI. When the message is a step_request_questions reply we do
    // not append a separate user bubble — the tool call block will show the
    // user's answer inline as the resolution. Adding a bubble too would
    // duplicate the content and leave the tool call block stuck as pending.
    const clientMessageId = (!isSlashCommand && !isStepInputReply)
      ? `cmsg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`
      : null;
    const willQueueBehindActiveTurn = !isSlashCommand && !isStepInputReply && turnInProgress;
    const composerAttachmentsSnapshot = $state.snapshot(composerAttachments) as typeof composerAttachments;

    if (!isSlashCommand && !isStepInputReply && clientMessageId && !willQueueBehindActiveTurn) {
      saveOptimisticUserMessageDraft({
        conversationId: directQuestionDraftConversationId() ?? routeConversationId,
        clientMessageId,
        content: optimisticContent,
        attachments: composerAttachmentsSnapshot,
        createdAt: Date.now(),
      });
      // Pass the current chat mode so the plan-mode marker is visible
      // immediately on the optimistic bubble (Issue D — no blink).
      chatV2Store.addOptimisticUser({
        content: optimisticContent,
        attachments: composerAttachmentsSnapshot,
        clientMessageId,
      });
      applyChatV2StateToTimeline();
    }

    if (!isSlashCommand) {
      lastSubmittedMessage = content;
      lastRecoverableMessage = '';
      lastRecoverableTurnId = null;
      suppressedRecoverableTurnId = null;
      if (!willQueueBehindActiveTurn) {
        awaitingAssistantStart = true;
        turnInProgress = true;
        activeTurnChatMode = optimisticTurnMode;
        patchConversationInList(
          sendConversationId,
          optimisticConversationTurnPatch(optimisticTurnMode),
          { touchLastMessageAt: true }
        );
      }
    }
    error = '';
    composer = '';
    // Drop any persisted draft synchronously so a quick tab-away right
    // after send does not repopulate the pill from storage.
    clearDraft(currentDraftKey);
    // Clear the native value synchronously and let Svelte flush the
    // reactive binding before measuring scrollHeight. Without the tick,
    // `syncComposerHeight` runs while the textarea still holds the
    // multi-line content, so it "resizes" back to the same height and
    // the composer stays expanded until the user types again.
    if (composerElement) composerElement.value = '';
    await tick();
    syncComposerHeight();
    const attachments = composerAttachmentsSnapshot;
    composerAttachments = [];

    if (isStepInputReply) {
      // Resolve the notification ID. Order of preference:
      //   1. The tool call's own annotation (authoritative if present).
      //   2. `pendingDirectQuestion.notificationId` from the WS event.
      //   3. A fresh fetch of pending input/challenge notifications.
      const notificationId = await resolveDirectQuestionNotificationId(pendingStepTool);

      if (notificationId) {
        directQuestionSubmitting = true;
        // The tool call block's resolved state is driven by the canonical
        // Chat v2 frame that follows the respond call.
        syncVisibleWindow();
        userScrolledUp = false;
        tailPinned = true;
        scrollToBottom(true);
        try {
          if (inputKind === 'auth_challenge') {
            await api.notifications.resolve(notificationId, {
              decision: 'continue',
              response: content
            });
          } else {
          const questions = directQuestionItems();
          const reply = buildDirectQuestionReply(questions, content);
          if (!directQuestionReplySatisfiesRequired(questions, reply)) {
            directQuestionSubmitting = false;
            addToast('Answer all required questions before sending.', 'error');
            return;
          }
            await api.notifications.resolve(notificationId, {
              decision: 'continue',
              response_payload: reply as unknown as Record<string, unknown>
            });
          }
          haptic.success();
          clearActiveDirectQuestionDraft();
          return;
        } catch (caughtError) {
          directQuestionSubmitting = false;
          addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to submit response');
          return;
        }
      }

      // Fall back to a regular message: we saw a pending step_request_questions
      // but could not resolve a notification_id. Replace the missing
      // optimistic bubble so the user still sees their message.
      chatV2Store.addOptimisticUser({
        content,
        attachments,
        clientMessageId: `cmsg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`,
      });
    }

    syncVisibleWindow();
    userScrolledUp = false;
    tailPinned = true;
    scrollToBottom(true);
    haptic.success();
    if (isSlashCommand) {
      try {
        const response = await executeCommandWithRetry(sendConversationId, outboundContent);
        await applyCommandResponse(response);
      } catch (caughtError) {
        addToast(asApiError(caughtError).message, 'error', 4000, 'Command failed');
      }
      return;
    }
    const clientTxnId = clientMessageId ?? crypto.randomUUID();
    const effectiveClientMessageId = clientMessageId ?? clientTxnId;
    const createdAt = new Date().toISOString();
    let outboxStored = false;
    const effectiveContent = chatModeDirective?.oneShot ? (chatModeDirective.content ?? '') : outboundContent;
    const effectiveChatMode = chatModeDirective?.oneShot ? chatModeDirective.mode : undefined;
    const outboxEntry: OutboxEntry = {
      client_txn_id: clientTxnId,
      client_message_id: effectiveClientMessageId,
      conversation_id: sendConversationId,
      content: effectiveContent,
      attachments,
      chat_mode: effectiveChatMode,
      status: 'pending',
      created_at: createdAt,
      updated_at: createdAt
    };
    try {
      await chatV2Outbox.put(outboxEntry);
      outboxStored = true;
      await chatV2Outbox.update(clientTxnId, { status: 'sending', updated_at: new Date().toISOString() });
    } catch (outboxError) {
      console.warn('Chat v2 outbox persistence failed; sending online without durable retry', outboxError);
    }
    try {
      await chatV2Api.sendMessage(sendConversationId, clientTxnId, {
        content: effectiveContent,
        attachments,
        client_message_id: effectiveClientMessageId,
        chat_mode: effectiveChatMode
      });
      if (outboxStored) {
        await chatV2Outbox.update(clientTxnId, { status: 'acked', updated_at: new Date().toISOString() }).catch(() => undefined);
      }
      // Cursor-correct incremental sync instead of a full snapshot fetch: a
      // snapshot resolved while live WS frames are streaming replaces the
      // cursor with a value BEHIND the frames already applied, making every
      // subsequent frame mismatch -> gapped -> another recovery (visible
      // re-sort/flicker at the start of each turn). /sync advances from the
      // current cursor and falls back to a snapshot only when actually needed.
      await recoverChatV2Canonical(sendConversationId);
    } catch (caughtError) {
      if (outboxStored) {
        await chatV2Outbox.update(clientTxnId, {
          status: 'failed',
          updated_at: new Date().toISOString(),
          last_error: asApiError(caughtError).message
        }).catch(() => undefined);
      }
      if (!shouldApplyChatSendFailureSideEffects(sendConversationId, conversationIdFromRoute())) return;
      awaitingAssistantStart = false;
      turnInProgress = chatV2Store.snapshot.runtime?.has_active_turn === true;
      currentActiveTurnId = chatV2Store.snapshot.runtime?.active_turn?.turn_id ?? null;
      applyChatV2StateToTimeline();
      await recoverChatV2Snapshot(sendConversationId).catch(() => undefined);
      // Keep the durable optimistic row visible, but make admission failure
      // explicit instead of leaving it indefinitely in the sending state.
      chatV2Store.markOptimisticUserFailed(effectiveClientMessageId);
      addToast(asApiError(caughtError).message, 'error');
    }
  }

  async function uploadFiles(files: File[]): Promise<void> {
    if (files.length === 0) return;
    try {
      const uploaded = await Promise.all(files.map((file) => api.artifacts.upload(file)));
      composerAttachments = [...composerAttachments, ...uploaded];
      addToast(files.length === 1 ? 'Attachment uploaded.' : `${files.length} attachments uploaded.`, 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to upload attachment');
    }
  }

  function uniqueNewPastedFiles(files: File[]): File[] {
    const now = Date.now();
    for (const [key, seenAt] of recentPastedFileFingerprints) {
      if (now - seenAt > PASTE_DUPLICATE_SUPPRESSION_MS) {
        recentPastedFileFingerprints.delete(key);
      }
    }

    const unique: File[] = [];
    for (const file of files) {
      const key = pastedFileFingerprint(file);
      const seenAt = recentPastedFileFingerprints.get(key);
      if (typeof seenAt === 'number' && now - seenAt <= PASTE_DUPLICATE_SUPPRESSION_MS) {
        continue;
      }
      recentPastedFileFingerprints.set(key, now);
      unique.push(file);
    }
    return unique;
  }

  function removeAttachment(artifactId: string): void {
    composerAttachments = composerAttachments.filter((item) => item.artifact_id !== artifactId);
  }

  function handleDragEnter(event: DragEvent): void {
    event.preventDefault();
    dragCounter++;
    if (dragCounter === 1) showDropZone = true;
  }

  function handleDragLeave(event: DragEvent): void {
    event.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
      dragCounter = 0;
      showDropZone = false;
    }
  }

  function handleDragOver(event: DragEvent): void {
    event.preventDefault();
  }

  async function handleDrop(event: DragEvent): Promise<void> {
    event.preventDefault();
    dragCounter = 0;
    showDropZone = false;
    const files = event.dataTransfer?.files;
    if (files && files.length > 0) {
      await uploadFiles(Array.from(files));
    }
  }

  async function handlePaste(event: ClipboardEvent): Promise<void> {
    let files = pastedFilesFromClipboardData(event.clipboardData);
    if (files.length > 0) {
      files = uniqueNewPastedFiles(files);
      if (files.length === 0) return;
      event.preventDefault();
      await uploadFiles(files);
      return;
    }
    files = await readPastedFilesFromNavigator();
    if (files.length > 0) {
      files = uniqueNewPastedFiles(files);
      if (files.length === 0) return;
      event.preventDefault();
      await uploadFiles(files);
    }
  }

  async function retryFailedTurn(turnId: string | null): Promise<void> {
    if (!currentConversation || !turnId || turnInProgress || awaitingAssistantStart) return;
    error = '';
    awaitingAssistantStart = true;
    try {
      await chatV2Api.retryTurn(currentConversation.conversation_id, turnId, {
        client_txn_id: crypto.randomUUID()
      });
      lastRecoverableMessage = '';
      lastRecoverableTurnId = null;
      await recoverChatV2Canonical(currentConversation.conversation_id);
    } catch (caughtError) {
      const apiError = asApiError(caughtError);
      error = apiError.message;
      if (shouldClearRecoverableRetry(caughtError)) {
        suppressedRecoverableTurnId = turnId;
        lastRecoverableMessage = '';
        lastRecoverableTurnId = null;
      }
      awaitingAssistantStart = false;
      turnInProgress = chatV2Store.snapshot.runtime?.has_active_turn === true;
      currentActiveTurnId = chatV2Store.snapshot.runtime?.active_turn?.turn_id ?? null;
      await recoverChatV2Snapshot(currentConversation.conversation_id).catch(() => undefined);
      reportError(caughtError);
    }
  }

  async function retryLastTurn(): Promise<void> {
    await retryFailedTurn(lastRecoverableTurnId);
  }

  async function continueFailedTurn(): Promise<void> {
    await retryFailedTurn(retryableFailedTurnId);
  }

  function syncComposerHeight(): void {
    if (!composerElement) return;
    composerElement.style.height = '0px';
    composerElement.style.height = `${Math.min(composerElement.scrollHeight, 220)}px`;
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    // Slash suggestion navigation
    if (slashSuggestionsVisible) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        slashSelectedIndex = (slashSelectedIndex + 1) % slashFilteredSuggestions.length;
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        slashSelectedIndex = (slashSelectedIndex - 1 + slashFilteredSuggestions.length) % slashFilteredSuggestions.length;
        return;
      }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
        event.preventDefault();
        acceptSlashSuggestion(slashSelectedIndex);
        return;
      }
      if (event.key === 'Escape') {
        slashSuggestionsVisible = false;
        return;
      }
    }
    // Enter key behaviour:
    //   - Cmd/Ctrl+Enter always sends, regardless of the toggle. Covers
    //     desktop users who want the keyboard shortcut without enabling
    //     Enter-to-send as the default.
    //   - Enter alone only sends when the user has opted into it.
    //   - Shift+Enter is always a newline (delegated to the textarea).
    if (event.key !== 'Enter' || event.shiftKey) return;
    if (event.metaKey || event.ctrlKey) {
      event.preventDefault();
      void handleSend();
      return;
    }
    if (!enterToSend) return;
    event.preventDefault();
    void handleSend();
  }

  async function handleEscalationDecision(callId: string, decision: 'approve' | 'deny'): Promise<void> {
    const current = escalations.find((item) => item.call_id === callId) ?? null;
    if (current === null) return;
    escalationResolutionPending = current;
    escalationBusyCallId = callId;
    try {
      await api.notifications.resolve(callId, { decision });
    } catch (caughtError) {
      escalationResolutionPending = null;
      escalationBusyCallId = null;
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to resolve approval');
    }
  }

  async function sendConversationModeText(text: string): Promise<void> {
    if (!currentConversation || !text.trim()) return;
    const clientTxnId = crypto.randomUUID();
    try {
      await chatV2Api.sendMessage(currentConversation.conversation_id, clientTxnId, {
        content: text.trim(),
        attachments: [],
        client_message_id: clientTxnId
      });
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to send message');
    }
  }

  function escalationSecondsRemaining(esc: Escalation): number {
    const timeout = esc.timeout_seconds ?? escalationTimeoutSeconds;
    const elapsed = (Date.now() - (esc.received_at ?? Date.now())) / 1000;
    return Math.max(0, Math.ceil(timeout - elapsed));
  }

  function nextAnimationFrame(): Promise<void> {
    if (typeof requestAnimationFrame === 'undefined') return Promise.resolve();
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
  }

  function shouldFillTimelineViewport(): boolean {
    return shouldAutoLoadOlderForViewport({
      visibleStartIndex,
      hasOlderMessages,
      loadingOlderMessages,
      scrollHeight: timelineEl?.scrollHeight ?? 0,
      clientHeight: timelineEl?.clientHeight ?? 0,
    });
  }

  function scheduleTimelineViewportFill(): void {
    if (timelineViewportFillScheduled || timelineViewportFillInFlight) return;
    if (typeof requestAnimationFrame === 'undefined') return;
    timelineViewportFillScheduled = true;
    requestAnimationFrame(() => {
      timelineViewportFillScheduled = false;
      void ensureTimelineViewportFilled();
    });
  }

  async function backfillOlderForViewportFillPreservingTail(): Promise<boolean> {
    if (!olderMessagesCursor || !currentConversation) return false;

    loadingOlderMessages = true;
    try {
      const beforeCount = renderableVisibleItems.length;
      const beforeCursor = olderMessagesCursor;
      const response = await chatV2Api.timeline(currentConversation.conversation_id, {
        before: olderMessagesCursor
      });
      chatV2Store.applyBackfill(response);
      applyChatV2StateToTimeline();
      olderMessagesCursor = response.before_cursor ?? null;
      hasOlderMessages = Boolean(response.has_more_before && olderMessagesCursor);

      const prependedCount = Math.max(0, renderableVisibleItems.length - beforeCount);
      timelineWindow = windowAfterViewportFillBackfill(
        timelineWindow,
        prependedCount,
        renderableVisibleItems.length,
        true,
      );
      saveCurrentConversationView();
      await tick();
      await nextAnimationFrame();
      scheduleScrollToBottom(false, 1);

      return prependedCount > 0 || beforeCursor !== olderMessagesCursor;
    } catch (caughtError) {
      const message = asApiError(caughtError).message;
      if (isRecoverableBackfillCursorError(caughtError)) {
        olderMessagesCursor = null;
        hasOlderMessages = false;
      } else {
        addToast(`Unable to load older messages: ${message}`, 'error');
      }
      return false;
    } finally {
      loadingOlderMessages = false;
    }
  }

  async function ensureTimelineViewportFilled(): Promise<void> {
    if (timelineViewportFillInFlight) return;
    timelineViewportFillInFlight = true;
    try {
      for (let attempt = 0; attempt < TIMELINE_VIEWPORT_FILL_MAX_ATTEMPTS; attempt += 1) {
        await tick();
        if (!shouldFillTimelineViewport()) break;
        if (visibleStartIndex <= 0) {
          if (!hasOlderMessages) break;
          if (!userScrolledUp && timelineWindow.end === null) {
            const changed = await backfillOlderForViewportFillPreservingTail();
            if (!changed) break;
            continue;
          }
          await loadOlder();
          await tick();
          await nextAnimationFrame();
          continue;
        }

        const beforeStart = visibleStartIndex;
        const beforeEnd = visibleEndIndex;
        const beforeCount = renderableVisibleItems.length;
        const wasTailPinned = !userScrolledUp && timelineWindow.end === null;
        const anchor = wasTailPinned ? null : captureTimelineAnchor();
        timelineWindow = expandWindowUpPreservingLiveTail(timelineWindow, renderableVisibleItems.length);
        await tick();
        await nextAnimationFrame();
        if (wasTailPinned) {
          scheduleScrollToBottom(false, 1);
        } else {
          restoreTimelineAnchor(anchor);
        }

        const changed = beforeStart !== visibleStartIndex
          || beforeEnd !== visibleEndIndex
          || beforeCount !== renderableVisibleItems.length;
        if (!changed) break;
      }
    } finally {
      timelineViewportFillInFlight = false;
    }
  }

  async function loadOlder(): Promise<void> {
    if (!timelineEl || loadingOlderMessages) return;

    if (visibleStartIndex === 0 && (!hasOlderMessages || !olderMessagesCursor || !currentConversation)) {
      return;
    }

    loadingOlderMessages = true;

    try {
      // Element-anchored restore: the reference row stays visually fixed across
      // the prepend, immune to a simultaneous tail-window change. The anchor is
      // captured immediately before each DOM-mutating apply — never before the
      // network fetch (capturing before async work snapped actively-scrolling
      // users back to the pre-fetch position).
      if (visibleStartIndex > 0) {
        const anchor = captureTimelineAnchor();
        timelineWindow = expandWindowUp(timelineWindow, renderableVisibleItems.length);
        await tick();
        requestAnimationFrame(() => {
          restoreTimelineAnchor(anchor);
          loadingOlderMessages = false;
        });
      } else if (olderMessagesCursor && currentConversation) {
        const beforeCount = renderableVisibleItems.length;
        const response = await chatV2Api.timeline(currentConversation.conversation_id, {
          before: olderMessagesCursor
        });
        const anchor = captureTimelineAnchor();
        chatV2Store.applyBackfill(response);
        applyChatV2StateToTimeline();
        olderMessagesCursor = response.before_cursor ?? null;
        hasOlderMessages = Boolean(response.has_more_before && olderMessagesCursor);
        // Keep the rows the user was reading mounted (do NOT reset to the oldest
        // page): shift the window down by however many rows were prepended so
        // the same newer rows stay rendered and the anchor restore is exact.
        const prependedCount = Math.max(0, renderableVisibleItems.length - beforeCount);
        timelineWindow = windowAfterPrepend(timelineWindow, prependedCount, renderableVisibleItems.length);
        saveCurrentConversationView();
        await tick();
        requestAnimationFrame(() => {
          restoreTimelineAnchor(anchor);
          loadingOlderMessages = false;
        });
      } else {
        loadingOlderMessages = false;
      }
    } catch (caughtError) {
      const message = asApiError(caughtError).message;
      // A stale before-cursor after compaction/lineage change is recoverable:
      // clear it so the next scroll re-fetches from the fresh window instead of
      // failing forever. Only surface a toast for genuinely unexpected errors.
      if (isRecoverableBackfillCursorError(caughtError)) {
        olderMessagesCursor = null;
        hasOlderMessages = false;
      } else {
        addToast(`Unable to load older messages: ${message}`, 'error');
      }
      loadingOlderMessages = false;
      // No scroll restore on failure: nothing mutated the timeline DOM, so the
      // user's current position is already correct.
    }
  }

  function isRecoverableBackfillCursorError(caughtError: unknown): boolean {
    const apiError = asApiError(caughtError);
    const message = (apiError.message ?? '').toLowerCase();
    return (
      apiError.status === 400
      && (message.includes('lineage') || message.includes('cursor'))
    );
  }

  async function openCreatedConversation(conversationId: string): Promise<void> {
    let createdConversation: Conversation | null = null;

    try {
      createdConversation = await api.conversations.detail(conversationId, { includeState: false });
      const desiredStatusFilter = conversationStatusFilterForConversation(createdConversation, selectedConversationStatus);
      if (selectedConversationStatus !== desiredStatusFilter) {
        selectedConversationStatus = desiredStatusFilter;
      }
      mergeConversationList([createdConversation]);
      patchAgentDirectChat(createdConversation);
    } catch {
      // Navigation will let the regular conversation loader surface detail
      // failures; the sidebar refresh below is only opportunistic.
    }

    await goto(conversationUrl(conversationId));

    void refreshSidebarData()
      .then(() => {
        if (createdConversation) {
          mergeConversationList([createdConversation]);
          patchAgentDirectChat(createdConversation);
        }
      })
      .catch(() => {
        if (createdConversation) {
          mergeConversationList([createdConversation]);
          patchAgentDirectChat(createdConversation);
        }
      });
  }

  async function patchCreatedConversationRow(conversationId: string): Promise<void> {
    try {
      const createdConversation = await api.conversations.detail(conversationId, { includeState: false });
      if (!conversationMatchesSidebarProjectionFilter(createdConversation, currentSidebarProjectionFilter())) {
        return;
      }
      mergeConversationList([createdConversation]);
      patchAgentDirectChat(createdConversation);
      rememberSidebarProjection();
    } catch {
      await refreshSidebarData();
    }
  }

  function handleSocketEvent(event: import('$lib/types/api').CognisWebSocketEvent | ChatRealtimeFrame): void {
    const currentId = conversationIdFromRoute();
    const chatV2OwnsCurrent = chatV2OwnsActiveConversation(currentId);
    const legacyLifecycleEnabled = shouldApplyLegacyLifecycleFrame(chatV2OwnsCurrent);
    if (event.type === 'chat_v2_frame') {
      if (event.conversation_id === currentId) {
        void applyChatV2Frame(event);
      }
      return;
    }
    const eventSessionId = 'session_id' in event && typeof event.session_id === 'string' ? event.session_id : null;
    const eventPreviousSessionId = event.type === 'session_compacted' ? event.previous_session_id : null;
    // A new conversation was created on another device/tab. Newer servers send
    // the hydrated sidebar row so this can stay local; fall back to a full
    // projection refresh for older/no-row payloads.
    if (event.type === 'sidebar_conversation_upsert') {
      const upsert = event as typeof event & { conversation?: Conversation | null };
      if (upsert.conversation) {
        applySidebarConversationUpsert(upsert.conversation);
      } else {
        void refreshSidebarData();
      }
      return;
    }
    if (event.type === 'sidebar_conversation_removed') {
      removeConversationFromSidebar(event.conversation_id, {
        markCurrentDeleted: event.conversation_id === currentId,
      });
      return;
    }
    if (event.type === 'conversation_created') {
      void openCreatedConversation(event.conversation_id);
      return;
    }
    if (
      event.type === 'conversation_updated'
      && typeof (event as { created_conversation_id?: unknown }).created_conversation_id === 'string'
    ) {
      void patchCreatedConversationRow((event as { created_conversation_id: string }).created_conversation_id);
    }

    if ('conversation_id' in event && event.conversation_id && event.conversation_id !== currentId) {
      // Event for a different conversation — mark it as unread locally
      // and show a browser notification if appropriate.
      const otherConvId = event.conversation_id;
      if (event.type === 'conversation_state_snapshot') {
        const todos = backendTodoSnapshot(event.state);
        if (todos !== null) {
          setConversationTodoSnapshot(otherConvId, todos);
        }
        patchConversationInList(
          otherConvId,
          conversationStateConversationPatch(event.state),
          { pendingNotificationSource: 'server' },
        );
        return;
      }
      if (event.type === 'conversation_state_delta') {
        const replacement = event.replace?.state;
        if (replacement && typeof replacement === 'object') {
          const state = replacement as ConversationStateEnvelope;
          const todos = backendTodoSnapshot(state);
          if (todos !== null) {
            setConversationTodoSnapshot(otherConvId, todos);
          }
          patchConversationInList(
            otherConvId,
            conversationStateConversationPatch(state),
            { pendingNotificationSource: 'server' },
          );
        }
        return;
      }
      if (event.type === 'conversation_updated' && event.conversation_id) {
        const patch = conversationUpdatedRowPatch(event);
        if (Object.keys(patch).length > 0) {
          patchConversationInList(event.conversation_id, patch, {
            touchUpdatedAt: typeof event.title === 'string' || typeof event.updated_at === 'string',
            touchLastMessageAt: typeof event.last_message_at === 'string',
            pendingNotificationSource: Array.isArray(event.pending_notification_types) ? 'server' : undefined,
          });
        }
      } else if (
        event.type === 'turn_started'
        || (event.type === 'queued' && event.command_result !== true)
         || event.type === 'delegation_started'
      ) {
        setConversationTurnIndicator(
          otherConvId,
          true,
          event.type === 'turn_started' ? { touchLastMessageAt: true } : {}
        );
        if (event.type === 'turn_started') {
          patchConversationInList(otherConvId, {
            active_turn_chat_mode: normalizeChatModeTone(event.chat_mode),
            active_turn_chat_mode_source: event.chat_mode_source ?? null,
          });
        }
      } else if (event.type === 'turn_settled') {
        clearConversationTurnState(otherConvId, event.completed_at);
      } else if (event.type === 'task_paused') {
        clearConversationTurnState(otherConvId);
      } else if (event.type === 'message_complete') {
        clearConversationTurnState(otherConvId, event.completed_at);
      } else if (event.type === 'workflow_completed' || event.type === 'workflow_failed' || event.type === 'workflow_cancelled') {
        clearConversationTurnState(otherConvId);
      }
      const pendingNotificationType = notificationTypeForEvent(event.type);
      if (pendingNotificationType) {
        if (event.type.endsWith('_resolved')) {
          refreshConversationPendingNotificationTypes(otherConvId);
        } else {
          patchConversationPendingNotificationType(otherConvId, pendingNotificationType, true);
        }
      }
      if (event.type === 'message_complete' || event.type === 'workflow_completed' || event.type === 'workflow_failed') {
        const idx = conversations.findIndex((c) => c.conversation_id === otherConvId);
        const agentDirectConversation = agentDirectChats.find(
          (item) => item.conversation.conversation_id === otherConvId
        )?.conversation;
        const conversation = idx >= 0 ? conversations[idx] : agentDirectConversation ?? null;
        const agentWorkConversation = isManagedConversation(conversation);
        if (idx >= 0) {
          if (event.type === 'message_complete') {
            patchConversationInList(
              otherConvId,
              {
                has_unread: !agentWorkConversation,
                last_message_at: event.completed_at ?? undefined,
              },
              { touchLastMessageAt: true }
            );
          } else {
            patchConversationInList(otherConvId, { has_unread: !agentWorkConversation });
          }
        }
        if (agentWorkConversation) {
          return;
        }
        // Browser notification
        const convTitle = conversation?.title ?? 'Conversation';
        const agentId = conversation?.agent_id ?? '';
        const agentObj = agents.find((a) => a.agent_id === agentId);
        const agentLabel = agentObj?.display_name ?? agentObj?.name ?? 'Cognis';
        if (event.type === 'workflow_completed') {
          notifyIfHidden(agentLabel, `Task completed in "${convTitle}"`, otherConvId, currentId);
        } else if (event.type === 'workflow_failed') {
          notifyIfHidden(agentLabel, `Task failed in "${convTitle}"`, otherConvId, currentId);
        } else {
          notifyIfHidden(agentLabel, `New message in "${convTitle}"`, otherConvId, currentId);
        }
      }
      if (event.type === 'turn_settled') {
        patchConversationInList(
          otherConvId,
          { last_message_at: event.completed_at ?? undefined },
          { touchLastMessageAt: true }
        );
      }
      return;
    }

    if (
      isForeignSessionTimelineEvent({
        eventType: event.type,
        eventSessionId,
        eventPreviousSessionId,
        rootSessionId: currentConversation?.active_session_id,
      })
    ) {
      return;
    }

    if (event.type === 'session_compaction_started') {
      // Chat v2 surfaces compaction lifecycle via canonical frames.
      return;
    }

    if (event.type === 'session_compaction_finished') {
      // Chat v2 surfaces compaction lifecycle via canonical frames.
      return;
    }

    if (currentConversation) {
      if (event.type === 'message_complete') {
        patchConversationInList(
          currentConversation.conversation_id,
          {
            has_unread: false,
            last_message_at: event.completed_at ?? undefined,
            last_read_at: event.completed_at ?? new Date().toISOString(),
          },
          { touchLastMessageAt: true }
        );
      } else if (
         event.type === 'delegation_started'
        || event.type === 'delegation_completed'
        || event.type === 'delegation_failed'
        || event.type === 'workflow_composed'
        || event.type === 'workflow_step_started'
        || event.type === 'workflow_step_completed'
        || event.type === 'workflow_completed'
        || event.type === 'workflow_failed'
        || event.type === 'workflow_cancelled'
        || event.type === 'session_compacted'
      ) {
        patchConversationInList(currentConversation.conversation_id, { has_unread: false });
      }
    }

    if (
      chatV2OwnsCurrent
      && (
        event.type === 'delegation_completed'
        || event.type === 'delegation_failed'
        || event.type === 'workflow_step_completed'
        || event.type === 'workflow_completed'
        || event.type === 'workflow_failed'
        || event.type === 'workflow_cancelled'
        || event.type === 'message_complete'
        || event.type === 'turn_settled'
        || event.type === 'queued_messages_updated'
      )
    ) {
      // Terminal turn boundaries pull canonical state immediately so streamed
      // content does not blink out when the runtime overlay is cleared at
      // settle. Progress-only events stay debounced to avoid refetch storms.
      const immediate =
        event.type === 'message_complete'
        || event.type === 'turn_settled'
        || event.type === 'workflow_completed'
        || event.type === 'workflow_failed'
        || event.type === 'workflow_cancelled';
      scheduleChatV2CanonicalRecovery(currentId, { immediate });
      return;
    }

    if (event.type === 'system_message' && event.command_result === true) {
      if (
        currentConversation &&
        currentConversation.conversation_id === event.conversation_id &&
        (event.chat_mode === 'plan' || event.chat_mode === 'build' || event.chat_mode === 'default')
      ) {
        const platformData = { ...(currentConversation.context?.platform_data ?? {}) };
        if (event.chat_mode === 'default') {
          delete platformData.chat_mode;
        } else {
          platformData.chat_mode = event.chat_mode;
        }
        const context = { ...currentConversation.context, platform_data: platformData };
        currentConversation = { ...currentConversation, context };
        patchConversationInList(currentConversation.conversation_id, { context });
      }
      if (currentConversation && event.resolved_agent_profile_id) {
        patchConversationInList(currentConversation.conversation_id, {
          agent_profile_id: event.resolved_agent_profile_id
        });
      }
      if (currentConversation && event.executor_id) {
        patchConversationInList(currentConversation.conversation_id, {
          active_executor_id: event.executor_id,
          active_executor_source: 'user'
        });
      }
      appendChatV2LocalSystemMessage(
        event.text,
        `system:${event.command ?? event.code ?? 'message'}`,
        event.notice_id
      );
      return;
    }

    if (event.type === 'queued' && event.command_result === true) {
      appendChatV2LocalSystemMessage(commandQueuedMessage(event), `queued:${event.command ?? event.task_id ?? 'task'}`);
      return;
    }

    if (legacyLifecycleEnabled && (event.type === 'queued_messages_updated' || event.type === 'queued' || event.type === 'message_complete' || event.type === 'turn_settled')) {
      queuedCount = event.queued_count ?? 0;
      if ('messages' in event && Array.isArray(event.messages)) {
        applyQueuedMessageSnapshot(event.messages, event.queued_count ?? event.messages.length);
      } else if (queuedCount === 0) {
        queuedMessages = [];
      }
    }

    if (legacyLifecycleEnabled && (event.type === 'turn_started' || event.type === 'queued')) {
      turnInProgress = true;
      if (event.type === 'turn_started') {
        // Track the current turn_id so we can guard against late patches
        // re-arming turnInProgress after cancel (Issue B client guard).
        if (event.turn_id) {
          currentActiveTurnId = event.turn_id;
          lastSettledTurnId = null;
        }
        activeTurnChatMode = normalizeChatModeTone(event.chat_mode);
        if (currentConversation) {
          patchConversationInList(currentConversation.conversation_id, {
            active_turn_chat_mode: activeTurnChatMode,
            active_turn_chat_mode_source: event.chat_mode_source ?? null,
          });
        }
      }
      setConversationTurnIndicator(currentConversation?.conversation_id, true);
    }

    if (event.type === 'error') {
      const nextError = socketErrorMessage(event);
      if (isNonFatalWebSocketBackpressureError({ code: event.code, message: nextError })) {
        return;
      }
      if (chatV2OwnsCurrent) {
        if (event.code === 'session_ended' && currentConversation) {
          void api.conversations.sessions(currentConversation.conversation_id).then((s) => {
            sessions = s;
            syncActiveSessionAttention(currentConversation?.conversation_id, s);
          }).catch(() => {});
        }
        return;
      }
      if (shouldSuppressPreSessionSocketError({
        code: event.code,
        message: nextError,
        conversation: currentConversation,
        sessionCount: sessions.length,
      })) {
        error = '';
        awaitingAssistantStart = false;
        turnInProgress = false;
        clearConversationTurnState(currentConversation?.conversation_id);
        directQuestionSubmitting = false;
        return;
      }
      // User-initiated cancellation is not really an error condition —
      // show a transient toast and skip the persistent red banner so it
      // can't get stuck on screen without any dismiss affordance.
      if (event.code === 'turn_cancelled') {
        addToast(nextError, 'info', 3_000);
        error = '';
        awaitingAssistantStart = false;
        turnInProgress = false;
        // Record the settled turn_id for diagnostics and retry state.
        if (currentActiveTurnId) lastSettledTurnId = currentActiveTurnId;
        currentActiveTurnId = null;
        clearConversationTurnState(currentConversation?.conversation_id);
        directQuestionSubmitting = false;
        pendingDirectQuestion = null;
        directQuestionPageIndex = 0;
        directQuestionCollapsed = false;
        if (escalationBusyCallId) {
          escalationBusyCallId = null;
          escalationResolutionPending = null;
          void refreshEscalations();
        }
        return;
      }
      if (event.code === 'session_ended') {
        if (currentConversation) {
          void api.conversations
            .sessions(currentConversation.conversation_id)
            .then((s) => {
              sessions = s;
              syncActiveSessionAttention(currentConversation?.conversation_id, s);
            })
            .catch(() => {});
        }
      }
      error = nextError;
      if (event.code === 'pending_input_request') {
        void refreshPendingDirectQuestion();
      }
      awaitingAssistantStart = false;
      turnInProgress = false;
      clearConversationTurnState(currentConversation?.conversation_id);
      directQuestionSubmitting = false;
      if (escalationBusyCallId) {
        escalationBusyCallId = null;
        escalationResolutionPending = null;
        void refreshEscalations();
      }
      if (event.recoverable) {
        lastRecoverableMessage = lastSubmittedMessage;
        const retryEvent = event as { turn_id?: string };
        const recoverableTurnId = typeof retryEvent.turn_id === 'string'
          ? retryEvent.turn_id
          : currentActiveTurnId;
        lastRecoverableTurnId = recoverableTurnId === suppressedRecoverableTurnId
          ? null
          : recoverableTurnId;
      }
      return;
    }

    if (event.type === 'conversation_state_snapshot') {
      applyConversationStateSnapshot(event.state);
      return;
    }

    if (event.type === 'conversation_state_delta') {
      applyConversationStateDelta(event);
      return;
    }

    if (event.type === 'conversation_runtime_snapshot') {
      if (!chatV2OwnsCurrent) {
        applyConversationRuntimeSnapshot(event);
      }
      return;
    }

    if (!chatV2OwnsCurrent && event.type === 'delegation_started') {
      awaitingAssistantStart = false;
      turnInProgress = true;
      setConversationTurnIndicator(currentConversation?.conversation_id, true);
    }

    const pendingNotificationType = notificationTypeForEvent(event.type);
    if (pendingNotificationType) {
      if (event.type.endsWith('_resolved')) {
        refreshConversationPendingNotificationTypes(currentConversation?.conversation_id);
      } else {
        patchConversationPendingNotificationType(
          currentConversation?.conversation_id,
          pendingNotificationType,
          true,
        );
      }
    }

    if (
      'session_id' in event
      && shouldAdoptConversationSessionId(currentConversation?.active_session_id, event.type, event.session_id)
    ) {
      syncConversationActiveSession(event.session_id);
    }

    if (legacyLifecycleEnabled && (event.type === 'turn_settled' || event.type === 'task_paused')) {
      awaitingAssistantStart = false;
      turnInProgress = false;
      if (currentConversation) {
        clearConversationTurnState(
          currentConversation.conversation_id,
          event.type === 'turn_settled' ? event.completed_at : undefined,
        );
      }
      directQuestionSubmitting = false;
    }

    if (event.type === 'message_complete' || event.type === 'workflow_completed' || event.type === 'workflow_failed' || event.type === 'workflow_cancelled') {
      if (!chatV2OwnsCurrent) {
        awaitingAssistantStart = false;
        turnInProgress = false;
      // Record the settled turn_id (same guard as cancel path — Issue B).
      if (currentActiveTurnId) lastSettledTurnId = currentActiveTurnId;
      if (event.type === 'message_complete' && event.turn_id) lastSettledTurnId = event.turn_id;
      currentActiveTurnId = null;
      clearConversationTurnState(
        currentConversation?.conversation_id,
        event.type === 'message_complete' ? event.completed_at : undefined,
      );
      if (directQuestionSubmitting) {
        pendingDirectQuestion = null;
        directQuestionPageIndex = 0;
        directQuestionCollapsed = false;
      }
        directQuestionSubmitting = false;
      }
      // Update context usage from message_complete
      if (event.type === 'message_complete' && event.context_usage) {
        applyContextUsageSnapshot(event.context_usage);
      }
      if (event.type === 'message_complete') {
        applyLastGenerationSnapshot(event.last_generation);
      }
      if (currentConversation && event.type === 'message_complete') {
        const agentObj = agents.find((a) => a.agent_id === currentConversation?.agent_id);
        const agentLabel = agentObj?.display_name ?? agentObj?.name ?? 'Cognis';
        const convTitle = currentConversation.title ?? 'Conversation';
        notifyIfHidden(agentLabel, `New message in "${convTitle}"`, currentConversation.conversation_id, currentConversation.conversation_id);
      }
      // Mark as read since the user is viewing this conversation
      if (currentConversation && !document.hidden) {
        markConversationReadLocally(currentConversation.conversation_id);
        api.conversations.markRead(currentConversation.conversation_id).catch(() => {});
      }
    }

    if (
      currentConversation &&
      (event.type === 'delegation_started' ||
        event.type === 'delegation_completed' ||
        event.type === 'delegation_failed')
    ) {
      api.conversations
        .sessions(currentConversation.conversation_id)
        .then((s) => {
          sessions = s;
          syncActiveSessionAttention(currentConversation?.conversation_id, s);
        })
        .catch(() => {});
    }

    // Escalation push events
    if (event.type === 'escalation') {
      const existing = escalations.find((e) => e.call_id === event.call_id);
      if (!existing && escalationResolutionPending?.call_id !== event.call_id) {
        escalations = sortEscalations([...escalations, {
          call_id: event.call_id,
          session_id: event.session_id ?? null,
          tool_name: event.tool_name,
          decision: 'escalate',
          resolved: false,
          reasoning: event.reasoning,
          risk: event.risk,
          timeout_seconds: event.timeout_seconds,
          received_at: Date.now(),
        }]);
        startEscalationCountdown();
        if (!chatV2OwnsCurrent) {
          scrollToBottom(false);
        }
      }
      return;
    }

    if (event.type === 'escalation_resolved') {
      escalations = escalations.filter((e) => e.call_id !== event.call_id);
      escalationBusyCallId = null;
      escalationResolutionPending = null;
      if (escalations.length === 0) stopEscalationCountdown();
      return;
    }

    // Handle conversation_updated for title and activity changes.
    if (event.type === 'conversation_updated') {
      if (currentConversation && event.conversation_id === currentConversation.conversation_id) {
        const patch = conversationUpdatedRowPatch(event);
        if (chatV2OwnsCurrent) {
          delete patch.has_active_turn;
          delete patch.active_turn_chat_mode;
          delete patch.active_turn_chat_mode_source;
        } else if (typeof event.has_active_turn === 'boolean') {
          turnInProgress = event.has_active_turn;
        }
        if (Object.keys(patch).length > 0) {
          patchConversationInList(currentConversation.conversation_id, patch, {
            touchUpdatedAt: typeof event.title === 'string' || typeof event.updated_at === 'string',
            touchLastMessageAt: typeof event.last_message_at === 'string',
            pendingNotificationSource: Array.isArray(event.pending_notification_types) ? 'server' : undefined,
          });
        }
      }
      return;
    }

    // Handle session_compacted: Chat v2 frames carry the compaction summary;
    // here we only refresh the session list and active session.
    if (event.type === 'session_compacted') {
      syncConversationActiveSession(event.session_id);
      if (!chatV2OwnsCurrent) {
        awaitingAssistantStart = false;
        turnInProgress = false;
        setConversationTurnIndicator(currentConversation?.conversation_id, false);
      }
      if (event.command_result === true) {
        appendChatV2LocalSystemMessage(sessionCompactedMessage(event), 'session_compacted');
      }
      if (chatV2OwnsCurrent) {
        scheduleChatV2CanonicalRecovery(currentId, { immediate: true });
      }
      // Refresh session list to show the new session
      if (currentConversation) {
        api.conversations.sessions(currentConversation.conversation_id)
          .then((s) => {
            sessions = s;
            syncActiveSessionAttention(currentConversation?.conversation_id, s);
          })
          .catch(() => {});
      }
      return;
    }

    // Handle session_reset: clear timeline for new session
    if (event.type === 'session_reset') {
      syncConversationActiveSession(event.session_id);
      if (!chatV2OwnsCurrent) {
        awaitingAssistantStart = false;
        turnInProgress = false;
        setConversationTurnIndicator(currentConversation?.conversation_id, false);
        activeSessionLastSeq = 0;
        olderMessagesCursor = null;
        hasOlderMessages = false;
        chatV2Store.reset();
        syncVisibleWindow();
        scrollToBottom(true);
      } else {
        scheduleChatV2CanonicalRecovery(currentId, { immediate: true });
      }
      // Refresh session list
      if (currentConversation) {
        api.conversations.sessions(currentConversation.conversation_id)
          .then((s) => {
            sessions = s;
            syncActiveSessionAttention(currentConversation?.conversation_id, s);
          })
          .catch(() => {});
      }
      return;
    }

    // Handle history_rebased: reload the same conversation in place.
    if (event.type === 'history_rebased') {
      if (!chatV2OwnsCurrent) {
        awaitingAssistantStart = false;
        turnInProgress = false;
        activeSessionLastSeq = 0;
        olderMessagesCursor = null;
        hasOlderMessages = false;
        queuedCount = 0;
        queuedMessages = [];
        directQuestionSubmitting = false;
        pendingDirectQuestion = null;
        pendingCredentialRequest = null;
        directQuestionPageIndex = 0;
        directQuestionCollapsed = false;
        escalationBusyCallId = null;
        escalationResolutionPending = null;
        setConversationTurnIndicator(currentConversation?.conversation_id, false);
      } else {
        scheduleChatV2CanonicalRecovery(currentId, { immediate: true });
      }
      if (event.session_id) {
        syncConversationActiveSession(event.session_id);
      }
      if (event.message) {
        addToast(event.message, 'info', 2_000);
      }
      if (currentConversation) {
        const requestId = beginConversationLoad();
        void reloadConversationSubloads(currentConversation.conversation_id, requestId, {
          reloadSessions: true,
          reloadHistory: true,
          resubscribe: true,
        });
      }
      return;
    }

    if (event.type === 'reconnected') {
      void resyncSidebarData('websocket-reconnect', { force: true });
      const previousTurnInProgress = turnInProgress;
      if (!chatV2OwnsCurrent) {
        awaitingAssistantStart = false;
        turnInProgress = event.has_active_turn ?? hasAnyActiveTurnTimelineItem();
        setConversationTurnIndicator(currentConversation?.conversation_id, turnInProgress);
      }
      if (currentConversation) {
        const shouldRefresh = shouldReconcileAfterReconnect({
          remoteLastSeq: event.last_seq,
          activeSessionLastSeq,
          remoteHasActiveTurn: event.has_active_turn,
          localTurnInProgress: previousTurnInProgress,
        });
        void refreshConversationView(shouldRefresh ? 'websocket-reconnect-gap' : 'websocket-reconnect');
      }
      return;
    }

    if (event.type === 'workflow_step_question' && event.notification_id) {
      if (!event.task_id) {
        const nextQuestion = pendingDirectQuestionFromQuestionSet(
          event.notification_id,
          event.step_name,
          event.questions,
          event.context,
        );
        if (shouldResetPendingDirectQuestionForm(
          pendingDirectQuestion?.notificationId,
          nextQuestion.notificationId,
        )) {
          directQuestionAnswers = restoreDirectQuestionDraft(nextQuestion);
          directQuestionPageIndex = 0;
          directQuestionCollapsed = false;
          directQuestionSubmitting = false;
        }
        pendingDirectQuestion = nextQuestion;
        if (!chatV2OwnsCurrent) {
          awaitingAssistantStart = false;
          turnInProgress = false;
          setConversationTurnIndicator(currentConversation?.conversation_id, false);
        }
      }
    }

    if (event.type === 'auth_challenge' && event.notification_id) {
      if (!event.task_id) {
        const nextQuestion = pendingDirectQuestionFromAuthChallengeEvent(event);
        if (!nextQuestion) return;
        if (shouldResetPendingDirectQuestionForm(
          pendingDirectQuestion?.notificationId,
          nextQuestion.notificationId,
        )) {
          directQuestionAnswers = restoreDirectQuestionDraft(nextQuestion);
          directQuestionPageIndex = 0;
          directQuestionCollapsed = false;
          directQuestionSubmitting = false;
        }
        pendingDirectQuestion = nextQuestion;
        if (!chatV2OwnsCurrent) {
          awaitingAssistantStart = false;
          turnInProgress = false;
          setConversationTurnIndicator(currentConversation?.conversation_id, false);
        }
      }
    }

    if (event.type === 'credential_request' && event.notification_id) {
      void refreshPendingDirectQuestion();
    }

    if (event.type === 'workflow_step_question_resolved' || event.type === 'auth_challenge_resolved') {
      if (pendingDirectQuestion && event.notification_id === pendingDirectQuestion.notificationId) {
        pendingDirectQuestion = null;
        directQuestionAnswers = {};
        directQuestionPageIndex = 0;
        directQuestionCollapsed = false;
      }
      directQuestionSubmitting = false;
    }

    if (
      event.type === 'system_message' &&
      currentConversation &&
      currentConversation.conversation_id === event.conversation_id &&
      (event.chat_mode === 'plan' || event.chat_mode === 'build' || event.chat_mode === 'default')
    ) {
      const platformData = { ...(currentConversation.context?.platform_data ?? {}) };
      if (event.chat_mode === 'default') {
        delete platformData.chat_mode;
      } else {
        platformData.chat_mode = event.chat_mode;
      }
      const context = { ...currentConversation.context, platform_data: platformData };
      currentConversation = { ...currentConversation, context };
      patchConversationInList(currentConversation.conversation_id, { context });
    }

    if (event.type === 'credential_request_resolved') {
      if (pendingCredentialRequest && event.notification_id === pendingCredentialRequest.notification_id) {
        pendingCredentialRequest = null;
      }
      void refreshPendingDirectQuestion();
    }

    // Chat v2 owns the canonical timeline for every conversation. The legacy
    // per-event applyEvent path has been removed; canonical content arrives via
    // chat_v2_frame (handled by applyChatV2Frame) and snapshots.
    scheduleCurrentConversationViewSave();
    // No longer polling for escalations — they arrive via push events
  }

  async function handleAgentFilterChange(): Promise<void> {
    persistSelectedAgent();
    resetConversationSearchResults();
    await refreshAvailableChannelTypes();
    await Promise.all([loadAgentDirectChats(), loadConversationPage(true)]);
  }

  function handleViewSession(sessionId: string): void {
    if (!currentConversation) return;
    subSessionId = sessionId;
    subSessionPanelOpen = true;
    subSessionInfo = null;
    subSessionInfoOpen = false;
  }

  function closeSubSessionPanel(): void {
    subSessionClosing = true;
    setTimeout(() => {
      subSessionPanelOpen = false;
      subSessionClosing = false;
      subSessionId = '';
      subSessionInfo = null;
      subSessionInfoOpen = false;
    }, 250);
  }

  $effect(() => {
    const conversationId = routeConversationId;
    untrack(() => {
      if (conversationId) {
        void openConversation(conversationId);
      } else {
        initializing = false;
        resetConversationSwitch();
        initialConversationResolved = true;
      }
    });
  });

  $effect(() => {
    currentConversation?.conversation_id;
    conversationModeOpen;
    syncServiceWorkerActiveConversation();
  });

  $effect(() => {
    if ((!timelineEl && !timelineContentEl && !footerChromeEl) || typeof ResizeObserver === 'undefined') {
      return;
    }
    // Track scroll dimensions so no-op refreshes do not produce spurious
    // bottom re-pins. Include clientHeight: composer/footer growth can shrink
    // the timeline viewport without changing scrollHeight, and a pinned tail
    // should stay pinned through that layout change.
    let lastDimensions = {
      scrollHeight: timelineEl?.scrollHeight ?? 0,
      clientHeight: timelineEl?.clientHeight ?? 0,
    };
    const observer = new ResizeObserver(() => {
      const currentDimensions = {
        scrollHeight: timelineEl?.scrollHeight ?? 0,
        clientHeight: timelineEl?.clientHeight ?? 0,
      };
      if (!chatScrollDimensionsChanged(lastDimensions, currentDimensions)) {
        return;
      }
      lastDimensions = currentDimensions;
      // Re-pin iff the user is pinned to the tail. tailPinned is always kept
      // in sync with !userScrolledUp (single source of truth), so this is
      // equivalent to: if (!userScrolledUp || bottomScrollPending) re-pin.
      if (shouldPreserveLiveTailOnResize({ tailPinned, autoScrollPending: bottomScrollPending })) {
        scheduleScrollToBottom(false, 2);
      }
      scheduleTimelineViewportFill();
    });
    if (timelineEl) {
      observer.observe(timelineEl);
    }
    if (timelineContentEl) {
      observer.observe(timelineContentEl);
    }
    if (footerChromeEl) {
      observer.observe(footerChromeEl);
    }
    return () => observer.disconnect();
  });

  $effect(() => {
    currentConversation?.conversation_id;
    conversationSubloadsLoading;
    displayedTimeline.length;
    visibleStartIndex;
    visibleEndIndex;
    hasOlderMessages;
    loadingOlderMessages;

    if (!currentConversation || conversationSubloadsLoading) return;
    scheduleTimelineViewportFill();
  });

  $effect(() => {
    const latestTodos = selectLatestTodoState(chatV2Store.visibleItems, currentConversation?.context?.type === 'web');
    if (latestTodos.length > 0) {
      retainedChatTodos = latestTodos;
      return;
    }
    if (!turnInProgress && pendingDirectQuestion === null && !directQuestionSubmitting) {
      retainedChatTodos = [];
    }
  });

  $effect(() => {
    if (mobileListOpen) {
      const handle = registerOverlay({ kind: 'sheet', blocksChrome: false });
      mobileListOverlayCleanup = handle.unregister;
      return () => {
        handle.unregister();
        mobileListOverlayCleanup = null;
      };
    }

    mobileListOverlayCleanup?.();
    mobileListOverlayCleanup = null;
  });

  $effect(() => {
    if (!mobileListOpen) {
      return;
    }

    const handleKeydown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeMobileList();
      }
    };

    window.addEventListener('keydown', handleKeydown);
    return () => {
      window.removeEventListener('keydown', handleKeydown);
    };
  });

  $effect(() => {
    const handleKeydown = (event: KeyboardEvent): void => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') {
        const target = event.target as HTMLElement | null;
        if (
          target instanceof HTMLInputElement ||
          target instanceof HTMLTextAreaElement ||
          target instanceof HTMLSelectElement ||
          target?.isContentEditable
        ) {
          return;
        }
        event.preventDefault();
        openChatSearch();
      }
      if (event.key === 'Escape' && chatSearchOpen) {
        event.preventDefault();
        chatSearchOpen = false;
        chatSearchResults = [];
        seededChatSearchSession = null;
        seededChatSearchRef = null;
      }
    };
    window.addEventListener('keydown', handleKeydown);
    return () => window.removeEventListener('keydown', handleKeydown);
  });

  $effect(() => {
    if (conversationSearchSubmitted && conversationSearch.trim() !== conversationSearchSubmitted) {
      resetConversationSearchResults();
    }
  });

  $effect(() => {
    syncSeededSearchFromUrl();
  });

  $effect(() => {
    if (!chatSearchOpen || seededChatSearchRan || !chatSearchQuery.trim() || !currentConversation) {
      return;
    }
    if (conversationSubloadsLoading) return;
    seededChatSearchRan = true;
    void runChatSearch();
  });

  $effect(() => {
    const conversationId = currentConversation?.conversation_id ?? '';
    if (!chatSearchOpen || !chatSearchQuery.trim() || !conversationId) return;
    if (conversationSubloadsLoading) return;
    if (lastChatSearchConversationId && conversationId !== lastChatSearchConversationId) {
      chatSearchResults = [];
      chatSearchSelectedIndex = 0;
      void runChatSearch();
    }
  });

  let visibleConversationList = $derived.by(() => {
    let list = conversations.filter((conversation) => !isAgentDirectConversation(conversation));
    const query = conversationSearch.trim().toLowerCase();
    if (query && (!searchEnabled || !conversationSearchSubmitted)) {
      list = list.filter((c) => conversationTitle(c).toLowerCase().includes(query));
    }
    return list;
  });
  let visibleConversationSections = $derived.by(() => groupConversationsByActivity(visibleConversationList, historySectionNow));

  let visibleAgentDirectChats = $derived.by(() => {
    return sortAgentDirectChats(agentDirectChats);
  });
  // Canonical Chat v2 visible items are the single source of truth for the
  // rendered timeline. Native selectors operate on the same canonical union;
  // there is no legacy render projection or structural cast.
  //
  // Window over explicitly renderable canonical items only. The policy is
  // exhaustive, so newly added kinds cannot silently create blank rows.
   let renderableVisibleItems = $derived.by<ChatV2TimelineItem[]>(() =>
     chatV2Store.visibleItems.filter(isRenderableTimelineItem)
   );
  let displayedTimeline = $derived.by<ChatV2TimelineItem[]>(() => {
    const end = timelineWindowEndIndex(renderableVisibleItems.length);
    const start = Math.min(Math.max(0, visibleStartIndex), end);
    return renderableVisibleItems.slice(start, end);
  });
  let displayedTimelineHasStreaming = $derived(displayedTimeline.some((item) => isInlineLiveChatV2Item(item)));
  const retryableFailedTurnId = $derived.by(() =>
    currentConversation
      && !turnInProgress
      && !awaitingAssistantStart
      && !pendingDirectQuestion
      && !directQuestionSubmitting
      && !isReadOnly(currentConversation)
      ? (() => {
          const turnId = retryableFailedTurnIdFromTailChatV2(displayedTimeline);
          return turnId === suppressedRecoverableTurnId ? null : turnId;
        })()
      : null
  );
  const canRetryFailedTurn = $derived.by(() =>
    retryableFailedTurnId !== null
  );
  let chatSearchMatchedMessageIds = $derived.by(() => new Set(chatSearchResults.map((result) => result.targetId)));
  let selectedChatSearchTargetId = $derived(chatSearchResults[chatSearchSelectedIndex]?.targetId ?? null);

  // Sync composer draft with the active conversation. On conversation
  // switch, save the current draft under the previous key and hydrate
  // the new one. Runs before the composer-watching effect thanks to
  // declaration order.
  $effect(() => {
    const id = currentConversation?.conversation_id;
    const nextKey = id ? DRAFT_PREFIX + id : null;
    if (nextKey === currentDraftKey) return;
    if (currentDraftKey) writeDraft(currentDraftKey, composer);
    const loaded = nextKey ? readDraft(nextKey) : '';
    if (composer !== loaded) composer = loaded;
    currentDraftKey = nextKey;
  });

  // Debounce-persist the composer draft on every change. Clears the
  // entry when composer becomes empty so we do not leave stale drafts.
  $effect(() => {
    const key = currentDraftKey;
    const value = composer;
    if (!key) return;
    if (typeof window === 'undefined') return;
    if (draftSaveTimer !== null) window.clearTimeout(draftSaveTimer);
    draftSaveTimer = window.setTimeout(() => {
      draftSaveTimer = null;
      writeDraft(key, value);
    }, 300);
  });

  onMount(() => {
    void loadUserPreferences(auth.getSnapshot().user?.email);
    restoreEnterToSendPreference();
    restoreSelectedChannel();
    restoreChatSidebarState();
    mobileListOpen = !conversationIdFromRoute();
    unsubscribeWs = wsClient.subscribe(handleSocketEvent);
    unsubscribeComposerFocus = onChatComposerFocusRequest(() => {
      composerElement?.focus();
    });
    unsubscribeCancelTurn = onCancelActiveTurnRequest(() => {
      cancelActiveTurnWithChatV2();
    });
    visibilityHandler = () => {
      syncServiceWorkerActiveConversation();
      if (!document.hidden) {
        resumeControllerRecoveryIfPending();
        void resyncSidebarData('visibility');
        void refreshConversationView('visibility');
        if (currentConversation) void drainChatV2Outbox(currentConversation.conversation_id);
        void refreshPendingNotifications();
      } else {
      }
    };
    focusHandler = () => {
      syncServiceWorkerActiveConversation();
      void resyncSidebarData('focus');
      void refreshConversationView('focus');
      if (currentConversation) void drainChatV2Outbox(currentConversation.conversation_id);
    };
    pageShowHandler = () => {
      void resyncSidebarData('pageshow');
      void refreshConversationView('pageshow');
      if (currentConversation) void drainChatV2Outbox(currentConversation.conversation_id);
    };
    onlineHandler = () => {
      void resyncSidebarData('online');
      void refreshConversationView('online');
      if (currentConversation) void drainChatV2Outbox(currentConversation.conversation_id);
    };
    document.addEventListener('visibilitychange', visibilityHandler);
    window.addEventListener('focus', focusHandler);
    window.addEventListener('pageshow', pageShowHandler);
    window.addEventListener('online', onlineHandler);
    startNotificationRefreshPolling();
    staleRuntimeGuardTimer = window.setInterval(() => {
      if (!currentConversation || document.hidden) return;
      const hasActiveWork = turnInProgress || hasAnyActiveTurnTimelineItem();
      if (!hasActiveWork) {
        // The staleness inputs cleared (turn settled, items terminal): the
        // refresh loop did its job. Re-arm the attempt budget.
        staleRuntimeRefreshAttempts = 0;
        return;
      }
      const lastRuntimeAt = Math.max(lastRuntimeSnapshotAt, lastConversationViewRefreshAt);
      if (shouldRefreshForStaleRuntime({
        turnInProgress,
        hasActiveTimelineItem: hasActiveWork,
        lastRuntimeAt,
        now: Date.now(),
        staleMs: STALE_RUNTIME_REFRESH_MS,
      })) {
        // Normal cadence for the first attempts, then a slow backoff: a
        // persistently "active" item after several refreshes is usually a
        // leaked non-terminal timeline item that refreshing cannot fix, and a
        // tight refresh loop keeps disrupting the user's scroll position. The
        // backoff never stops entirely — with a silently broken WebSocket the
        // periodic refresh is the only way a really-completed turn ever
        // settles in the UI.
        if (!shouldAttemptStaleRuntimeRefresh({
          attempts: staleRuntimeRefreshAttempts,
          lastAttemptAt: lastStaleRuntimeRefreshAttemptAt,
          now: Date.now(),
          intervalMs: STALE_RUNTIME_REFRESH_MS,
        })) {
          return;
        }
        staleRuntimeRefreshAttempts += 1;
        lastStaleRuntimeRefreshAttemptAt = Date.now();
        void refreshConversationView('stale-runtime-ttl');
      }
    }, 5000);
    scheduleHistorySectionDayRefresh();
    void reconcileChatNotifications();
    void refreshSearchHealth();

    void initialize();
    // Same-tab tap on the Chat tab:
    //   * Mobile: open the conversation list drawer so the user can
    //     switch conversations or start a new one without retyping.
    //   * Desktop: just scroll the timeline to the newest message.
    const unsubTabReset = onTabReset('/chat', () => {
      if (isMobileViewport()) {
        mobileListOpen = true;
      } else {
        scrollToBottom(true);
      }
    });

    return () => {
      saveCurrentConversationView();
      mobileListOverlayCleanup?.();
      mobileListOverlayCleanup = null;
      stopInitialLoadTimeout();
      unsubscribeWs?.();
      unsubscribeComposerFocus?.();
      unsubscribeCancelTurn?.();
      unsubTabReset();
      if (draftSaveTimer !== null) {
        window.clearTimeout(draftSaveTimer);
        draftSaveTimer = null;
      }
      if (conversationViewSaveTimer !== null) {
        window.clearTimeout(conversationViewSaveTimer);
        conversationViewSaveTimer = null;
      }
      // Flush a final draft write on unmount so the sessionStorage
      // value reflects the last in-memory state.
      if (currentDraftKey) writeDraft(currentDraftKey, composer);
      stopEscalationCountdown();
      stopNotificationRefreshPolling();
      stopHistorySectionDayTimer();
      if (visibilityHandler) {
        document.removeEventListener('visibilitychange', visibilityHandler);
      }
      if (focusHandler) {
        window.removeEventListener('focus', focusHandler);
      }
      if (pageShowHandler) {
        window.removeEventListener('pageshow', pageShowHandler);
      }
      if (onlineHandler) {
        window.removeEventListener('online', onlineHandler);
      }
      if (foregroundSyncTimer !== null) {
        window.clearTimeout(foregroundSyncTimer);
        foregroundSyncTimer = null;
      }
      clearControllerRecovery();
      clearConversationSwitchTimeout();
      if (staleRuntimeGuardTimer !== null) {
        window.clearInterval(staleRuntimeGuardTimer);
        staleRuntimeGuardTimer = null;
      }
      for (const timer of chatV2CanonicalRecoveryTimers.values()) {
        window.clearTimeout(timer);
      }
      chatV2CanonicalRecoveryTimers.clear();
      chatV2CanonicalRecoveryRerun.clear();
      chatV2Lifecycle.release();
      if (activeConversationId) {
        wsClient.unsubscribeConversation(activeConversationId);
      }
      syncServiceWorkerActiveConversation(false);
    };
  });
</script>

<svelte:head>
  <title>{currentConversation ? `${conversationTitle(currentConversation)} · Chat · Cognis` : 'Chat · Cognis'}</title>
</svelte:head>

{#if initializing && !conversationIdFromRoute()}
  <LoadingState label="Loading conversation" description="Fetching history, restoring workflow prompts, and preparing the live stream." />
{:else}
  <div
    class={`relative flex h-full min-h-0 flex-col overflow-hidden ${isWindowMode ? '' : `gap-3 ${chatSidebarCollapsed ? '' : 'lg:grid lg:grid-cols-[320px_minmax(0,1fr)] lg:gap-4'}`}`}
    use:edgeSwipe={{ edge: 'left', onTrigger: handleChatLeftEdgeSwipe }}
    use:edgeSwipe={{ edge: 'right', onTrigger: handleChatRightEdgeSwipe }}
  >
    {#if mobileListOpen && !isWindowMode}
      <button
        aria-label="Close conversation list"
        class="fixed inset-0 z-30 bg-slate-950/80 backdrop-blur-sm lg:hidden"
        onclick={closeMobileList}
        type="button"
        transition:fade={{ duration: 180 }}
      ></button>
    {/if}

    <!--
      Sidebar: overlay card on mobile (needs its own chrome so it floats
      over the chat), flat column with a subtle right divider on lg+.
      Dropping the rounded card background on lg lets the conversation
      list feel like part of the page instead of a box inside a box.
    -->
    <!--
      Conversation list on mobile floats over the chat area as a card.
      Its top is pushed below the Dynamic Island by a generous 16px
      buffer on top of `env(safe-area-inset-top)` so notification
      banners and the status bar always have room above the card.
    -->
    <!--
      On mobile, the conversation list is a floating card. When closed it is
      translated off-screen. We also mark it `inert` so assistive technologies
      skip it entirely while it is hidden — this prevents the aside from
      appearing as an active landmark in the accessibility tree when the
      conversation is in focus.
    -->
    {#if !isWindowMode}
    <aside
      aria-label="Conversation list"
      aria-modal={mobileListOpen ? 'true' : undefined}
      class={`fixed left-3 right-3 top-[calc(1rem+env(safe-area-inset-top))] bottom-[calc(0.75rem+env(safe-area-inset-bottom))] z-40 flex max-w-[22rem] min-h-0 flex-col rounded-[1.75rem] border border-slate-800/80 bg-slate-900/95 shadow-card backdrop-blur transition-transform duration-200 ease-out lg:static lg:right-auto lg:max-w-none lg:w-[18rem] lg:translate-x-0 lg:rounded-none lg:border-0 lg:border-r lg:border-slate-800/60 lg:bg-transparent lg:shadow-none lg:backdrop-blur-0 ${chatSidebarCollapsed ? 'lg:hidden' : 'lg:flex'} ${mobileListOpen || !currentConversation ? 'translate-x-0' : '-translate-x-[120%] pointer-events-none lg:pointer-events-auto'}`}
      inert={(!mobileListOpen && Boolean(currentConversation) && isMobileViewport()) || undefined}
      role={mobileListOpen ? 'dialog' : undefined}
    >
      <!-- Static top: filters -->
      <div class="shrink-0 space-y-3 p-4 pb-2 sm:p-4">
        <div class="flex items-center justify-between">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Conversations</p>
          <div class="flex items-center gap-2">
            <Button aria-expanded={conversationFiltersOpen} aria-label="Toggle conversation filters" size="sm" variant="secondary" onclick={() => (conversationFiltersOpen = !conversationFiltersOpen)}>
              <span class="hidden sm:inline">Filters</span>
              {#if conversationFiltersOpen}
                <ChevronUp class="h-4 w-4" />
              {:else}
                <ChevronDown class="h-4 w-4" />
              {/if}
            </Button>
            <Button aria-label="Close conversation list" class="lg:hidden" size="sm" variant="secondary" onclick={closeMobileList}>Close</Button>
          </div>
        </div>

        {#if !sidebarProjectionLoaded && conversationListLoading}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Loading setup</p>
            <p class="text-sm leading-6 text-slate-400">Loading agents and conversations…</p>
          </div>
        {:else if sidebarProjectionFailed && agents.length === 0}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Setup unavailable</p>
            <p class="text-sm leading-6 text-slate-400">Could not load agents and setup state.</p>
            <Button class="w-full justify-center" size="sm" variant="secondary" onclick={() => void loadSidebarProjection()}>Retry</Button>
          </div>
        {:else if sidebarProjectionLoaded && agents.length === 0}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Setup incomplete</p>
            <p class="text-sm leading-6 text-slate-400">Create an agent to start chatting.</p>
            <Button class="w-full justify-center" size="sm" onclick={() => goto('/agents/new')}>Create agent</Button>
          </div>
        {:else if isLlmUnavailableForSetup()}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Setup incomplete</p>
            <p class="text-sm leading-6 text-slate-400">Configure an LLM provider first.</p>
            <Button class="w-full justify-center" size="sm" onclick={() => goto('/settings?tab=providers')}>Open provider settings</Button>
          </div>
        {:else}
          <div class={`${conversationFiltersOpen ? 'grid' : 'hidden'} grid-cols-1 gap-2 sm:grid-cols-2`}>
            <div class="relative">
              <label class="mb-1 block text-xs font-medium uppercase tracking-widest text-slate-500" for="conversation-agent-filter">Agent</label>
              <button
                id="conversation-agent-filter"
                type="button"
                class="flex w-full items-center justify-between gap-2 rounded-lg border border-slate-700 bg-slate-950/80 px-3 py-2 text-left text-xs font-medium text-slate-200 transition hover:border-slate-500 hover:text-white"
                onclick={() => {
                  agentFilterDropdownOpen = !agentFilterDropdownOpen;
                  channelFilterDropdownOpen = false;
                }}
                aria-haspopup="listbox"
                aria-expanded={agentFilterDropdownOpen}
              >
                <span class="truncate">{selectedAgentFilterLabel()}</span>
                <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
              </button>
              {#if agentFilterDropdownOpen}
                <div class="absolute left-0 right-0 z-40 mt-1 max-h-64 overflow-y-auto rounded-xl border border-slate-700 bg-slate-950 p-1 shadow-xl shadow-black/40" role="listbox" aria-multiselectable="true">
                  <button
                    type="button"
                    class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selectedAgentIds.length === 0 ? 'bg-sky-500/20 text-sky-100' : 'text-slate-300 hover:bg-slate-900 hover:text-white'}`}
                    onclick={() => { selectedAgentIds = []; void handleAgentFilterChange(); }}
                    aria-selected={selectedAgentIds.length === 0}
                    role="option"
                  >
                    <span class="grid h-4 w-4 shrink-0 place-items-center">{#if selectedAgentIds.length === 0}<Check class="h-3 w-3" />{/if}</span>
                    <span>All agents</span>
                  </button>
                  {#each activePrimaryAgents() as agent}
                    {@const selected = selectedAgentIds.includes(agent.agent_id)}
                    <button
                      type="button"
                      class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selected ? 'bg-sky-500/20 text-sky-100' : 'text-slate-300 hover:bg-slate-900 hover:text-white'}`}
                      onclick={() => void toggleSelectedAgent(agent.agent_id)}
                      aria-selected={selected}
                      role="option"
                      title={agent.display_name ?? agent.name ?? agent.agent_id}
                    >
                      <span class="grid h-4 w-4 shrink-0 place-items-center">{#if selected}<Check class="h-3 w-3" />{/if}</span>
                      <AgentAvatar name={agent.display_name ?? agent.name ?? agent.agent_id} avatarUrl={agent.avatar_url ?? null} class="h-4 w-4" />
                      <span class="truncate">{agent.display_name ?? agent.name ?? agent.agent_id}</span>
                    </button>
                  {/each}
                </div>
              {/if}
            </div>

            <div class="relative">
              <label class="mb-1 block text-xs font-medium uppercase tracking-widest text-slate-500" for="conversation-channel-filter">Channel</label>
              <button
                id="conversation-channel-filter"
                type="button"
                class="flex w-full items-center justify-between gap-2 rounded-lg border border-slate-700 bg-slate-950/80 px-3 py-2 text-left text-xs font-medium text-slate-200 transition hover:border-slate-500 hover:text-white"
                onclick={() => {
                  channelFilterDropdownOpen = !channelFilterDropdownOpen;
                  agentFilterDropdownOpen = false;
                }}
                aria-haspopup="listbox"
                aria-expanded={channelFilterDropdownOpen}
              >
                <span class="truncate">{selectedChannelFilterLabel()}</span>
                <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
              </button>
              {#if channelFilterDropdownOpen}
                <div class="absolute left-0 right-0 z-40 mt-1 max-h-64 overflow-y-auto rounded-xl border border-slate-700 bg-slate-950 p-1 shadow-xl shadow-black/40" role="listbox" aria-multiselectable="true">
                  <button
                    type="button"
                    class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selectedChannels.length === 0 ? 'bg-sky-500/20 text-sky-100' : 'text-slate-300 hover:bg-slate-900 hover:text-white'}`}
                    onclick={() => { selectedChannels = []; void persistSelectedChannels(); }}
                    aria-selected={selectedChannels.length === 0}
                    role="option"
                  >
                    <span class="grid h-4 w-4 shrink-0 place-items-center">{#if selectedChannels.length === 0}<Check class="h-3 w-3" />{/if}</span>
                    <span>All channels</span>
                  </button>
                  {#each channelTypes() as ch}
                    {@const selected = selectedChannels.includes(ch)}
                    <button
                      type="button"
                      class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selected ? 'bg-sky-500/20 text-sky-100' : 'text-slate-300 hover:bg-slate-900 hover:text-white'}`}
                      onclick={() => void toggleSelectedChannel(ch)}
                      aria-selected={selected}
                      role="option"
                    >
                      <span class="grid h-4 w-4 shrink-0 place-items-center">{#if selected}<Check class="h-3 w-3" />{/if}</span>
                      <span class="truncate">{channelDisplayLabel(ch)}</span>
                    </button>
                  {/each}
                </div>
              {/if}
            </div>
          </div>
        {/if}

        {#if visibleAgentDirectChats.length > 0}
          <div class="border-t border-slate-800/60 pt-3">
            <div class="mb-2 flex items-center justify-between px-1">
              <p class="text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500">Direct chats</p>
            </div>
            <div class="flex gap-2 overflow-x-auto pb-1">
              {#each visibleAgentDirectChats as item}
                {@const conversation = item.conversation}
                {@const isActive = conversation.conversation_id === currentConversation?.conversation_id}
                {@const inProgress = conversation.has_active_turn || (isActive && turnInProgress)}
                {@const showAttentionDot = conversationShowsAttentionDot(conversation, isActive, inProgress)}
                {@const attentionDescription = conversationAttentionDescription(conversation)}
                {@const turnMode = conversationChatMode(conversation)}
                <a
                  class={`group flex min-w-[4.5rem] flex-col items-center gap-1 rounded-2xl px-2 py-2 transition ${isActive ? 'bg-sky-500/15 text-white' : 'text-slate-300 hover:bg-slate-900/70'}`}
                  href={conversationUrl(conversation.conversation_id)}
                  onclick={closeMobileList}
                  aria-current={isActive ? 'page' : undefined}
                  aria-label={`Open direct chat with ${agentLabel(item.agent)}${isActive ? ', current conversation' : ''}${showAttentionDot ? `, ${attentionDescription}` : ''}`}
                  title={`Open direct chat with ${agentLabel(item.agent)}`}
                >
                  <span class="relative grid h-12 w-12 shrink-0 place-items-center">
                    {#if inProgress}
                      <span class={`conversation-turn-orbit ${conversationOrbitClass(conversation, turnMode)}`} aria-hidden="true"><span></span></span>
                    {/if}
                    <AgentAvatar name={agentLabel(item.agent)} avatarUrl={item.agent.avatar_url ?? null} class="h-10 w-10" />
                    {#if showAttentionDot}
                      <span class={`absolute right-0 top-0 h-3 w-3 rounded-full border-2 border-slate-950 ${conversationDotClass(conversation)}`} title={attentionDescription}></span>
                    {/if}
                  </span>
                  <span class="max-w-[4rem] truncate text-center text-[11px] font-medium">{agentLabel(item.agent)}</span>
                </a>
              {/each}
            </div>
          </div>
        {/if}

        <div class="flex items-center justify-between gap-3 border-t border-slate-800/60 pt-3">
          <div class="flex min-w-0 items-center gap-2">
            <h2 class="text-sm font-semibold text-white">History</h2>
            {#if conversationListLoading || conversationListRefreshing || conversationSearchLoading}
              <span class="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500" aria-live="polite">
                <RefreshCw class="h-3 w-3 animate-spin" />
                {conversationSearchLoading ? 'Searching' : conversationListRefreshing ? 'Refreshing' : 'Loading'}
              </span>
            {/if}
          </div>
          <div class="flex items-center gap-2">
            <button
              class="rounded-lg p-1 text-slate-500 transition hover:bg-slate-800 hover:text-sky-300 disabled:cursor-not-allowed disabled:text-slate-700"
              disabled={conversationListRefreshing || conversationListLoading}
              onclick={() => void forceRefreshConversationHistory()}
              type="button"
              aria-label="Refresh conversation history"
              title="Refresh conversation history"
            >
              <RefreshCw class={`h-4 w-4 ${conversationListRefreshing ? 'animate-spin' : ''}`} />
            </button>
            <button
              class="text-xs font-medium text-sky-400 transition hover:text-sky-300 disabled:cursor-not-allowed disabled:text-slate-600"
              disabled={newChatCreating}
              onclick={openNewConversationModal}
              type="button"
            >+ New</button>
          </div>
        </div>

        <form class="relative" onsubmit={(event) => { event.preventDefault(); void submitConversationSearch(); }}>
          <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
          <Input
            bind:value={conversationSearch}
            class="pl-9 pr-20"
            placeholder={searchEnabled ? 'Search conversations' : 'Filter by title'}
          />
          {#if conversationSearch}
            <button
              aria-label="Clear search"
              class="absolute right-10 top-1.5 rounded-lg p-1 text-slate-500 transition hover:bg-slate-800 hover:text-slate-200"
              type="button"
              onclick={clearConversationSearch}
            >
              <X class="h-4 w-4" />
            </button>
          {/if}
          <button
            aria-label="Search conversations"
            class="absolute right-2 top-1.5 rounded-lg p-1 text-sky-400 transition hover:bg-slate-800 hover:text-sky-300 disabled:text-slate-600"
            type="submit"
            disabled={!searchEnabled || !conversationSearch.trim() || conversationSearchLoading}
          >
            <Search class={`h-4 w-4 ${conversationSearchLoading ? 'animate-pulse' : ''}`} />
          </button>
        </form>
        {#if !searchEnabled}
          <p class="text-xs text-slate-500">Content search disabled; filtering loaded titles only.</p>
        {/if}

        <div class="grid grid-cols-3 gap-1.5 sm:gap-2">
          <Button
            size="sm"
            variant={selectedConversationStatus === 'active' ? 'primary' : 'secondary'}
            onclick={() => void setConversationStatusFilter('active')}
          >Active</Button>
          <Button
            size="sm"
            variant={selectedConversationStatus === 'starred' ? 'primary' : 'secondary'}
            onclick={() => void setConversationStatusFilter('starred')}
          >Starred</Button>
          <Button
            size="sm"
            variant={selectedConversationStatus === 'archived' ? 'primary' : 'secondary'}
            onclick={() => void setConversationStatusFilter('archived')}
          >Archived</Button>
        </div>
      </div>

      <!-- Scrollable middle: conversation list -->
      <PullToRefresh
        class="min-h-0 flex-1 px-4 py-2"
        disabled={!isMobileViewport()}
        onRefresh={forceRefreshConversationHistory}
      >
        <div class="space-y-1 pb-3">
          {#if (conversationListLoading || conversationListRefreshing) && visibleConversationList.length === 0 && !(conversationSearchSubmitted && searchEnabled)}
            <div class="rounded-2xl border border-slate-800 bg-slate-900/40 px-4 py-6 text-center text-sm text-slate-400">
              <RefreshCw class="mx-auto mb-2 h-4 w-4 animate-spin text-slate-500" />
              Loading conversation history...
            </div>
          {/if}
          {#if conversationSearchSubmitted && conversationSearchLoading && searchEnabled}
            <p class="mb-3 rounded-2xl border border-slate-800 px-4 py-4 text-center text-sm text-slate-400">
              Searching conversation history...
            </p>
          {:else if conversationSearchSubmitted && conversationSearchError && searchEnabled}
            <p class="mb-3 rounded-2xl border border-rose-900/50 bg-rose-950/30 px-4 py-4 text-center text-sm text-rose-200">
              {conversationSearchError}
            </p>
          {:else if conversationSearchSubmitted && conversationSearchResults.length > 0}
            <div class="mb-3 space-y-1">
              <p class="px-2 text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500">Search results</p>
              {#each conversationSearchResults as result}
                {@const agent = searchResultAgent(result)}
                {@const canExpand = result.match_count > 1 && result.extra_matches.length > 0}
                {@const expanded = searchResultExpanded(result.intaris_session_id)}
                {@const resultTime = searchMatchTime(result.top_match)}
                <div class="rounded-xl transition hover:bg-slate-900/60">
                  <div class="group flex items-start gap-3 rounded-xl px-3 py-2.5 text-left text-slate-200 transition hover:bg-slate-900/60">
                    <button
                      class="flex min-w-0 flex-1 items-start gap-3 text-left"
                      type="button"
                      onclick={() => void openConversationSearchResult(result)}
                    >
                      <AgentAvatar name={agent?.display_name ?? agent?.name ?? result.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-8 w-8 shrink-0" />
                      <span class="min-w-0 flex-1">
                        <span class="block break-words text-sm font-medium text-white">{searchResultConversationTitle(result)}</span>
                        <span class="mt-0.5 flex items-center gap-2">
                          <span class="truncate text-xs text-slate-400">{agent?.display_name ?? agent?.name ?? result.agent_id}</span>
                          {#if result.match_count > 1}
                            <span class="shrink-0 text-[10px] font-medium text-slate-500">{result.match_count} matches</span>
                          {/if}
                          {#if resultTime}
                            <span class="shrink-0 text-[10px] text-slate-500">{resultTime}</span>
                          {/if}
                          <span class="shrink-0 text-[10px] text-slate-500">{searchMatchScore(result.top_match)}</span>
                        </span>
                        <span class="mt-1 line-clamp-2 text-xs leading-5 text-slate-400">{searchResultSnippet(result)}</span>
                      </span>
                    </button>
                    {#if canExpand}
                      <button
                        aria-label={expanded ? 'Collapse matches' : 'Expand matches'}
                        class="mt-1 shrink-0 rounded-lg p-1 text-slate-500 transition hover:bg-slate-800 hover:text-slate-200"
                        type="button"
                        onclick={() => toggleSearchResultExpanded(result.intaris_session_id)}
                      >
                        {#if expanded}
                          <ChevronUp class="h-4 w-4" />
                        {:else}
                          <ChevronDown class="h-4 w-4" />
                        {/if}
                      </button>
                    {/if}
                  </div>
                  {#if expanded}
                    <div class="ml-11 mr-3 mb-2 space-y-1 border-l border-slate-800 pl-2">
                      {#each result.extra_matches as match}
                        {@const matchTime = searchMatchTime(match)}
                        <button
                          class="block w-full rounded-lg px-2 py-1.5 text-left text-xs leading-5 text-slate-400 transition hover:bg-slate-800/70 hover:text-slate-200"
                          type="button"
                          onclick={() => void openConversationSearchResult(result, match)}
                        >
                          <span class="mb-0.5 block text-[10px] text-slate-500">
                            {#if matchTime}{matchTime} · {/if}{searchMatchScore(match)}
                          </span>
                          <span>{searchMatchSnippet(match)}</span>
                        </button>
                      {/each}
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {:else if conversationSearchSubmitted && !conversationSearchLoading && searchEnabled}
            <p class="mb-3 rounded-2xl border border-dashed border-slate-700 px-4 py-4 text-center text-sm text-slate-400">
              No content matches found.
            </p>
          {/if}

          {#if conversationSearchSubmitted && searchEnabled}
            <!-- Content search replaces the normal list; empty state is handled above. -->
          {:else}
            {#if visibleConversationList.length === 0 && !(conversationListLoading || conversationListRefreshing)}
            <p class="rounded-2xl border border-dashed border-slate-700 px-4 py-6 text-center text-sm text-slate-400">
              No conversations found.
            </p>
            {:else}
            {#each visibleConversationSections as section (section.key)}
              <section class="space-y-1" aria-labelledby={`history-section-${section.key}`}>
                <p id={`history-section-${section.key}`} class="sticky top-0 z-10 rounded-lg bg-slate-950 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500 first:mt-0">{section.label}</p>
                {#each section.conversations as conversation (conversation.conversation_id)}
                  {@const agent = conversationAgentForDisplay(conversation)}
                  {@const isActive = conversation.conversation_id === currentConversation?.conversation_id}
                  {@const unread = conversation.has_unread && !isActive}
                  {@const inProgress = conversation.has_active_turn || (isActive && turnInProgress)}
                  {@const showAttentionDot = conversationShowsAttentionDot(conversation, isActive, inProgress)}
                  {@const attentionDescription = conversationAttentionDescription(conversation)}
                  {@const turnMode = conversationChatMode(conversation)}
                  {@const rowTodoProgressTodos = conversationTodoProgressTodos(conversation)}
                  <a
                    class={`group flex items-start gap-3 rounded-xl px-3 py-2.5 transition ${isActive ? 'bg-sky-500/15 text-white' : 'text-slate-200 hover:bg-slate-900/60'}`}
                    href={conversationUrl(conversation.conversation_id)}
                    onclick={closeMobileList}
                    title={conversationTitle(conversation)}
                  >
                    <div class="relative grid h-9 w-9 shrink-0 place-items-center">
                      {#if inProgress}
                        <span class={`conversation-turn-orbit ${conversationOrbitClass(conversation, turnMode)}`} aria-hidden="true"><span></span></span>
                      {/if}
                      <AgentAvatar name={agent?.display_name ?? agent?.name ?? conversation.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-8 w-8" />
                      {#if showAttentionDot}
                        <span class={`absolute right-0 top-0 h-2.5 w-2.5 rounded-full border-2 border-slate-950 ${conversationDotClass(conversation)}`} title={attentionDescription}></span>
                      {/if}
                    </div>
                    <div class="min-w-0 flex-1">
                      <p class="break-words text-sm {unread ? 'font-semibold text-white' : 'font-medium text-white'}">{conversationTitle(conversation)}</p>
                      <div class="mt-0.5 flex items-center gap-2">
                        <span class="truncate text-xs text-slate-400">{agent?.display_name ?? agent?.name ?? conversation.agent_id}</span>
                        {#if (conversation.context?.type ?? 'web').toLowerCase() !== 'web'}
                          <span class="shrink-0 rounded-full border border-slate-700 bg-slate-800/60 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-widest text-slate-500">
                            {contextTypeBadge(conversation)}
                          </span>
                        {/if}
                      </div>
                    </div>
                    <div class="relative z-10 mt-1 flex shrink-0 flex-col items-center gap-1">
                      {#if canStarConversation(conversation)}
                        <button
                          aria-label={conversation.starred_at ? 'Unstar conversation' : 'Star conversation'}
                          class={`rounded-lg p-1 transition hover:bg-slate-800 ${conversation.starred_at ? 'text-amber-300 hover:text-amber-200' : 'text-slate-600 hover:text-slate-200'}`}
                          disabled={starringConversationId === conversation.conversation_id}
                          onclick={(event) => { event.preventDefault(); event.stopPropagation(); void toggleConversationStar(conversation); }}
                          title={conversation.starred_at ? 'Unstar conversation' : 'Star conversation'}
                          type="button"
                        >
                          <Star class={`h-4 w-4 ${conversation.starred_at ? 'fill-current' : ''}`} />
                        </button>
                      {/if}
                      {#if shouldShowConversationTodoProgress(conversation)}
                        <TodoProgressPopover
                          todos={rowTodoProgressTodos}
                          size="sm"
                          placement="bottom-right"
                          class="text-emerald-300"
                          label="Conversation todo progress"
                        />
                      {/if}
                    </div>
                  </a>
                {/each}
              </section>
            {/each}
            {/if}
          {/if}

          {#if conversationsHasMore}
            <div class="pt-2">
              <Button class="w-full justify-center" size="sm" variant="secondary" disabled={conversationListLoading} onclick={() => loadConversationPage(false)}>
                {conversationListLoading ? 'Loading...' : 'Load more conversations'}
              </Button>
            </div>
          {/if}
        </div>
      </PullToRefresh>

    </aside>
    {/if}

    <!--
      Main chat area: flat column directly on the page background. The
      previous lg: card with border/backdrop-blur/shadow created a
      redundant dark container inside the app frame. Content hierarchy
      is established by the header border-b, the message list, and the
      composer border-t — no extra wrapper needed.
    -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <section
      class="relative flex min-h-0 flex-1 flex-col bg-transparent"
      ondragenter={handleDragEnter}
      ondragleave={handleDragLeave}
      ondragover={handleDragOver}
      ondrop={(event) => void handleDrop(event)}
    >
      {#if switchingConversation}
        <div class="pointer-events-none absolute inset-0 z-20 bg-slate-950/35 backdrop-blur-[1px]"></div>
      {/if}
      {#if showDropZone}
        <div class="pointer-events-none absolute inset-0 z-50 flex items-center justify-center rounded-3xl border-2 border-dashed border-sky-400 bg-sky-500/10 backdrop-blur-sm">
          <div class="rounded-2xl bg-slate-900/90 px-6 py-4 text-center">
            <p class="text-lg font-medium text-sky-300">Drop files here to attach</p>
            <p class="mt-1 text-sm text-slate-400">Images, PDFs, audio, and other files</p>
          </div>
        </div>
      {/if}
      <!--
        Chat header. The iOS PWA status bar can overlay both compact and
        tablet/desktop-width layouts, so keep safe-area clearance on lg+ too.
      -->
      <div class="border-b border-slate-800/80 px-2.5 pt-[calc(0.5rem+env(safe-area-inset-top))] pb-2 sm:px-4 sm:pt-[calc(0.75rem+env(safe-area-inset-top))] sm:pb-3 lg:px-5 lg:pt-[calc(1rem+env(safe-area-inset-top))] lg:pb-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              <!--
                Desktop-only toggle for the conversation sidebar. Sits in
                the top-left corner of the chat header, next to the
                title, so both states (expand / collapse) appear in the
                same place instead of the expand button living in the
                header while the collapse button hid in the sidebar
                footer.
              -->
              {#if !isWindowMode}
              <button
                class="hidden rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800 hover:text-white lg:inline-flex"
                onclick={toggleChatSidebar}
                type="button"
                title={chatSidebarCollapsed ? 'Show conversations' : 'Hide conversations'}
                aria-label={chatSidebarCollapsed ? 'Show conversations' : 'Hide conversations'}
              >
                {#if chatSidebarCollapsed}
                  <ChevronsRight class="h-4 w-4" />
                {:else}
                  <ChevronsLeft class="h-4 w-4" />
                {/if}
              </button>
              {/if}
              <div class="flex items-center gap-1.5 lg:hidden">
                {#if !isWindowMode}
                  <Button aria-label="Open navigation" size="sm" variant="secondary" onclick={requestOpenMobileNav}>
                    <Menu class="h-4 w-4" />
                  </Button>
                  <Button aria-label="Open conversations" size="sm" variant="secondary" onclick={openMobileList}>
                    <ArrowLeft class="h-4 w-4" />
                  </Button>
                {/if}
              </div>
              <!-- Editable title -->
              {#if editingTitle}
                <!-- svelte-ignore a11y_autofocus -->
                <div class="flex min-w-0 flex-1 items-center gap-1.5">
                  <input
                    class="min-w-0 flex-1 rounded-lg border border-sky-500/50 bg-slate-950/80 px-2 py-1 text-lg font-semibold text-white focus:outline-none focus:ring-1 focus:ring-sky-300 sm:text-xl"
                    bind:value={editTitleValue}
                    onblur={handleTitleBlur}
                    onkeydown={handleTitleKeydown}
                    autofocus
                  />
                  <button
                    class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:bg-slate-800 hover:text-sky-200 disabled:cursor-not-allowed disabled:opacity-50"
                    onpointerdown={handleTitleRefreshPointerDown}
                    onclick={() => void refreshTitleSuggestion()}
                    type="button"
                    title="Load latest Intaris title"
                    aria-label="Load latest Intaris title"
                    disabled={titleSuggestionLoading}
                  >
                    <RefreshCw class={`h-4 w-4 ${titleSuggestionLoading ? 'animate-spin' : ''}`} />
                  </button>
                </div>
              {:else}
                <button
                  class="min-w-0 flex-1 truncate text-left text-lg font-semibold text-white transition hover:text-sky-300 sm:text-xl"
                  onclick={startEditTitle}
                  type="button"
                  title={currentConversation ? `${conversationTitle(currentConversation)} - click to edit title` : 'Click to edit title'}
                >
                  {currentConversation ? conversationTitle(currentConversation) : 'Conversation'}
                </button>
              {/if}
            </div>

            <!-- Sub-header info row -->
            <div class="mt-1.5 hidden items-center justify-between gap-3 text-sm text-slate-400 sm:flex">
              <div class="flex min-w-0 flex-wrap items-center gap-3">
              {#if currentConversation}
                {@const agent = conversationAgentForDisplay(currentConversation)}
                <div class="relative">
                  <button
                    type="button"
                    class="flex items-center gap-1.5 rounded-lg px-1 py-0.5 transition hover:bg-slate-800"
                    onclick={() => { showAgentProfile = !showAgentProfile; }}
                  >
                    <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url ?? null} class="h-5 w-5" />
                    <span>{agent.display_name ?? agent.name}</span>
                  </button>
                  {#if showAgentProfile}
                    <AgentProfilePopover {agent} onClose={() => { showAgentProfile = false; }} />
                  {/if}
                </div>

                {#if currentConversation.active_session_id}
                  <button
                    class="flex items-center gap-1 font-mono text-xs text-slate-500 transition hover:text-slate-300"
                    onclick={copySessionId}
                    type="button"
                    title="Copy full session ID"
                  >
                    {currentConversation.active_session_id.slice(0, 12)}
                    {#if sessionIdCopied}
                      <Check class="h-3 w-3 text-emerald-400" />
                    {:else}
                      <Copy class="h-3 w-3" />
                    {/if}
                  </button>
                {/if}

                {@const usage = activeContextUsage()}
                {#if usage?.model}
                  <span class="max-w-[18rem] truncate rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-300" title={usage?.provider_id ? `${usage.model} (${usage.provider_id})` : usage.model}>
                    {usage.model}
                  </span>
                  {#if usage.reasoning_effort}
                    <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400" title="Thinking effort">
                      thinking {usage.reasoning_effort}
                    </span>
                  {/if}
                  {#if contextUsageProfileLabel(usage)}
                    <span class="rounded-full border border-sky-400/25 bg-sky-400/10 px-2 py-0.5 text-[10px] font-medium text-sky-100" title="Agent profile">
                      profile {contextUsageProfileLabel(usage)}
                    </span>
                  {/if}
                {/if}

                {#if conversationTaskId(currentConversation)}
                  <a
                    href="/tasks/{conversationTaskId(currentConversation)}"
                    class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-slate-400 transition hover:border-sky-400/40 hover:text-sky-200"
                  >
                    {contextTypeBadge(currentConversation)}
                  </a>
                {:else}
                  <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-slate-400">
                    {contextTypeBadge(currentConversation)}
                  </span>
                {/if}

                {#if backingSessionBadge}
                  <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400" title="Backing sessions">
                    {backingSessionBadge}
                  </span>
                {/if}

              {:else}
                <span>No active conversation selected</span>
              {/if}
              </div>
              {#if activeContextUsage()}
                {@const usage = activeContextUsage()}
                <button
                  class={`relative ml-auto inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border bg-slate-900/70 transition hover:border-sky-400/40 hover:bg-slate-800/80 ${headerInfoOpen && headerInfoMode === 'context' ? 'border-sky-400/50' : 'border-slate-700'}`}
                  title={contextUsageTooltip(usage)}
                  aria-label={headerInfoOpen && headerInfoMode === 'context' ? 'Close context usage details' : 'Open context usage details'}
                  aria-expanded={headerInfoOpen && headerInfoMode === 'context'}
                  onclick={toggleContextInfo}
                  type="button"
                >
                  <svg class="absolute inset-1 -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
                    <circle cx="18" cy="18" r={CONTEXT_DONUT_RADIUS} fill="none" stroke="rgba(100,116,139,0.35)" stroke-width="3" />
                    <circle
                      cx="18"
                      cy="18"
                      r={CONTEXT_DONUT_RADIUS}
                      fill="none"
                      stroke={contextUsageStroke(usage)}
                      stroke-linecap="round"
                      stroke-width="3"
                      stroke-dasharray={CONTEXT_DONUT_CIRCUMFERENCE}
                      stroke-dashoffset={contextDonutDashOffset(usage ? contextWindowUsagePercentage(usage) : null)}
                    />
                  </svg>
                </button>
              {/if}
            </div>

          </div>

          <!--
            Header action row. One Info button toggles a full-width
            details panel that appears below the header (under the
            session id on desktop and under the title on mobile). The
            old chevron toggle and the separate desktop Info button
            were merged into this single icon — the same button on
            every viewport for a predictable affordance.
          -->
          <div class="flex items-start gap-2">
            {#if currentConversation && canStarConversation(currentConversation)}
              <div class="flex flex-col items-center gap-1">
                <button
                  class={`inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 transition hover:bg-slate-800 sm:h-8 sm:w-8 ${currentConversation.starred_at ? 'text-amber-300 hover:text-amber-200' : 'text-slate-400 hover:text-slate-100'}`}
                  onclick={() => { if (currentConversation) void toggleConversationStar(currentConversation); }}
                  type="button"
                  title={currentConversation.starred_at ? 'Unstar conversation' : 'Star conversation'}
                  aria-label={currentConversation.starred_at ? 'Unstar conversation' : 'Star conversation'}
                  disabled={starringConversationId === currentConversation.conversation_id}
                >
                  <Star class={`h-4 w-4 ${currentConversation.starred_at ? 'fill-current' : ''}`} />
                </button>
              </div>
            {/if}
            {#if !isWindowMode && canOpenAuxiliaryWindow}
              <button
                class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100 sm:h-8 sm:w-8"
                onclick={openCurrentConversationInSeparateWindow}
                type="button"
                title="Open in separate window"
                aria-label="Open conversation in separate window"
                disabled={!currentConversation}
              >
                <ExternalLink class="h-4 w-4" />
              </button>
            {/if}
            <button
              class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100 sm:h-8 sm:w-8"
              onclick={() => openChatSearch()}
              type="button"
              title="Search conversation"
              aria-label="Search conversation"
              disabled={!currentConversation}
            >
              <Search class="h-4 w-4" />
            </button>
            <SessionDetailsButton open={headerInfoOpen} loading={sessionInfoLoading} onclick={toggleHeaderInfo} />
            {#if !isAgentDirectConversation(currentConversation)}
              <div class="hidden flex-wrap gap-2 sm:flex">
                {#if currentConversation?.status === 'archived'}
                  <Button size="sm" variant="secondary" disabled={archivingConversation} onclick={restoreConversation}>
                    {archivingConversation ? 'Restoring...' : 'Restore'}
                  </Button>
                {:else}
                  <Button size="sm" variant="secondary" disabled={!currentConversation || archivingConversation} onclick={archiveConversation}>
                    {archivingConversation ? 'Archiving...' : 'Archive'}
                  </Button>
                {/if}
                <Button size="sm" variant="danger" disabled={!currentConversation || deletingConversation} onclick={deleteConversation}>
                  {deletingConversation ? 'Deleting...' : 'Delete'}
                </Button>
              </div>
            {/if}
          </div>
        </div>
      </div>

      {#if chatSearchOpen}
        <ChatSearchBar
          bind:query={chatSearchQuery}
          results={chatSearchResults}
          selectedIndex={chatSearchSelectedIndex}
          loading={chatSearchLoading}
          disabled={!currentConversation}
          onSubmit={runChatSearch}
          onClose={() => { chatSearchOpen = false; chatSearchResults = []; seededChatSearchSession = null; seededChatSearchRef = null; }}
          onNext={() => selectChatSearchResult(chatSearchSelectedIndex + 1)}
          onPrevious={() => selectChatSearchResult(chatSearchSelectedIndex - 1)}
          onSelect={selectChatSearchResult}
        />
      {/if}

      <!--
        Expanded session details panel. Sits directly below the header
        so it extends the header's width, not the title column's. On
        mobile, it also carries the agent / context / session-id
        chips that are hidden in the sub-header row above.
      -->
      {#if headerInfoOpen && currentConversation}
        {@const panelAgent = conversationAgentForDisplay(currentConversation)}
        {@const panelPerformance = activeLastGeneration()}
        {@const panelResponsiveness = panelPerformance ? responsivenessBadge(panelPerformance) : null}
        <div class="max-h-[min(70vh,calc(var(--app-viewport-height,100dvh)-8rem))] overflow-y-auto overscroll-contain border-b border-slate-800/80 bg-slate-900/40 px-3 py-3 sm:max-h-[min(72vh,calc(var(--app-viewport-height,100dvh)-9rem))] sm:px-4 sm:py-4 lg:px-5">
          {#if headerInfoMode === 'context'}
            <button
              class="mb-3 inline-flex items-center gap-1 text-xs font-medium text-slate-400 transition hover:text-slate-200"
              type="button"
              onclick={closeHeaderInfo}
            >
              <ArrowLeft class="h-3.5 w-3.5" />
              Back
            </button>
          {/if}
          {#if headerInfoMode !== 'context'}
            <h2 class="mb-3 text-sm font-semibold text-white">Conversation Info</h2>
          {/if}
          <div class={`mb-3 flex-wrap items-center gap-2 text-sm text-slate-300 sm:hidden ${headerInfoMode === 'context' ? 'hidden' : 'flex'}`}>
            <div class="flex items-center gap-2 rounded-lg bg-slate-900/80 px-2 py-1">
              <AgentAvatar name={panelAgent.display_name ?? panelAgent.name} avatarUrl={panelAgent.avatar_url ?? null} class="h-5 w-5" />
              <span>{panelAgent.display_name ?? panelAgent.name}</span>
            </div>
            {#if conversationTaskId(currentConversation)}
              <a
                href="/tasks/{conversationTaskId(currentConversation)}"
                class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-slate-400 transition hover:border-sky-400/40 hover:text-sky-200"
              >
                {contextTypeBadge(currentConversation)}
              </a>
            {:else}
              <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-slate-400">
                {contextTypeBadge(currentConversation)}
              </span>
            {/if}
            {#if backingSessionBadge}
              <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400" title="Backing sessions">{backingSessionBadge}</span>
            {/if}
            {#if currentConversation.active_session_id}
              <button
                class="flex items-center gap-1 font-mono text-xs text-slate-500 transition hover:text-slate-300"
                onclick={copySessionId}
                type="button"
                title="Copy full session ID"
              >
                {currentConversation.active_session_id.slice(0, 12)}
                {#if sessionIdCopied}
                  <Check class="h-3 w-3 text-emerald-400" />
                {:else}
                  <Copy class="h-3 w-3" />
                {/if}
              </button>
            {/if}
          </div>
          {#if sessionInfoLoading}
            <p class="text-xs text-slate-500">Loading session details…</p>
          {:else if sessionInfo}
            {@const panelContextUsage = contextUsage ?? sessionInfo.context_usage}
            {@const narrativeText = sessionNarrativeText(sessionInfo)}
            {#if headerInfoMode !== 'context'}
              <SessionDetailsContent
                detail={sessionInfo}
                sessionId={currentConversation.active_session_id ?? sessionInfo.intaris_session_id}
                contextUsage={panelContextUsage}
                performance={activeLastGeneration()}
                onOpenIntaris={openIntarisSession}
              />
            {:else}
            {#if headerInfoMode !== 'context' && narrativeText}
              <div class="mb-3">
                <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">{sessionNarrativeLabel(sessionInfo)}</p>
                <p class={`mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-200 ${sessionNarrativeExpanded ? '' : 'line-clamp-3 sm:line-clamp-none'}`}>{narrativeText}</p>
                <button
                  class="mt-1 inline text-xs font-medium text-sky-300 underline-offset-4 hover:text-sky-200 hover:underline sm:hidden"
                  type="button"
                  onclick={() => { sessionNarrativeExpanded = !sessionNarrativeExpanded; }}
                >
                  {sessionNarrativeExpanded ? 'Show less' : 'Show more'}
                </button>
              </div>
            {/if}
            {#if headerInfoMode !== 'context'}
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
                <span>Status: <span class="text-slate-200">{sessionInfo.status}</span></span>
                <span>Calls: <span class="text-slate-200">{sessionInfo.total_calls}</span></span>
                <span class="text-emerald-400">{sessionInfo.approved_count} approved</span>
                <span class="text-rose-400">{sessionInfo.denied_count} denied</span>
                <span class="text-sky-400">{sessionInfo.escalated_count} escalated</span>
              </div>
              <Button size="sm" variant="secondary" onclick={() => void openIntarisSession(sessionInfo?.intaris_session_id ?? '')}>Open in Intaris</Button>
            </div>
            {/if}
            {#if headerInfoMode !== 'context'}
              <div class="mt-4 grid gap-3 lg:grid-cols-2">
                <section class="rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
                  <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Runtime</p>
                  <dl class="mt-2 grid gap-2 text-xs sm:grid-cols-2">
                    <div>
                      <dt class="text-slate-500">Model</dt>
                      <dd class="mt-0.5 break-all text-slate-200">{panelPerformance?.model ?? panelContextUsage?.model ?? 'Unknown'}</dd>
                    </div>
                    <div>
                      <dt class="text-slate-500">Provider</dt>
                      <dd class="mt-0.5 text-slate-200">{panelPerformance?.provider_name ?? panelPerformance?.provider_id ?? panelContextUsage?.provider_id ?? 'Cloud or default'}</dd>
                    </div>
                    {#if panelPerformance?.runtime}
                      <div>
                        <dt class="text-slate-500">Runtime</dt>
                        <dd class="mt-0.5 text-slate-200">{panelPerformance.runtime}</dd>
                      </div>
                    {/if}
                    {#if panelPerformance?.executor_name || panelPerformance?.executor_id}
                      <div>
                        <dt class="text-slate-500">Executor</dt>
                        <dd class="mt-0.5 text-slate-200">{panelPerformance.executor_name ?? panelPerformance.executor_id}</dd>
                      </div>
                    {/if}
                    {#if panelPerformance?.configured_context_tokens}
                      <div>
                        <dt class="text-slate-500">Configured context</dt>
                        <dd class="mt-0.5 text-slate-200">{formatTokenCount(panelPerformance.configured_context_tokens)} tokens</dd>
                      </div>
                    {/if}
                    {#if panelPerformance?.quantization}
                      <div>
                        <dt class="text-slate-500">Quantization</dt>
                        <dd class="mt-0.5 text-slate-200">{panelPerformance.quantization}</dd>
                      </div>
                    {/if}
                  </dl>
                </section>
                <section class="rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
                  <div class="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Last generation</p>
                      <p class="mt-1 text-xs text-slate-500">
                        {panelPerformance ? `Measured ${new Date(panelPerformance.measured_at).toLocaleString()}` : 'Local runtime telemetry is not available for this model.'}
                      </p>
                    </div>
                    {#if panelResponsiveness}
                      <span
                        class={`rounded-full border px-2 py-1 text-[10px] font-semibold ${panelResponsiveness.tone === 'good' ? 'border-emerald-400/30 bg-emerald-400/10 text-emerald-200' : panelResponsiveness.tone === 'slow' ? 'border-amber-400/30 bg-amber-400/10 text-amber-100' : 'border-sky-400/30 bg-sky-400/10 text-sky-100'}`}
                        title={panelResponsiveness.detail}
                      >
                        {panelResponsiveness.label}
                      </span>
                    {/if}
                  </div>
                  {#if panelPerformance}
                    <div class="mt-3 grid gap-2 sm:grid-cols-2">
                      {#each localPerformanceMetrics(panelPerformance) as metric}
                        <div class="rounded-xl border border-slate-800/70 bg-slate-950/40 px-2.5 py-2">
                          <p class="text-[10px] uppercase tracking-wide text-slate-500">{metric.label} · {metric.raw}</p>
                          <p class="mt-1 text-xs font-medium text-slate-100">{metric.value}</p>
                        </div>
                      {/each}
                    </div>
                    {#if panelResponsiveness}
                      <p class="mt-2 text-[11px] text-slate-500">{panelResponsiveness.label} for {panelResponsiveness.detail}.</p>
                    {/if}
                    {#if panelPerformance.processor || panelPerformance.gpu_residency || panelPerformance.digest}
                      <p class="mt-2 break-all text-[11px] text-slate-500">
                        {[panelPerformance.processor ? `Processor ${panelPerformance.processor}` : null, panelPerformance.gpu_residency ? `GPU residency ${panelPerformance.gpu_residency}` : null, panelPerformance.digest ? `Digest ${panelPerformance.digest}` : null].filter(Boolean).join(' · ')}
                      </p>
                    {/if}
                  {/if}
                </section>
              </div>
            {/if}
            {#if panelContextUsage}
              {@const policy = panelContextUsage.projection_policy}
              <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
                <div class="mb-3 flex items-center gap-3">
                  <svg class="h-12 w-12 shrink-0 -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
                    <circle cx="18" cy="18" r={CONTEXT_DONUT_RADIUS} fill="none" stroke="rgba(100,116,139,0.35)" stroke-width="3" />
                    <circle
                      cx="18"
                      cy="18"
                      r={CONTEXT_DONUT_RADIUS}
                      fill="none"
                      stroke={contextUsageStroke(panelContextUsage)}
                      stroke-linecap="round"
                      stroke-width="3"
                      stroke-dasharray={CONTEXT_DONUT_CIRCUMFERENCE}
                      stroke-dashoffset={contextDonutDashOffset(contextWindowUsagePercentage(panelContextUsage))}
                    />
                  </svg>
                  <div>
                    <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Context window</p>
                    <p class="mt-1 text-sm font-medium text-white">
                      {formatTokenCount(panelContextUsage.prompt_tokens)} / {formatTokenCount(panelContextUsage.max_context_tokens)}
                      <span class={contextUsageColor(panelContextUsage)}>({formatPercent(contextWindowUsagePercentage(panelContextUsage))})</span>
                    </p>
                    <p class="mt-0.5 text-xs text-slate-500">
                      {panelContextUsage.model}
                      {panelContextUsage.reasoning_effort ? ` · thinking ${panelContextUsage.reasoning_effort}` : ''}
                      {contextUsageProfileLabel(panelContextUsage) ? ` · profile ${contextUsageProfileLabel(panelContextUsage)}` : ''}
                    </p>
                  </div>
                </div>
                <div class="grid gap-2 text-xs text-slate-400 xl:grid-cols-[minmax(0,1fr)_minmax(16rem,0.45fr)]">
                  <div class="space-y-2">
                    {#each contextBudgetBars(panelContextUsage) as metric}
                      <div class="rounded-xl border border-slate-800/70 bg-slate-950/40 px-2.5 py-2">
                        <div class="mb-1.5 flex items-center justify-between gap-3">
                          <span class="inline-flex items-center gap-1.5">
                            {metric.label}
                            <Popover text={metric.description} placement="top">
                              <button
                                class="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-700 text-[10px] font-semibold text-slate-500 transition hover:border-sky-400/40 hover:text-sky-200"
                                type="button"
                                aria-label={`${metric.label} help`}
                              >
                                ?
                              </button>
                            </Popover>
                          </span>
                          <span class="text-slate-200">{metric.value}</span>
                        </div>
                        <div class="h-1.5 overflow-hidden rounded-full bg-slate-800">
                          <div class={`h-full rounded-full ${metric.color}`} style={`width: ${metric.percent}%`}></div>
                        </div>
                      </div>
                    {/each}
                  </div>
                  <div class="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
                    {#each contextBudgetChips(panelContextUsage) as metric}
                      <div class="rounded-xl border border-slate-800/70 bg-slate-950/40 px-2.5 py-2">
                        <div class="flex items-baseline justify-between gap-2">
                          <span class="inline-flex items-center gap-1.5">
                            {metric.label}
                            <Popover text={metric.description} placement="top">
                              <button
                                class="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-700 text-[10px] font-semibold text-slate-500 transition hover:border-sky-400/40 hover:text-sky-200"
                                type="button"
                                aria-label={`${metric.label} help`}
                              >
                                ?
                              </button>
                            </Popover>
                          </span>
                          <span class="text-slate-200">{metric.value}</span>
                        </div>
                      </div>
                    {/each}
                  </div>
                </div>
              </div>
            {/if}
            {/if}
          {:else}
            <p class="text-xs text-slate-500">Unable to load session details.</p>
          {/if}
          <div class={`mt-3 flex-wrap gap-2 sm:hidden ${headerInfoMode === 'context' ? 'hidden' : 'flex'}`}>
            {#if currentConversation.status === 'archived'}
              <Button size="sm" variant="secondary" disabled={archivingConversation} onclick={restoreConversation}>
                {archivingConversation ? 'Restoring…' : 'Restore'}
              </Button>
            {:else}
              <Button size="sm" variant="secondary" disabled={!currentConversation || archivingConversation} onclick={archiveConversation}>
                {archivingConversation ? 'Archiving…' : 'Archive'}
              </Button>
            {/if}
            <Button size="sm" variant="danger" disabled={!currentConversation || deletingConversation} onclick={deleteConversation}>
              {deletingConversation ? 'Deleting…' : 'Delete'}
            </Button>
          </div>
        </div>
      {/if}

      <!-- Message area + composer -->
      <div class="flex min-h-0 flex-1 flex-col gap-2 px-0 pt-2 pb-0 sm:gap-4 sm:px-4 sm:py-4">
        {#if isMemoryDegraded()}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            Memory is currently unavailable — this conversation won't have access to past context.
          </div>
        {/if}

        {#if showPushPrompt}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="min-w-0 flex-1">
                <p class="font-medium">Native notifications</p>
                <p class="mt-1 text-sky-100/80">{pushPromptText()}</p>
                {#if pushPromptError}
                  <p class="mt-2 text-xs text-rose-200">{pushPromptError}</p>
                {/if}
                {#if pushDeliveryError}
                  <p class="mt-2 break-words font-mono text-xs text-amber-100">Last delivery error: {pushDeliveryError}</p>
                {/if}
                <button class="mt-2 text-xs font-medium text-sky-100 underline-offset-4 hover:underline" type="button" onclick={() => goto('/settings?tab=notifications')}>
                  Manage in Settings
                </button>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                {#if !needsIosHomeScreenInstall()}
                  <Button size="sm" variant="secondary" disabled={pushPromptBusy} onclick={() => void enableChatNotifications()}>
                    {pushPromptBusy ? 'Enabling…' : 'Enable'}
                  </Button>
                {/if}
                <button
                  aria-label="Dismiss notification prompt"
                  class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-sky-100 transition hover:bg-sky-500/20"
                  onclick={() => {
                    pushPromptDismissed = true;
                    setWebPushPromptDismissed(true);
                  }}
                  type="button"
                >
                  <X class="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
        {/if}

        {#if queuedCount > 0}
          <div class="rounded-2xl border border-sky-400/30 bg-sky-500/10 px-3 py-3 text-sm text-sky-100">
            <p class="font-medium">Current turn is still running; queued messages below will run next.</p>
            {#if queuedMessages.length > 0}
              <div class="mt-3 space-y-1.5">
                {#each queuedMessages as queued (queued.queue_id)}
                  {@const canMutateQueue = canMutateQueuedMessages()}
                  {@const isExpanded = queueIsExpanded(queued.queue_id)}
                  <div class="chat-queue-item-enter rounded-xl border border-sky-300/20 bg-slate-950/40 px-2.5 py-2">
                    <div class="flex min-w-0 flex-wrap items-center gap-2 sm:flex-nowrap">
                      <span class="shrink-0 rounded-full border border-sky-300/20 bg-sky-400/10 px-2 py-0.5 text-[11px] font-medium text-sky-100">#{queued.position}</span>
                      <span class="shrink-0 text-[11px] uppercase tracking-wide text-sky-200/70">waiting</span>
                      <p class="min-w-0 flex-1 truncate text-slate-100">{queued.content}</p>
                      {#if queued.attachments?.length}
                        <span class="shrink-0 rounded-full border border-sky-300/20 px-2 py-0.5 text-[11px] text-sky-100/80">{queued.attachments.length} attachment{queued.attachments.length === 1 ? '' : 's'}</span>
                      {/if}
                      <div class="ml-auto flex shrink-0 items-center gap-1.5">
                        <Button size="sm" variant="ghost" disabled={queueBusyId === queued.queue_id} aria-expanded={isExpanded} aria-label={`${isExpanded ? 'Collapse' : 'Expand'} queued message #${queued.position}`} onclick={() => toggleQueuedMessage(queued.queue_id)}>{isExpanded ? 'Collapse' : 'Details'}</Button>
                        {#if canMutateQueue}
                          <Button size="sm" variant="secondary" disabled={queueBusyId === queued.queue_id} onclick={() => startQueuedMessageEdit(queued)}>Edit</Button>
                          <Button size="sm" variant="danger" disabled={queueBusyId === queued.queue_id} onclick={() => void deleteQueuedMessage(queued.queue_id)}>Delete</Button>
                        {/if}
                      </div>
                    </div>
                    {#if queueEditingId === queued.queue_id}
                      <div class="mt-2 rounded-lg border border-slate-800/70 bg-slate-950/70 p-2.5">
                        <textarea
                          class="min-h-24 w-full resize-y rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none transition placeholder:text-slate-500 focus:border-sky-400"
                          bind:value={queueEditContent}
                          disabled={queueBusyId === queued.queue_id}
                          aria-label={`Edit queued message #${queued.position}`}
                        ></textarea>
                        {#if queueEditError}
                          <p class="mt-2 text-xs text-rose-200">{queueEditError}</p>
                        {/if}
                        {#if queued.attachments?.length}
                          <p class="mt-2 text-xs text-sky-100/70">{queued.attachments.length} attachment{queued.attachments.length === 1 ? '' : 's'} attached. To change attachments, delete this queued message and recreate it.</p>
                        {/if}
                        <div class="mt-2 flex justify-end gap-2">
                          <Button size="sm" variant="ghost" disabled={queueBusyId === queued.queue_id} onclick={cancelQueuedMessageEdit}>Cancel</Button>
                          <Button size="sm" variant="primary" disabled={queueBusyId === queued.queue_id || !queueEditContent.trim()} onclick={() => void saveQueuedMessageEdit(queued.queue_id)}>
                            {queueBusyId === queued.queue_id ? 'Saving…' : 'Save'}
                          </Button>
                        </div>
                      </div>
                    {:else if isExpanded}
                      <div class="mt-2 rounded-lg border border-slate-800/70 bg-slate-950/70 p-2.5">
                        <p class="max-h-[32vh] overflow-auto whitespace-pre-wrap break-words text-slate-100">{queued.content}</p>
                        {#if queued.attachments?.length}
                          <p class="mt-2 text-xs text-sky-100/70">{queued.attachments.length} attachment{queued.attachments.length === 1 ? '' : 's'} attached. To change attachments, delete this queued message and recreate it.</p>
                        {/if}
                      </div>
                    {/if}
                  </div>
                {/each}
              </div>
            {:else}
              <p class="mt-1 text-sky-100/80">{queuedCount} queued message{queuedCount === 1 ? '' : 's'} waiting. Details are syncing…</p>
            {/if}
          </div>
        {/if}

        {#if error}
          <div class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-3 py-3 text-sm text-rose-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="min-w-0 flex-1 break-words">{error}</p>
              <div class="flex shrink-0 items-center gap-2">
                {#if lastRecoverableTurnId}
                  <Button size="sm" variant="secondary" onclick={retryLastTurn}>Retry</Button>
                {/if}
                <button
                  aria-label="Dismiss error"
                  class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-rose-100 transition hover:bg-rose-500/20"
                  onclick={() => { error = ''; lastRecoverableMessage = ''; lastRecoverableTurnId = null; }}
                  type="button"
                >
                  <X class="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
        {/if}

        {#if !error && canRetryFailedTurn}
          <div class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-3 py-3 text-sm text-rose-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="min-w-0 flex-1 break-words">
                The last turn failed after your message. Retry it instead of typing continue.
              </p>
              <Button size="sm" variant="secondary" onclick={continueFailedTurn}>Retry failed turn</Button>
            </div>
          </div>
        {/if}

        {#if initialLoadTimedOut && !currentConversation && !error}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-4 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p class="font-medium">Still loading conversation</p>
                <p class="mt-1 text-sky-100/80">History or live session setup is taking longer than expected.</p>
              </div>
              <div class="flex gap-2">
                <Button size="sm" variant="secondary" onclick={() => void openConversation(conversationIdFromRoute())}>Retry</Button>
                <Button size="sm" variant="secondary" onclick={() => goto('/settings/system')}>Diagnostics</Button>
              </div>
            </div>
          </div>
        {/if}

        {#if controllerRecoveryPending}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>
                {shouldContinueControllerRecovery(controllerRecoveryAttempts)
                  ? 'Controller is reconnecting. Your saved conversation remains visible and updates will retry automatically.'
                  : 'Controller is still unavailable. Your saved conversation remains visible; retry when it is back.'}
              </p>
              <Button size="sm" variant="secondary" onclick={() => void retryControllerRecoveryNow()}>Retry now</Button>
            </div>
          </div>
        {/if}

        {#if sessionsError && !isPreSessionConversation && !controllerRecoveryPending}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-3 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Session details are temporarily unavailable: {sessionsError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry</Button>
            </div>
          </div>
        {/if}

        {#if historyError && !isPreSessionConversation && !controllerRecoveryPending}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Conversation history is temporarily unavailable: {historyError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry history</Button>
            </div>
          </div>
        {/if}

        {#if escalationError && !controllerRecoveryPending}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Escalation updates are temporarily unavailable: {escalationError}</p>
              <Button size="sm" variant="secondary" onclick={() => refreshEscalations()}>Refresh escalations</Button>
            </div>
          </div>
        {/if}

        {#if cachedConversationRefreshing && currentConversation && !historyError && !sessionsError}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-100">
            <div class="flex items-center gap-2">
              <RefreshCw class="h-3.5 w-3.5 animate-spin" />
              <span>Showing cached conversation, refreshing latest messages…</span>
            </div>
          </div>
        {/if}

        <!-- Timeline -->
        <TimelineViewport
          items={[]}
          hasStreamingItems={displayedTimelineHasStreaming}
          agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
          bind:viewportElement={timelineEl}
          bind:contentElement={timelineContentEl}
          bind:userScrolledUp
          class="relative min-h-0 flex-1 overflow-y-auto overscroll-contain [overflow-anchor:auto] px-2.5 py-1.5 sm:p-4"
          contentClass="space-y-3 [overflow-anchor:auto]"
          onScroll={handleTimelineScroll}
          onWheel={handleTimelineWheel}
          onTouchStart={handleTimelineTouchStart}
          onTouchMove={handleTimelineTouchMove}
          onTouchEnd={handleTimelineTouchEnd}
          onKeydown={handleTimelineKeydown}
          onPointerDown={closeHeaderInfo}
           onViewSession={handleViewSession}
           onJumpToBottom={jumpToBottom}
           onJumpToActiveStart={jumpToActiveTimelineStart}
          autoScrollOnResize={false}
          followPausedLabel="Scroll to bottom"
          testId="timeline-viewport"
        >
            {#if (visibleStartIndex > 0 || hasOlderMessages) && !loadingOlderMessages}
              <button
                class="mx-auto block rounded-full border border-slate-700 bg-slate-900/80 px-3 py-1.5 text-xs text-slate-300 transition hover:border-sky-500/50 hover:text-sky-100"
                onclick={() => void loadOlder()}
                type="button"
              >
                Load older messages
              </button>
            {/if}
            {#if loadingOlderMessages}
              <p class="px-4 py-2 text-center text-xs text-slate-500">Loading older messages…</p>
            {/if}

            {#if !currentConversation && !error}
              <div class="rounded-2xl border border-dashed border-slate-700 px-4 py-10 text-center text-sm text-slate-400">
                {#if initialLoadTimedOut}
                  Conversation data is still loading. Use Retry above if it stays stuck.
                {:else}
                  Loading conversation history and preparing the live stream.
                {/if}
              </div>
            {:else if displayedTimeline.length === 0}
              {#if conversationSubloadsLoading && !historyError && !sessionsError}
                <div class="rounded-2xl border border-dashed border-slate-700 px-4 py-10 text-center text-sm text-slate-400">
                  {#if initialLoadTimedOut}
                    Conversation data is still loading. Use Retry above if it stays stuck.
                  {:else}
                    Loading conversation history and preparing the live stream.
                  {/if}
                </div>
              {:else if !historyError && !sessionsError}
                <p class="px-4 py-16 text-center text-sm text-slate-500">
                  Send the first message to start this conversation.
                </p>
              {/if}
            {:else}
              <ChatV2TimelineList
                items={displayedTimeline}
                scope={currentConversation ? { key: `conversation:${currentConversation.conversation_id}`, kind: 'conversation', conversation_id: currentConversation.conversation_id } : undefined}
                cycleStates={chatV2Store.cycleStates}
                agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
                searchQuery={chatSearchQuery}
                searchMatchedIds={chatSearchOpen ? chatSearchMatchedMessageIds : emptySearchMatchedIds}
                searchSelectedId={selectedChatSearchTargetId}
                preferences={$userPreferences}
                onViewSession={handleViewSession}
              />
            {/if}

            <!-- Escalation prompts (sequential: show one at a time) -->
            {#if escalationResolutionPending}
              <div class="rounded-3xl border border-sky-500/30 bg-sky-500/10 px-4 py-4 shadow-card">
                <div class="flex flex-wrap items-center justify-between gap-4">
                  <div>
                    <p class="text-xs font-medium uppercase tracking-[0.25em] text-sky-200">Approval submitted</p>
                    <h3 class="mt-1 text-base font-semibold text-white">{escalationResolutionPending.tool_name ?? 'Escalated action'}</h3>
                  </div>
                  <LiveDots inline={true} size="sm" tone="sky" label="Waiting for controller acknowledgement" />
                </div>
              </div>
            {:else if escalations.length > 0}
              {@const current = escalations[0]}
              <div class="space-y-3">
                <EscalationPrompt
                  item={current}
                  secondsRemaining={escalationSecondsRemaining(current)}
                  pending={escalationBusyCallId === current.call_id}
                  queuedCount={escalations.length - 1}
                  onApprove={() => handleEscalationDecision(current.call_id, 'approve')}
                  onDeny={() => handleEscalationDecision(current.call_id, 'deny')}
                />
              </div>
            {/if}

        </TimelineViewport>

        <div bind:this={footerChromeEl} class="shrink-0 space-y-3">
          {#if shouldShowChatTodoDrawer}
            <TimelineTodoDrawer todos={chatTodos} bind:open={chatTodoDrawerOpen} />
          {/if}

          <!-- Composer or read-only banner -->
          {#if currentConversation && isManagedConversation(currentConversation)}
            <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
              <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div class="min-w-0 sm:flex-1">
                  <p class="font-medium">Agent work</p>
                  <p class="mt-1 text-sky-100/80">
                    Read-only target conversation · state {managedConversationState(currentConversation)} · turn {managedTurnState(currentConversation)}
                  </p>
                  <div class="mt-2 flex flex-wrap items-center gap-3">
                    {#if currentConversation.managed_agent?.controller_conversation_id}
                      <a
                        class="inline-flex items-center gap-1 text-xs font-medium text-sky-100 underline-offset-4 hover:underline"
                        href={conversationUrl(currentConversation.managed_agent.controller_conversation_id)}
                      >
                        Open controlling conversation
                        <ExternalLink class="h-3 w-3" />
                      </a>
                    {/if}
                    {#if currentConversation.managed_agent?.follow_up_conversation_id}
                      <a
                        class="inline-flex items-center gap-1 text-xs font-medium text-sky-100 underline-offset-4 hover:underline"
                        href={conversationUrl(currentConversation.managed_agent.follow_up_conversation_id)}
                      >
                        Open follow-up conversation
                        <ExternalLink class="h-3 w-3" />
                      </a>
                    {/if}
                  </div>
                  {#if currentConversation.managed_agent?.last_error && managedTurnState(currentConversation) !== 'running' && managedTurnState(currentConversation) !== 'queued'}
                    <p class="mt-2 break-words text-xs text-rose-100">Last error: {currentConversation.managed_agent.last_error}</p>
                  {/if}
                </div>
                <div class="grid w-full grid-cols-2 gap-2 sm:flex sm:w-auto sm:shrink-0 sm:flex-wrap sm:justify-end">
                  {#if managedConversationActive(currentConversation)}
                    <Button class="w-full sm:w-auto" size="sm" variant="danger" disabled={managedActionBusy !== null} onclick={stopManagedConversation}>
                      {managedActionBusy === 'stop' ? 'Stopping…' : 'Stop'}
                    </Button>
                  {/if}
                  <Button
                    class="w-full sm:w-auto"
                    size="sm"
                    variant="secondary"
                    disabled={managedActionBusy !== null || managedConversationState(currentConversation) === 'closed' || managedConversationActive(currentConversation)}
                    onclick={() => {
                      managedActionError = '';
                      managedInstructionOpen = !managedInstructionOpen;
                    }}
                  >
                    Send instruction
                  </Button>
                  <Button
                    class={managedConversationActive(currentConversation) ? 'col-span-2 w-full sm:col-span-1 sm:w-auto' : 'w-full sm:w-auto'}
                    size="sm"
                    variant="secondary"
                    disabled={managedActionBusy !== null || managedConversationState(currentConversation) === 'closed' || managedConversationActive(currentConversation)}
                    onclick={takeControlOfManagedConversation}
                  >
                    {managedActionBusy === 'take-control' ? 'Forking…' : 'Take control'}
                  </Button>
                </div>
              </div>
              {#if managedConversationActive(currentConversation)}
                <p class="mt-2 text-xs text-sky-100/70">Stop the current turn before sending a manual instruction or taking control.</p>
              {/if}
              {#if managedInstructionOpen && managedConversationState(currentConversation) !== 'closed'}
                <form class="mt-3 space-y-2" onsubmit={(event) => { event.preventDefault(); void sendManagedInstruction(); }}>
                  <textarea
                    class="min-h-24 w-full rounded-2xl border border-sky-300/25 bg-slate-950/40 px-3 py-2 text-sm text-sky-50 outline-none placeholder:text-sky-100/45 focus:border-sky-200/60"
                    placeholder="Send a message into this managed conversation while keeping the parent notification link intact…"
                    value={managedInstruction}
                    disabled={managedActionBusy !== null || managedConversationActive(currentConversation)}
                    oninput={(event) => { managedInstruction = event.currentTarget.value; managedActionError = ''; }}
                  ></textarea>
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <p class="text-xs text-sky-100/65">This uses the managed send path; the conversation remains managed.</p>
                    <div class="flex items-center gap-2">
                      <Button size="sm" variant="ghost" type="button" disabled={managedActionBusy !== null} onclick={() => { managedInstructionOpen = false; managedActionError = ''; }}>
                        Cancel
                      </Button>
                      <Button size="sm" type="submit" disabled={managedActionBusy !== null || managedConversationActive(currentConversation) || !managedInstruction.trim()}>
                        {managedActionBusy === 'send' ? 'Sending…' : 'Send'}
                      </Button>
                    </div>
                  </div>
                </form>
              {/if}
              {#if managedActionError}
                <p class="mt-2 break-words text-xs text-rose-100">{managedActionError}</p>
              {/if}
            </div>
          {:else if currentConversation && !isWebConversation(currentConversation)}
            <div class="rounded-2xl border border-slate-700/60 bg-slate-900/60 px-4 py-3 text-center text-sm text-slate-400">
              This conversation is from <span class="font-medium text-slate-300">{contextTypeBadge(currentConversation)}</span>. Read-only in web UI.
            </div>
          {:else if currentConversation && currentConversation.status === 'archived'}
            <div class="rounded-2xl border border-slate-700/60 bg-slate-900/60 px-4 py-3 text-center text-sm text-slate-400">
              This conversation is archived.
            </div>
          {:else if currentConversation && currentConversation.status === 'deleted'}
            <div class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-center text-sm text-rose-100">
              This conversation has been deleted.
            </div>
          {:else if isSessionBlocked()}
            <div class="space-y-3 rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-center text-sm text-sky-100">
              <p>
                {#if activeSessionStatus() === 'suspended'}
                  This session is suspended.
                {:else if activeSessionStatus() === 'terminated'}
                  This session has been terminated.
                {:else}
                  This session has ended ({activeSessionStatus()}).
                {/if}
              </p>
              <Button size="sm" onclick={startNewConversationFromBlockedSession}>
                Start new conversation
              </Button>
            </div>
          {:else}
          <!--
            Composer: sits flush at the bottom of the viewport and spans
            the full width of the chat area. No outer card, no recessed
            background — the textarea below carries its own border. The
            visualViewport-sized app shell moves the footer above the
            keyboard. The bottom control inset is a fixed small clearance
            when the keyboard is closed and zero while it is open, so iOS
            home-indicator clearance belongs to the same transparent footer
            surface rather than appearing as a detached background slab.
          -->
          <form class="shrink-0 space-y-2 px-3 pt-3 sm:space-y-3 sm:px-5 sm:pt-4" style="padding-bottom: var(--app-bottom-control-inset);" onsubmit={(event) => { event.preventDefault(); void handleSend(); }}>
            <!--
              Slash command suggestions dropdown. Caps its height and scrolls
              internally so a long match list cannot push the composer up
              and force the timeline to reflow (the regression that read as
              "the whole page including the header scrolls when typing /").
              `overscroll-contain` keeps the gesture trapped inside the box
              instead of bubbling to the timeline.
            -->
            {#if slashSuggestionsVisible}
              <div class="mb-1 max-h-[40vh] overflow-y-auto overscroll-contain rounded-xl border border-slate-700 bg-slate-900/95 py-1 text-sm shadow-lg">
                {#each slashFilteredSuggestions as suggestion, i}
                  <button
                    class="flex w-full items-center gap-3 px-3 py-1.5 text-left text-xs transition {i === slashSelectedIndex ? 'bg-slate-700/60 text-slate-100' : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'}"
                    onmousedown={(event) => handleSlashSuggestionPointerDown(event, i)}
                    type="button"
                  >
                    <span class="min-w-0 font-mono font-medium text-sky-400">
                      {suggestion.kind === 'parameter' ? suggestion.value : suggestion.command}
                    </span>
                    <span class="min-w-0 flex-1 truncate opacity-70">
                      {#if suggestion.kind === 'parameter' && suggestion.label !== suggestion.value}
                        {suggestion.label}
                        {#if suggestion.description}
                          <span class="opacity-60"> — {suggestion.description}</span>
                        {/if}
                      {:else}
                        {suggestion.description}
                      {/if}
                    </span>
                    {#if suggestion.badges.length > 0}
                      <span class="flex shrink-0 gap-1">
                        {#each suggestion.badges as badge}
                          <span class="rounded-full border border-slate-600 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-300">{badge}</span>
                        {/each}
                      </span>
                    {/if}
                  </button>
                {/each}
              </div>
            {/if}

            {#if pendingDirectQuestion}
              <div id="pending-direct-question-panel" class="flex max-h-[min(58vh,calc(var(--app-viewport-height,100dvh)-9.5rem))] min-h-0 flex-col overflow-hidden rounded-2xl border border-sky-500/30 bg-sky-500/10 text-sm text-sky-50 shadow-lg sm:max-h-[min(50vh,calc(var(--app-viewport-height,100dvh)-12rem))]">
                <div class="shrink-0 border-b border-sky-400/15 px-4 py-3">
                  <div class="flex items-center justify-between gap-3">
                    <button
                      type="button"
                      class="flex min-w-0 flex-1 items-center gap-2 rounded-xl text-left transition hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-300/35"
                      aria-expanded={!directQuestionCollapsed}
                      aria-controls="pending-direct-question-panel"
                      onclick={() => { directQuestionCollapsed = !directQuestionCollapsed; }}
                    >
                      {#if directQuestionCollapsed}
                        <ChevronDown class="h-4 w-4 shrink-0 text-sky-200/80" />
                      {:else}
                        <ChevronUp class="h-4 w-4 shrink-0 text-sky-200/80" />
                      {/if}
                      <span class="truncate font-semibold">Assistant requested more input</span>
                    </button>
                    <div class="flex shrink-0 items-center gap-2">
                      {#if directQuestionSubmitting}
                        <LiveDots inline={true} size="sm" label="Answering" />
                      {:else if directQuestionVisibleItems.length > 1}
                        <span class="rounded-full border border-sky-300/25 bg-slate-950/40 px-2 py-0.5 font-mono text-[11px] text-sky-100">
                          {directQuestionCurrentIndex + 1}/{directQuestionVisibleItems.length}
                        </span>
                      {/if}
                    </div>
                  </div>
                  {#if pendingDirectQuestion.context && !directQuestionCollapsed}
                    <p class="mt-2 max-h-16 overflow-y-auto overscroll-contain text-xs leading-5 text-sky-100/80">{pendingDirectQuestion.context}</p>
                  {/if}
                </div>
                {#if directQuestionCollapsed}
                  <p class="px-4 py-3 text-xs text-sky-100/70">
                    Question prompt collapsed. Expand it to answer.
                  </p>
                {:else}
                <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-3">
                  {#if currentDirectQuestion}
                    {#key `${currentDirectQuestion.id}:${directQuestionCurrentIndex}`}
                    <div class="rounded-2xl border border-sky-400/20 bg-slate-950/30 p-3">
                      {#if currentDirectQuestion.header}
                        <p class="text-xs uppercase tracking-[0.2em] text-sky-100/70">{currentDirectQuestion.header}</p>
                      {/if}
                      <p class="text-sm font-medium text-sky-50">{currentDirectQuestion.question}</p>
                      {#if currentDirectQuestion.required}
                        <p class="mt-1 text-[11px] uppercase tracking-[0.18em] text-sky-100/55">Required</p>
                      {:else}
                        <p class="mt-1 text-[11px] uppercase tracking-[0.18em] text-sky-100/45">Optional</p>
                      {/if}
                      {#if currentDirectQuestion.options.length > 0}
                        <div class="mt-3 space-y-2">
                          {#each currentDirectQuestion.options as option (option.id)}
                            <button
                              class={`flex w-full items-start gap-3 rounded-2xl border px-3 py-2 text-left text-xs transition ${directQuestionOptionSelected(currentDirectQuestion.id, option.id) ? 'border-sky-300/70 bg-sky-300/20 text-white' : 'border-sky-400/30 bg-sky-400/10 text-sky-100 hover:bg-sky-400/20'}`}
                              type="button"
                              disabled={directQuestionSubmitting}
                              onclick={() => { submitDirectQuestionOption(currentDirectQuestion, option.id); }}
                            >
                              <span class={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center border ${currentDirectQuestion.multiple ? 'rounded' : 'rounded-full'} ${directQuestionOptionSelected(currentDirectQuestion.id, option.id) ? 'border-sky-200 bg-sky-300 text-slate-950' : 'border-sky-300/50 bg-slate-950/40'}`}>
                                {#if directQuestionOptionSelected(currentDirectQuestion.id, option.id)}
                                  {#if currentDirectQuestion.multiple}
                                    ✓
                                  {:else}
                                    <span class="h-1.5 w-1.5 rounded-full bg-slate-950"></span>
                                  {/if}
                                {/if}
                              </span>
                              <span class="min-w-0">
                                <span class="block font-medium">{option.label}</span>
                                {#if option.description}
                                  <span class="mt-0.5 block text-sky-100/60">{option.description}</span>
                                {/if}
                              </span>
                            </button>
                          {/each}
                        </div>
                      {/if}
                      {#if currentDirectQuestion.allow_custom}
                        <textarea
                          value={directQuestionState(currentDirectQuestion.id).custom}
                          oninput={(event) => setDirectQuestionCustom(currentDirectQuestion.id, event.currentTarget.value)}
                          class="mt-3 min-h-[72px] w-full rounded-2xl border border-sky-400/20 bg-slate-950/60 px-3 py-2 text-sm text-sky-50 placeholder:text-sky-100/50"
                          placeholder={currentDirectQuestion.options.length > 0 ? 'Optional custom answer' : 'Type your answer'}
                          disabled={directQuestionSubmitting}
                        ></textarea>
                      {/if}
                    </div>
                    {/key}
                  {:else}
                    <p class="rounded-2xl border border-sky-400/20 bg-slate-950/30 p-3 text-sky-100/80">Waiting for question details…</p>
                  {/if}
                  {#if currentDirectQuestion?.options.length}
                    <p class="mt-2 text-xs text-sky-100/55">Selecting an option only updates this answer. Use Next or Submit to continue.</p>
                  {/if}
                  {#if directQuestionVisibleItems.length > 1}
                    <div class="mt-3 grid gap-1" style={`grid-template-columns: repeat(${Math.min(directQuestionVisibleItems.length, 7)}, minmax(0, 1fr));`} aria-hidden="true">
                      {#each directQuestionVisibleItems as item, index (item.id)}
                        <span class={`h-1.5 rounded-full ${index === directQuestionCurrentIndex ? 'bg-sky-200' : directQuestionAnswerSatisfies(item) && (directQuestionState(item.id).selected.length > 0 || directQuestionState(item.id).custom.trim()) ? 'bg-sky-400/60' : 'bg-sky-900/70'}`}></span>
                      {/each}
                    </div>
                  {/if}
                </div>
                {#if currentDirectQuestion}
                  <div class="flex shrink-0 items-center gap-2 border-t border-sky-400/15 bg-slate-950/90 px-4 py-3 backdrop-blur">
                    <Button
                      size="sm"
                      variant="secondary"
                      class="border-rose-300/30 bg-rose-500/10 text-rose-100 hover:bg-rose-500/20"
                      disabled={directQuestionSubmitting}
                      onclick={() => { void cancelDirectQuestionRequest(); }}
                    >
                      Stop request
                    </Button>
                    {#if directQuestionVisibleItems.length > 1 && directQuestionCurrentIndex > 0}
                      <Button
                        size="sm"
                        variant="secondary"
                        class="min-w-10 px-2"
                        aria-label="Previous question"
                        disabled={directQuestionSubmitting}
                        onclick={goToPreviousDirectQuestion}
                      >
                        ←
                      </Button>
                    {/if}
                    {#if directQuestionCurrentIndex < directQuestionVisibleItems.length - 1}
                      <Button
                        size="sm"
                        class="ml-auto"
                        disabled={directQuestionSubmitting || !canGoToNextDirectQuestion()}
                        onclick={goToNextDirectQuestion}
                      >
                        Next →
                      </Button>
                    {:else}
                      <Button
                        size="sm"
                        class="ml-auto"
                        disabled={directQuestionSubmitting || !currentDirectQuestionReplyHasAnswer()}
                        onclick={() => { void submitCurrentDirectQuestionReply(); }}
                      >
                        Submit
                      </Button>
                    {/if}
                  </div>
                {/if}
                {/if}
              </div>
            {:else if pendingCredentialRequest}
              <CredentialRequestForm
                compact={true}
                notification={pendingCredentialRequest}
                onResolved={async () => {
                  pendingCredentialRequest = null;
                  await refreshPendingDirectQuestion();
                }}
              />
            {:else}
            {#if voiceTranscribing}
              <div class="flex items-center gap-2 rounded-2xl border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-xs text-sky-100" aria-live="polite">
                <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-sky-300"></span>
                Transcribing voice message…
              </div>
            {/if}
            {#if persistentChatMode === 'plan'}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-sky-300/25 bg-sky-300/[0.055] px-3 py-2 text-xs text-sky-100" aria-live="polite">
                <span><strong class="font-semibold">Plan mode active.</strong> This chat is read-only until you send <span class="font-mono">/build</span> or <span class="font-mono">/default</span>.</span>
                <span class="rounded-full border border-sky-300/25 bg-sky-300/[0.055] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.2em] text-sky-100">Plan</span>
              </div>
            {:else if persistentChatMode === 'build'}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-amber-300/35 bg-amber-300/[0.075] px-3 py-2 text-xs text-amber-100" aria-live="polite">
                <span><strong class="font-semibold">Build mode active.</strong> This chat is using implementation mode until you send <span class="font-mono">/default</span>.</span>
                <span class="rounded-full border border-amber-300/35 bg-amber-300/[0.075] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.2em] text-amber-100">Build</span>
              </div>
            {/if}
            <ComposerAttachments
              attachments={composerAttachments}
              onremove={removeAttachment}
              disabled={directQuestionSubmitting || voiceTranscribing}
            />
            <!--
              iMessage-style single-line composer:
              * Paperclip on the leading edge is a <label> wrapping an
                sr-only file input. Native label→input activation opens
                the iOS file picker reliably without the previous
                opacity-0 overlay, which on iOS used to show its tap
                highlight across the textarea width when the invisible
                <input>'s intrinsic box spilled past the 36px icon.
              * The textarea grows vertically up to a cap. Using
                `items-center` on the pill keeps the paperclip, the
                text baseline, and the trailing button visually on one
                line when the composer is a single row.
              * The trailing icon is context-sensitive: a send arrow when
                there is content to submit, a queued-send variant when a
                turn is already running, a stop square only when running
                with no draft, and nothing when idle + empty.
              * Enter defaults to newline; Cmd/Ctrl+Enter always
                submits, and the stored Enter-to-send preference still
                applies for users who opted in.
            -->
            <div class="flex items-center gap-1 rounded-3xl border border-slate-700 bg-transparent px-2 py-1 transition focus-within:border-sky-400/50 focus-within:ring-2 focus-within:ring-sky-300/20">
              <label
                aria-label="Attach files"
                class="inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-200 focus-within:bg-slate-800/60 focus-within:text-slate-200 {directQuestionSubmitting || voiceTranscribing ? 'pointer-events-none opacity-40' : ''}"
              >
                <Paperclip class="h-4 w-4 pointer-events-none" />
                <input
                  class="sr-only"
                  type="file"
                  multiple
                  disabled={directQuestionSubmitting || voiceTranscribing}
                  onchange={handleComposerFileInputChange}
                />
              </label>
              <textarea
                bind:this={composerElement}
                bind:value={composer}
                rows={1}
                class="min-h-[36px] max-h-[200px] flex-1 resize-none self-center bg-transparent px-1 py-[0.4rem] text-[16px] leading-5 text-slate-100 placeholder:text-slate-500 focus:outline-none focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 sm:text-sm"
                disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup() || directQuestionSubmitting || voiceTranscribing}
                enterkeyhint={enterToSend ? 'send' : 'enter'}
                autocapitalize="sentences"
                spellcheck="true"
                onkeydown={handleComposerKeydown}
                oninput={() => { updateSlashSuggestions(); syncComposerHeight(); }}
                onpaste={(event) => void handlePaste(event)}
                placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : pendingDirectQuestion ? 'Answer the pending clarification request...' : `Message ${currentAgentDisplayName}`}
              ></textarea>
              {#if canSendNow}
                <button
                  type="submit"
                  aria-label={turnInProgress ? 'Queue message' : pendingDirectQuestion ? 'Answer' : 'Send'}
                  title={turnInProgress ? 'Queue message after current turn' : pendingDirectQuestion ? 'Answer' : 'Send'}
                  class="relative inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-sky-500 text-slate-950 transition duration-100 hover:bg-sky-400 active:scale-90 disabled:opacity-50"
                  disabled={directQuestionSubmitting || voiceTranscribing}
                >
                  <ArrowUp class="h-4 w-4" stroke-width="2.5" />
                  {#if turnInProgress}
                    <span class="absolute -bottom-0.5 -right-0.5 inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-950 bg-slate-950 text-sky-300">
                      <ListPlus class="h-2.5 w-2.5" stroke-width="2.5" />
                    </span>
                  {/if}
                </button>
              {:else if turnInProgress}
                <button
                  type="button"
                  aria-label="Cancel turn"
                  title="Cancel turn"
                  class="relative inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-800 text-slate-200 transition hover:bg-slate-700"
                  onclick={() => { haptic.warning(); cancelActiveTurnWithChatV2(); }}
                >
                  <span class={`conversation-turn-orbit ${turnOrbitClass(activeTurnChatMode)}`} aria-hidden="true"><span></span></span>
                  <Square class="absolute h-2.5 w-2.5 fill-current text-slate-100" />
                </button>
              {:else}
                <button
                  type="button"
                  aria-label="Open conversation mode"
                  title="Conversation mode"
                  class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-200 disabled:cursor-not-allowed disabled:opacity-40"
                  onclick={() => { conversationModeOpen = true; }}
                  disabled={!currentConversation || isReadOnly(currentConversation) || directQuestionSubmitting || voiceTranscribing}
                >
                  <Headphones class="h-4 w-4" />
                </button>
                <MicRecorderButton
                  disabled={directQuestionSubmitting || voiceTranscribing || !currentConversation || isReadOnly(currentConversation)}
                  onrecorded={(attachment) => {
                    composerAttachments = [...composerAttachments, attachment];
                  }}
                  onsendrecorded={async () => {
                    await tick();
                    await handleSend();
                  }}
                />
              {/if}
            </div>
            {/if}
          </form>
          {/if}
        </div>
      </div>

      {#if showNewChatModal}
        <NewChatModal
          agents={agents}
          bind:selectedAgentId={newChatAgentId}
          bind:selectedAgentProfileId={newChatAgentProfileId}
          busy={newChatCreating}
          error={newChatError}
          oncancel={closeNewConversationModal}
          onconfirm={() => void createNewConversation()}
        />
      {/if}

      <!-- Sub-session drawer overlay -->
      {#if subSessionPanelOpen}
        <!-- Backdrop -->
        <button
          class={`absolute inset-0 z-20 bg-slate-950/80 transition-opacity duration-250 ${subSessionClosing ? 'opacity-0' : 'opacity-100'}`}
          onclick={closeSubSessionPanel}
          type="button"
          aria-label="Close sub-session"
        ></button>

        <!-- Drawer -->
        <aside class={`absolute inset-0 z-30 flex h-full min-h-0 w-full flex-col overflow-hidden border-l border-slate-800/80 bg-slate-900 shadow-2xl ${subSessionClosing ? 'animate-slide-out-right' : 'animate-slide-in-right'}`}>
          <div class="flex shrink-0 items-center gap-3 border-b border-slate-800/80 px-4 pb-3 pt-[calc(0.75rem+env(safe-area-inset-top))]">
            <button
              class="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800 hover:text-white"
              onclick={closeSubSessionPanel}
              type="button"
              title="Back to conversation"
            >
              <ArrowLeft class="h-4 w-4" />
            </button>
            <div class="min-w-0 flex-1">
              <p class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Sub-session</p>
              <p class="mt-0.5 truncate font-mono text-xs text-slate-500">{subSessionId.slice(0, 16)}</p>
              <div class="mt-2">
                <LiveDots inline={true} size="sm" tone={subSessionUserScrolledUp ? 'slate' : 'sky'} label={subSessionUserScrolledUp ? 'Live follow paused' : 'Following latest'} />
              </div>
            </div>
            <SessionDetailsButton open={subSessionInfoOpen} onclick={() => { subSessionInfoOpen = !subSessionInfoOpen; }} />
          </div>

          {#if subSessionInfoOpen}
            <SessionDetailsPanel sessionId={subSessionId} onOpenIntaris={openIntarisSession} />
          {/if}

          <ScopedChatV2Timeline
            scope={{
              key: `session:${subSessionId}`,
              kind: 'session',
              conversation_id: currentConversation?.conversation_id ?? null,
              session_id: subSessionId,
              label: subSessionId
            }}
            agent={subSessionAgent() ?? null}
            preferences={$userPreferences}
            bind:userScrolledUp={subSessionUserScrolledUp}
            onViewSession={handleViewSession}
            emptyLabel="No events recorded yet."
          />
        </aside>
      {/if}
    </section>
  </div>
{/if}

<ConversationMode
  open={conversationModeOpen}
  conversationId={currentConversation?.conversation_id ?? ''}
  agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
  onclose={() => { conversationModeOpen = false; }}
  sendEnableTts={(voice) => wsClient.enableTts(voice)}
  sendDisableTts={() => wsClient.disableTts()}
  submitText={(text) => { void sendConversationModeText(text); }}
  subscribeSentenceReady={(handler) => {
    return wsClient.subscribe((event) => {
      if (event.type === 'tts_sentence_ready' && currentConversation && event.conversation_id === currentConversation.conversation_id) {
        handler({
          message_id: event.message_id,
          sentence_index: event.sentence_index,
          text: event.text,
        });
      }
    });
  }}
  subscribeMessageComplete={(handler) => {
    return wsClient.subscribe((event) => {
      if (event.type === 'error') {
        handler();
        return;
      }
      if (event.type === 'message_complete' && currentConversation && event.conversation_id === currentConversation.conversation_id) {
        handler();
      }
    });
  }}
/>

<style>
  @keyframes slide-in-right {
    from { transform: translateX(100%); }
    to { transform: translateX(0); }
  }
  @keyframes slide-out-right {
    from { transform: translateX(0); }
    to { transform: translateX(100%); }
  }
  .animate-slide-in-right {
    animation: slide-in-right 0.25s ease-out forwards;
  }
  .animate-slide-out-right {
    animation: slide-out-right 0.25s ease-in forwards;
  }

  @keyframes conversation-turn-orbit {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  .conversation-turn-orbit {
    --turn-orbit-rgb: 56 189 248;
    --turn-orbit-tip-rgb: 125 211 252;
    --turn-orbit-shadow-rgb: 14 165 233;
    position: absolute;
    inset: 0;
    border-radius: 9999px;
    pointer-events: none;
    animation: conversation-turn-orbit 1.15s linear infinite;
    background: conic-gradient(
      from 0deg,
      rgb(var(--turn-orbit-rgb) / 0) 0deg,
      rgb(var(--turn-orbit-rgb) / 0.08) 210deg,
      rgb(var(--turn-orbit-rgb) / 0.46) 315deg,
      rgb(var(--turn-orbit-tip-rgb) / 0.95) 360deg
    );
    mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2px));
    -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 2px));
  }

  .conversation-turn-orbit--plan {
    --turn-orbit-rgb: 52 211 153;
    --turn-orbit-tip-rgb: 110 231 183;
    --turn-orbit-shadow-rgb: 16 185 129;
  }

  .conversation-turn-orbit--build {
    --turn-orbit-rgb: 251 146 60;
    --turn-orbit-tip-rgb: 252 211 77;
    --turn-orbit-shadow-rgb: 245 158 11;
  }

  .conversation-turn-orbit--amber {
    --turn-orbit-rgb: 251 191 36;
    --turn-orbit-tip-rgb: 252 211 77;
    --turn-orbit-shadow-rgb: 245 158 11;
  }

  .conversation-turn-orbit--rose {
    --turn-orbit-rgb: 251 113 133;
    --turn-orbit-tip-rgb: 253 164 175;
    --turn-orbit-shadow-rgb: 244 63 94;
  }

  .conversation-turn-orbit span {
    position: absolute;
    left: 50%;
    top: -1px;
    height: 0.5rem;
    width: 0.5rem;
    transform: translateX(-50%);
    border-radius: 9999px;
    background: rgb(var(--turn-orbit-rgb));
    box-shadow: 0 0 10px rgb(var(--turn-orbit-rgb) / 0.9), 0 0 18px rgb(var(--turn-orbit-shadow-rgb) / 0.45);
  }

  @media (prefers-reduced-motion: reduce) {
    .conversation-turn-orbit {
      animation: none;
      transform: rotate(45deg);
    }
  }
</style>
