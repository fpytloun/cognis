<script lang="ts">
  import { ListeningCues } from '$lib/utils/listening-cues';
  import { provideFeedbackContext } from '$lib/feedback/context';
  import { CommentWorkspace, provideCommentWorkspace } from '$lib/feedback/comment-workspace';
  import ChatHeaderMenu from '$lib/components/chat/ChatHeaderMenu.svelte';
  import type { IncludedFeedback, FeedbackSourceRef } from '$lib/feedback/types';
  import { provideChatNavigation } from '$lib/chat-v2/navigation';
  import { performManagedAction } from '$lib/chat-v2/managed-actions';
  import ManagedChannelBadge from '$lib/components/ManagedChannelBadge.svelte';
  import GitFork from 'lucide-svelte/icons/git-fork';
  import PinConversationButton from '$lib/components/chat/PinConversationButton.svelte';
  import { isWorkspaceReplica, openNativeConversationWindow } from '$lib/native/host';
  import { goto } from '$app/navigation';
  import { isResolvedApprovalRejection } from '$lib/approval-resolution';
  import { page } from '$app/state';
  import WaitingRefreshStatus from '$lib/components/WaitingRefreshStatus.svelte';
  import { WaitingActivity } from '$lib/complete-waiting';
  import {
    RefreshCooldown, SidebarRefreshController, overlaySidebarRows, updateSidebarRow, type RefreshAttempt,
  } from '$lib/sidebar-refresh';
  import { SidebarRefreshLifecycle } from '$lib/sidebar-refresh-lifecycle';
  import { onDestroy, onMount, setContext, tick, untrack } from 'svelte';
  import { get } from 'svelte/store';
  import { fade } from 'svelte/transition';
import ArrowLeft from 'lucide-svelte/icons/arrow-left';
import ArrowUp from 'lucide-svelte/icons/arrow-up';
import Square from 'lucide-svelte/icons/square';
import Check from 'lucide-svelte/icons/check';
import ChevronDown from 'lucide-svelte/icons/chevron-down';
import ChevronUp from 'lucide-svelte/icons/chevron-up';
import ChevronsLeft from 'lucide-svelte/icons/chevrons-left';
import ChevronsRight from 'lucide-svelte/icons/chevrons-right';
import Copy from 'lucide-svelte/icons/copy';
import ExternalLink from 'lucide-svelte/icons/external-link';
import Info from 'lucide-svelte/icons/info';
import Menu from 'lucide-svelte/icons/menu';
import Plus from 'lucide-svelte/icons/plus';
import RefreshCw from 'lucide-svelte/icons/refresh-cw';
import Search from 'lucide-svelte/icons/search';
  import MessagesSquare from 'lucide-svelte/icons/messages-square';
import Filter from 'lucide-svelte/icons/filter';
import Star from 'lucide-svelte/icons/star';
import X from 'lucide-svelte/icons/x';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import AttentionActionDialog from '$lib/components/attention/AttentionActionDialog.svelte';
  import DeferredView from '$lib/components/DeferredView.svelte';
  import AgentQuickProfileSwitcher from '$lib/components/chat/AgentQuickProfileSwitcher.svelte';
  import RateLimitProviderSwitch from '$lib/components/chat/RateLimitProviderSwitch.svelte';
  import {
    agentQuickProfileSelection,
    QUICK_PROFILE_RUNTIME_MAX_FAILURES,
    recordRuntimeForSession,
    shouldLoadRuntimeForSession,
    type RuntimeBySession
  } from '$lib/agent-quick-switch';
  import ChatSearchBar from '$lib/components/ChatSearchBar.svelte';
  import { findTarget } from '$lib/find';
  import SidebarConversationRow from '$lib/components/chat/SidebarConversationRow.svelte';
  import ConversationInfoDrawer from '$lib/components/ConversationInfoDrawer.svelte';
  import TimelineDiagnosticExport from '$lib/components/chat/TimelineDiagnosticExport.svelte';
   import InspectorOverview from '$lib/components/inspector/LazyInspectorOverview.svelte';
  import CredentialRequestForm from '$lib/components/CredentialRequestForm.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import PendingRequestPanel from '$lib/components/PendingRequestPanel.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import { appContactBindingId, shouldReplaceAgentSidebarConversation } from '$lib/app-contacts';
  import { preferredNewChatAgentId } from '$lib/chat/new-chat-agent';
  import { parseStoredMultiFilter } from '$lib/chat-page';
  import ProviderUnavailableNotices from '$lib/components/ProviderUnavailableNotices.svelte';
  import ChatV2TimelineList from '$lib/components/chat-v2/ChatV2TimelineList.svelte';
  import ChatV2TimelineItemRenderer from '$lib/components/chat-v2/ChatV2TimelineItemRenderer.svelte';
  import ChatV2Composer from '$lib/components/chat-v2/ChatV2Composer.svelte';
  import ConversationProjectControl from '$lib/components/projects/ConversationProjectControl.svelte';
  import SharedInspectorTabs from '$lib/components/inspector/SharedInspectorTabs.svelte';
  import ConversationUsagePanel from '$lib/components/inspector/ConversationUsagePanel.svelte';
  import ManagedConversationControls from '$lib/components/ManagedConversationControls.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';
  import SessionDetailsContent from '$lib/components/session/SessionDetailsContent.svelte';
  import { conversationYoloEnabled, sessionYoloEnabled } from '$lib/yolo-mode';
  import LazyWorkView from '$lib/components/work/LazyWorkView.svelte';
  import type { WorkInitialFocus } from '$lib/work/workFocus';
  import TimelineOngoingWorkDrawer from '$lib/components/timeline/TimelineOngoingWorkDrawer.svelte';
  import TimelineViewport from '$lib/components/timeline/TimelineViewport.svelte';
  import TodoProgressPopover from '$lib/components/TodoProgressPopover.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Popover from '$lib/components/ui/Popover.svelte';
  import PullToRefresh from '$lib/components/ui/PullToRefresh.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import { api, asApiError } from '$lib/api/client';
  import {
    dashboardConversationWaitingReason,
  } from '$lib/dashboard/dashboard';
  import { partitionSidebarConversationLanes } from '$lib/conversation-sidebar';
  import { mergeWaitingConversations, reconcileWaitingSnapshot } from '$lib/complete-waiting';
  import { ObservedConversationReadTracker } from '$lib/dashboard/conversation-read';
  import { hostAllowsObservation } from '$lib/native/host';
  import {
    activityOverviewIsVisible,
    getActivityOverview,
    getActivityOverviewEntry,
    invalidateActivityOverview,
    isActivityOverviewAbort,
    requestActivityOverview,
    visibleSnapshotOverview,
  } from '$lib/activityOverviewCache';
  import {
    getSessionInfo,
    mergeRuntimeSelection,
    newestRuntimeSelection,
    setSessionInfo,
    mergeSessionInfo,
    mergeSessionContextUsage,
    sessionInfoFromDetail,
    type SessionInfoData,
  } from '$lib/sessionInfoCache';
  import {
    acceptsSessionDiagnostics,
    diagnosticsForSession,
    mergeContextUsage,
    runtimeModelPresentation,
  } from '$lib/sessionDiagnostics';
  import {
    FocusedSessionDiagnosticsController,
    type FocusedSessionDiagnosticsState,
  } from '$lib/focusedSessionDiagnostics';
  import { FocusedSessionIdentityLoader } from '$lib/focusedSessionIdentity';
  import {
    buildConversationUrl,
    CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
    CHAT_USER_SCROLL_DELTA_THRESHOLD_PX,
    CHAT_TIMELINE_EXPAND_DOWN_THRESHOLD_PX,
    canonicalStateCanSettleDirectQuestion,
    conversationPendingSnapshotFlags,
    directQuestionFromPendingSummary,
    escalationFromPendingSummary,
    managedEscalationPending,
    managedInteractionVisibleInScope,
    mergeHydratedEscalation,
    conversationMatchesSidebarProjectionFilter,
    shouldInsertDirectlyLoadedConversation,
    conversationAttentionDotClass,
    conversationAttentionLabel,
    conversationAttentionTone,
    conversationActivityValue,
    conversationReadAcknowledgementAt,
    conversationShowsAttentionDot,
    conversationStatusFilterForConversation,
    conversationTurnModeTone,
    conversationUpdatedRowPatch,
    orderedConversationUpdatedRowPatch,
    chatV2RuntimeConversationPatch,
    distanceFromScrollBottom,
    getConversationRetryScope,
    groupConversationsByActivity,
     lastOpenedConversationEntry,
     managedConversationTurnState,
     mergeAuthoritativeSidebarConversation,
     mergeConversationPreservingActivity,
     reconcileConversationWithChatRuntime,
      mobileConversationStatusLabel,
      initialConversationFiltersOpen,
    mergeConversationRowPatch,
    mergeSidebarBackgroundWork,
    mergeSidebarConversationRows,
    removeSidebarConversationRow,
    getNextHistoryAfterSeq,
    isNearScrollBottom,
    isMissingSessionError,
    isCurrentConversationLoad,
    startCachedTimelineRefresh,
    isForeignSessionTimelineEvent,
    isLastOpenedConversationStorageKey,
    SidebarRevisionAdmission,
    SerialInvalidationCoalescer,
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
    reconcileLiveTailWindow,
    timelineWindowHasHiddenTail,
    shouldAutoLoadOlderForViewport,
    expandWindowUp,
    expandWindowUpPreservingLiveTail,
    expandWindowDown,
     reconcileTimelineMembership,
    clampWindow,
    freezeTailWindow,
    TIMELINE_WINDOW_TARGET_ROWS as TIMELINE_WINDOW_TARGET_ROWS_HELPER,
    TIMELINE_WINDOW_PAGE_ROWS as TIMELINE_WINDOW_PAGE_ROWS_HELPER,
    TIMELINE_WINDOW_MAX_ROWS as TIMELINE_WINDOW_MAX_ROWS_HELPER,
    TIMELINE_VIEWPORT_FILL_MAX_ATTEMPTS,
    type TimelineWindow,
    shouldAttemptStaleRuntimeRefresh,
    shouldApplyPendingNotificationRefresh,
    shouldApplyPendingNotificationConsumerRefresh,
    shouldApplySidebarProjectionRefresh,
    shouldDebounceConversationViewRefresh,
    shouldDebounceSidebarResync,
    shouldRecoverMissingConversationRow,
    shouldResetPendingDirectQuestionForm,
    isConversationSwitchStale,
    shouldApplyChatMutationResponse,
    shouldApplyChatSendFailureSideEffects,
    shouldApplyChatV2Recovery,
    shouldClearRecoverableRetry,
    settleWithTimeout,
    shouldReconcileAfterReconnect,
    ChatV2CanonicalRecoveryCoalescer,
    beginCanonicalTimelineAuthority,
    acceptCanonicalTimelineBase,
    canonicalTimelineBaseIsAccepted,
    transitionCanonicalTimelineAuthority,
    canonicalTimelineAuthorityIsReady,
    backfillInitialVisibleHistory,
    type CanonicalTimelineAuthority,
    nextControllerRecoveryDelayMs,
    shouldContinueControllerRecovery,
    shouldPreserveLiveTailOnResize,
    shouldRefreshForStaleRuntime,
    deriveChatV2ViewProjection,
    resolveOlderMessagesCursorAfterSnapshot,
    isRuntimeSnapshotOlderThanView,
    shouldAdoptConversationSessionId,
    shouldSuppressPreSessionSocketError,
    nextConversationLoadId,
    nextPollDelayMs,
    ChatV2ConversationLifecycle,
    type ChatModeTone,
    type ConversationStatusFilter,
    type PendingDirectQuestion,
    type DirectQuestionAck,
    buildDirectQuestionAck,
    shouldRestoreDirectQuestionOnSubmitFailure,
    sanitizeManagedQuestionOrigin,
    CHAT_STORAGE_KEYS,
    CHAT_PINNED_INSPECTOR_GAP,
    CHAT_PINNED_INSPECTOR_MIN_CHAT_WIDTH,
    conversationInspectorFits,
    canShowConversationLifecycleActions,
    reconcileRenderedOverviewSources,
    CONVERSATION_SWITCH_TIMEOUT_MS,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { edgeSwipe } from '$lib/actions/edgeSwipe';
  import { keyboardAvoidance } from '$lib/actions/keyboard-avoidance';
  import { scrollPersist } from '$lib/actions/scrollPersist';
  import {
    captureTimelineReadingAnchor, protectTimelineReadingWindow,
    timelineReadingScrollTop, timelinePagingDirection, type TimelineReadingAnchor,
  } from '$lib/timeline-reading-anchor';
  import { auth } from '$lib/stores/auth';
  import { confirmAction } from '$lib/stores/confirm';
  import { mobileNavOpen as mobileNavOpenStore, requestOpenMobileNav } from '$lib/stores/mobileNav';
  import {
    INSPECTOR_MIN_WIDTH,
    conversationInfoDrawer,
  } from '$lib/stores/conversationInfo.svelte';
  import { registerOverlay } from '$lib/stores/overlays';
  import { invalidateWorkScope } from '$lib/work/workViewState';
  import { canAttemptPwaAuxiliaryWindow, displayMode } from '$lib/stores/pwa';
  import { onTabReset } from '$lib/stores/tabReset';
   import { addToast } from '$lib/stores/toasts';
   import { loadUserPreferences, userPreferences } from '$lib/stores/userPreferences';
   import { hasConservativeTouchKeyboardFallback, viewportMetrics } from '$lib/stores/viewport';
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
   import { handleClipboardFilePaste, pastedFileFingerprint } from '$lib/clipboard';
  import {
    enableWebPush,
    hasDismissedWebPushPrompt,
    hasEnabledWebPush,
    isWebPushSupported,
    needsIosHomeScreenInstall,
    notifyIfHidden,
    permissionState,
    reconcileWebPushSubscription,
    setWebPushPromptDismissed,
    wantsWebPush
  } from '$lib/notifications';
  import { buildLinkedServiceUrl, openUrlInNewTab } from '$lib/config';
  import { providerNoticeOwnsSocketError } from '$lib/provider-notices';
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
    import { chatV2Api } from '$lib/chat-v2/api';
     import {
       dispatchChatComposerMessage,
       normalizeChatComposerInput,
     } from '$lib/chat-v2/composer-dispatch';
    import {
      isAutomaticContinuation,
      queuedMessageAccessibleLabel,
      queuedMessageLabel,
      visibleQueuedMessages
   } from '$lib/chat-v2/queue-presentation';
   import { conversationActivityState } from '$lib/conversation-activity';
   import { createClientPerformanceTiming } from '$lib/chat-v2/client-performance';
    import {
      shouldAwaitCanonicalUserMessage,
      shouldRenderOptimisticUserMessage
    } from '$lib/chat-v2/oversized-message';
  import { forkAssistantMessageContext, type ForkAssistantMessage } from '$lib/chat-message-actions';
   import { conversationTimelineScope, sessionTimelineScope, type ActivityOverviewResponse, type TimelineScope, type WorkCategory, type WorkstreamRef } from '$lib/chat-v2/types';
  import { createMemoryChatV2Outbox, createIndexedDbChatV2Outbox, type OutboxEntry } from '$lib/chat-v2/outbox';
   import { DocumentDrafts } from '$lib/offline/document-drafts';
   import { documentCoordination } from '$lib/document-coordination';
    import { ComposerSubmission } from '$lib/chat-v2/composer-submission';
    import { conversationComposer } from '$lib/chat-v2/conversation-composer';
    import { COMPOSER_FEEDBACK_EVENT, type ComposerFeedbackDetail } from '$lib/feedback/staging';
    import { sameSubmissionInput } from '$lib/chat-v2/submission-input';
    import { hydrateComposer, emptyComposerInput, selectLegacyInput, canSelectRecovery, type RecoveryScope, type LegacyComposerInput } from '$lib/chat-v2/composer-hydration';
    import LegacyComposerRecovery from '$lib/components/chat-v2/LegacyComposerRecovery.svelte';
    import { drainConversationOutbox, projectAdmissionFailure, sendOutboxEntry } from '$lib/chat-v2/outbox-send';
   const pageDataOwner = typeof window === 'undefined' ? null : documentCoordination().storageName('offline-cache');
  const liveComposer = typeof window === 'undefined' ? null : conversationComposer();
  const composerDrafts = liveComposer?.drafts ?? new DocumentDrafts();
  import {
    OutboxRetryScheduler,
    isRetryableSendError,
    mergeFailedSendIntoDraft,
  } from '$lib/chat-v2/outbox-retry';
  import {
    attachmentUploads,
    isLocalAttachment,
    toServerAttachment,
  } from '$lib/chat-v2/attachment-uploads';
  import { allDraftAttachmentLists } from '$lib/chat-v2/unsent-recovery';
  import { isOfflineError, offlineCache } from '$lib/offline/cache';
  import { recordPerf } from '$lib/perf-log';
  import { ChatV2Store } from '$lib/chat-v2/store.svelte';
  import { acquireTimeline, recoverTimeline, TimelineOwnershipError, peekTimeline, type TimelineController } from '$lib/chat-v2/timeline-controller.svelte';
  import { ConversationPreloadScheduler } from '$lib/chat-v2/preload-scheduler';
  import {
    isPinnedTransientNotice,
    isRenderableTimelineItem,
    selectActivePinnedTransientNotice,
    selectLatestTodoState,
    selectPendingInputToolCall,
  } from '$lib/chat-v2/selectors';
  import { selectFailedTurnRecovery } from '$lib/chat-v2/recovery';
  import RecoveryBanner from '$lib/components/RecoveryBanner.svelte';
  import type { ChatRealtimeFrame, ChatSnapshot, TimelineItem as ChatV2TimelineItem } from '$lib/chat-v2/types';
  import { incompleteTodos, visibleTodos as activeVisibleTodos } from '$lib/todos';
  import {
    activeRootSessionLineageIds,
    backgroundWorkItemIsRunning,
    currentCycleDelegations,
    directChildBackgroundWork,
    mergeCurrentCycleDelegations,
    overlayManagedConversationStatus,
    workstreamSessionIds,
  } from '$lib/ongoing-work';
  import {
     localPerformanceMetrics,
     mergeLatestPerformance,
     responsivenessBadgeOrNull
  } from '$lib/generation-performance';
  import {
    activityOverviewInvalidationScopeKeys,
    rootOverviewForConversation,
    canonicalWorkstreamSessionId,
    resolvedWorkSubtreeScope,
    structuralParentSessionId,
    treeSessionNavigation,
    traverseInspectorSession,
    workInvalidationTouchesTree,
    workstreamForSession,
  } from '$lib/inspectorTreeNavigation';
  import {
    backChildViewToRoot,
    childViewForWorkstream,
    childViewScope,
    childViewWorkstream,
    resolveChildWorkstreamIdentity,
    closeChildViewToRoot,
    controllerRootConversationId,
    enrichChildWorkstream,
    eventNeedsTreeRefresh,
    fallbackWorkstream,
    parentChildView,
    type ChildView,
  } from '$lib/childView';
  import {
    promoteRootOverview,
    RootOverviewRequestEpoch,
  } from '$lib/rootOverviewRequestEpoch';
  import {
    isTerminalSessionStatus,
    updateRuntimeActiveSessions,
  } from '$lib/session-status';
     import type { ActiveThinkingSnapshot, Agent, AgentDirectChat, AttachmentRef, AttentionActionDetail, AttentionActionSummary, BackgroundWorkItem, BackgroundWorkProjection, CognisWebSocketEvent, ContextUsage, Conversation, ConversationSearchMatch, ConversationStateEnvelope, ConversationTodoItem, Escalation, GenerationPerformanceSnapshot, MessageEvent, Notification, Project, QueuedMessage, QuestionSetAnswer, QuestionSetQuestion, QuestionSetReply, ResourceVisibility, Session, SidebarProjection } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';
  import { connectivity, isConnectivityError, onConnectivityRestored } from '$lib/connectivity';
  import { isNonFatalWebSocketBackpressureError } from '$lib/ws/errors';
  import {
    activityOverviewReadPresentation,
    beginActivityOverviewRead,
    cancelActivityOverviewRead,
    emptyActivityOverviewReadState,
    settleActivityOverviewRead,
  } from '$lib/work/activityOverviewState';
  import {
    TimelineViewportStateStore,
    type TimelineViewportAnchor,
    type TimelineViewportTransition,
  } from '$lib/timeline-viewport-state';

  let initializing = $state(true);
  let initialLoadTimedOut = $state(false);
  let switchingConversationId = $state<string | null>(null);
  let switchingConversation = $derived(switchingConversationId !== null);
  let initialConversationResolved = $state(false);
  let error = $state('');
  let historyError = $state('');
  let sessionsError = $state('');
  let conversations = $state<Conversation[]>([]);
  let waitingConversations = $state<Conversation[]>([]);
  let waitingLoadError = $state<string | null>(null);
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
  let conversationTitleSearchResults = $state<Conversation[]>([]);
  let conversationTitleSearchCursor = $state<string | null>(null);
  let conversationTitleSearchHasMore = $state(false);
  let conversationTitleSearchLoading = $state(false);
  let conversationTitleSearchError = $state('');
  let conversationTitleSearchRequestId = 0;
  let historySectionNow = $state(new Date());
  let expandedSearchSessionIds = $state<string[]>([]);
  let searchEnabled = $state(true);
  let isWindowMode = $derived(page.url.searchParams.get('window') === '1');
  let isStandalonePwa = $derived($displayMode === 'standalone');
  let canOpenAuxiliaryWindow = $derived(canAttemptPwaAuxiliaryWindow());
  let chatSearchOpen = $state(false);
  let chatSearchInput = $state<HTMLInputElement | null>(null);
  let chatSearchOpener: HTMLElement | null = null;
  let findOnlySearchConversationId = $state<string | null>(null);
  let conversationSidebar = $state<HTMLElement | null>(null);
  let chatSearchQuery = $state('');
  let chatSearchLoading = $state(false);
  let chatSearchResults = $state<ChatSearchResult[]>([]);
  let chatSearchSelectedIndex = $state(0);
  const selectedRemoteSearchMatch = $derived.by(() => {
    const result = chatSearchOpen ? chatSearchResults[chatSearchSelectedIndex] : null;
    return result?.source === 'server' && !renderableVisibleItems.some(item => item.id === result.targetId)
      ? result.server.match : null;
  });
  let seededChatSearchRan = $state(false);
  let seededChatSearchSession = $state<string | null>(null);
  let seededChatSearchRef = $state<string | null>(null);
  let lastSeededSearchKey = '';
  let lastChatSearchConversationId = '';
  let agents = $state<Agent[]>([]);
  let agentDirectChats = $state<AgentDirectChat[]>([]);
  let backgroundWork = $state<BackgroundWorkProjection>({
    items: [],
    active_count: 0,
    truncated: false,
    generated_at: new Date(0).toISOString(),
  });
  let sidebarProjectionLoaded = $state(false);
  let sidebarProjectionFailed = $state(false);
  let currentConversation = $state<Conversation | null>(null);
  let sessions = $state<Session[]>([]);
  type ChatTodo = ConversationTodoItem & { priority: string };
  let conversationTodoSnapshots = $state<Record<string, ChatTodo[]>>({});
  let conversationSubloadsLoading = $state(false);
  let cachedConversationRefreshing = $state(false);
  // The refresh banner is noise when the cached view is recent (preloaded or
  // just seen) or the refresh is quick; show it only for stale views whose
  // refresh is actually slow.
  // A view with content is already readable, so its quiet refresh only
  // earns a banner when genuinely slow; an empty cached view shows it soon.
  const CACHED_REFRESH_BANNER_DELAY_MS = 2_500;
  const EMPTY_CACHED_VIEW_BANNER_DELAY_MS = 300;
  const CACHED_VIEW_FRESH_MS = 30_000;
  let cachedViewFetchedAt = 0;
  let cachedViewHasContent = $state(false);
  let cachedRefreshBannerVisible = $state(false);
  let cachedRefreshBannerTimer: ReturnType<typeof setTimeout> | null = null;
  $effect(() => {
    const refreshing = cachedConversationRefreshing;
    const hasContent = cachedViewHasContent;
    if (cachedRefreshBannerTimer !== null) clearTimeout(cachedRefreshBannerTimer);
    cachedRefreshBannerTimer = null;
    if (!refreshing) {
      cachedRefreshBannerVisible = false;
      return;
    }
    if (hasContent && Date.now() - cachedViewFetchedAt < CACHED_VIEW_FRESH_MS) return;
    cachedRefreshBannerTimer = setTimeout(() => {
      cachedRefreshBannerTimer = null;
      if (cachedConversationRefreshing) cachedRefreshBannerVisible = true;
    }, hasContent ? CACHED_REFRESH_BANNER_DELAY_MS : EMPTY_CACHED_VIEW_BANNER_DELAY_MS);
  });
  let canonicalTimelineAuthority = $state<CanonicalTimelineAuthority>({
    conversationId: null,
    routeGeneration: 0,
    status: 'loading',
    baseAccepted: false,
  });
  let canonicalTimelineApplyRevision = 0;
  let composer = $state('');
  let composerElement = $state<HTMLTextAreaElement | null>(null);
   let composerAttachments = $state<AttachmentRef[]>([]);
    let composerRevision = 0;
    let composerInputRevision = $state<string>(crypto.randomUUID());
    let composerLegacyRecovery = $state(false);
    let composerLegacyCandidates = $state<LegacyComposerInput[]>([]);
    let composerRecoveryScope = $state.raw<RecoveryScope | null>(null);
    let composerHydratedGeneration = -1;
    let composerInputMode = $state<'default' | 'plan' | 'build' | null>(null);
    let includedFeedback = $state<IncludedFeedback | null>(null);
    provideFeedbackContext({ get context(): import('$lib/feedback/types').FeedbackContext {
      const id = conversationIdFromRoute();
      return id ? { kind: 'chat', conversation_id: id } : { kind: 'standalone' };
    } });
    const commentWorkspace = new CommentWorkspace(() => !!currentConversation && !isReadOnly(currentConversation));
    const commentState = commentWorkspace.state;
    provideCommentWorkspace(commentWorkspace);
    $effect(() => { commentWorkspace.reset(conversationIdFromRoute() ?? ''); });
    onMount(() => documentCoordination().onInvalidated(() => commentWorkspace.dispose()));
    onDestroy(() => commentWorkspace.dispose());
    function editComposerInput(): void { composerRevision++; composerInputRevision = crypto.randomUUID(); }
    function compositeComposerInput() {
      return { input_revision: composerInputRevision, text: composer,
        attachments: $state.snapshot(composerAttachments) as AttachmentRef[], chat_mode: composerInputMode, feedback: includedFeedback };
    }
   let composerAlive = true;
   let preparingComposer = false;
   let latestComposerSubmissionId: string | null = null;
   onDestroy(() => { composerAlive = false; });
  let managedInstructionOpen = $state(false);
  let managedInstruction = $state('');
  let managedActionBusy = $state<string | null>(null);
  let managedActionError = $state('');
  let recentPastedFileFingerprints = new Map<string, number>();
  let conversationModeOpen = $state(false);
  let conversationCues = $state<ListeningCues>();
  function openConversationMode(): void {
    conversationCues?.dispose();
    conversationCues = new ListeningCues();
    conversationCues.unlock();
    conversationModeOpen = true;
  }
  function closeConversationMode(): void {
    conversationModeOpen = false;
    conversationCues?.dispose();
    conversationCues = undefined;
  }
  $effect(() => () => { conversationCues?.dispose(); });
  let voiceTranscribing = $state(false);

  // Composer drafts persist across app restarts (per conversation) via
  // localStorage. This protects PWA users when the browser evicts or
  // reloads the web chat while a message is still being drafted.
  const DRAFT_PREFIX = 'cognis-chat-draft:';
  let currentDraftKey = $state<string | null>(null);
  let draftSaveTimer: number | null = null;

  function readDraft(key: string): string {
    if (typeof localStorage === 'undefined') return '';
    try {
      if (liveComposer) return liveComposer.hydrate(key.slice(DRAFT_PREFIX.length)).input.text;
      const saved = composerDrafts.readComposite(key);
      return saved ? (saved.consumed ? '' : saved.text) : composerDrafts.read(key) ?? '';
    } catch {
      return '';
    }
  }

  function writeDraft(key: string, value: string): void {
    if (typeof localStorage === 'undefined') return;
    try {
      if (!composerLegacyRecovery) liveComposer?.update(key.slice(DRAFT_PREFIX.length),
        key === currentDraftKey ? { ...compositeComposerInput(), text: value }
          : { ...(liveComposer?.hydrate(key.slice(DRAFT_PREFIX.length)).input ?? emptyComposerInput()),
            input_revision: crypto.randomUUID(), text: value });
    } catch {
      // non-fatal
    }
  }

  // Mobile browsers can freeze or kill a backgrounded PWA without further
  // events, so persist the in-flight debounced draft as soon as the page is
  // hidden instead of waiting for the debounce timer.
  function flushPendingDraft(): void {
    if (draftSaveTimer === null || typeof window === 'undefined') return;
    window.clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
    if (currentDraftKey) writeDraft(currentDraftKey, composer);
  }
  let showDropZone = $state(false);
  let dragCounter = 0;
  const PASTE_DUPLICATE_SUPPRESSION_MS = 2000;
  const CONTEXT_DONUT_RADIUS = 15.5;
  const CONTEXT_DONUT_CIRCUMFERENCE = 2 * Math.PI * CONTEXT_DONUT_RADIUS;
  let selectedAgentIds = $state<string[]>([]);
  // Project filter for the sidebar ('' = all projects) and the caller's projects.
  let selectedProjectId = $state('');
  let includeSharedChats = $state(true);
  let projects = $state<Project[]>([]);
  let projectChangeBusy = $state(false);
  let selectedConversationStatus = $state<ConversationStatusFilter>(parseConversationStatusFilter(page.url.searchParams.get('status')));
  let starringConversationId = $state<string | null>(null);
  let archivingConversation = $state(false);
  let deletingConversation = $state(false);
  let mobileListOpen = $state(false);
  let mobileListOverlayCleanup: (() => void) | null = null;
  let conversationFiltersOpen = $state(false);
  let conversationSearchOpen = $state(false);
  let conversationListEndVisible = $state(false);
  let lastAutomaticConversationPageKey = $state<string | null>(null);
  let agentFilterDropdownOpen = $state(false);
  let channelFilterDropdownOpen = $state(false);
  let selectedSidebarAttentionId = $state<string | null>(null);
  let selectedSidebarAttentionTitle = $state('');
  let selectedSidebarAttentionOrigin = $state<HTMLElement | null>(null);
  let headerInfoOpen = $derived(conversationInfoDrawer.open);
  let headerInfoMode = $derived(conversationInfoDrawer.mode);
  let headerInfoTrigger: HTMLElement | null = null;
  let headerInfoConversationId: string | null = null;
  let focusedSessionId = $state<string | null>(null);
  let childView = $state<ChildView | null>(null);
  let parentSurfaceWasHidden = false;
  let parentSurfaceScrollTop = 0;
  let parentSurfaceAnchor: ReturnType<typeof captureTimelineAnchor> = null;
  let parentSurfaceTransitionGeneration = 0;
  let selectedChildWorkstream = $state<WorkstreamRef | null>(null);
  let runtimeActiveChildSessionIds = $state<Set<string>>(new Set());
  let runtimeActiveSessionIds = $derived([...runtimeActiveChildSessionIds]);
  let focusedWorkCategory = $state<WorkCategory>('files');
  let focusedWorkInitialFocus = $state<WorkInitialFocus | null>(null);
  let activityOverview = $state<ActivityOverviewResponse | null>(null);
  let rootActivityOverview = $state<ActivityOverviewResponse | null>(null);
  let rootOverviewAbortController: AbortController | null = null;
  let activityOverviewReadState = $state(emptyActivityOverviewReadState());
  let activityOverviewAbortController: AbortController | null = null;
  let activityOverviewGeneration = 0;
  let treeRootConversationId = $derived(
    currentConversation ? controllerRootConversationId(currentConversation) : null,
  );
  let scopedRootActivityOverview = $derived(rootOverviewForConversation(
    rootActivityOverview,
    treeRootConversationId,
  ) ?? rootOverviewForConversation(
    rootActivityOverview,
    currentConversation?.conversation_id,
  ));
  let explicitFocusedIdentity = $state<WorkstreamRef | null>(null);
  let requestedInspectorScope = $derived(
    currentConversation
      ? childView
        ? childViewScope(childView)
        : resolvedWorkSubtreeScope(
            currentConversation.conversation_id,
            focusedSessionId,
            scopedRootActivityOverview?.workstreams ?? [],
            explicitFocusedIdentity,
          )
      : null,
  );
  let scopedActivityOverview = $derived(
    activityOverview?.scope.key === requestedInspectorScope?.key ? activityOverview : null,
  );
  let renderedActivityOverview = $derived(
    requestedInspectorScope?.key === (
      treeRootConversationId ? conversationTimelineScope(treeRootConversationId).key : null
    )
      ? scopedRootActivityOverview
      : scopedActivityOverview
        ? {
            ...scopedActivityOverview,
            workstreams: scopedRootActivityOverview?.workstreams
              ?? scopedActivityOverview.workstreams,
          }
        : null,
  );
  let overviewReadPresentation = $derived.by(() => {
    const focusedScopeKey = inspectorScope?.key ?? null;
    const rootScopeKey = treeRootConversationId
      ? conversationTimelineScope(treeRootConversationId).key
      : null;
    if (!focusedScopeKey || !rootScopeKey) {
      return { loading: false, refreshing: false, ready: false, error: null };
    }
    const focusedHasData = scopedActivityOverview?.scope.key === focusedScopeKey;
    const rootHasData = Boolean(scopedRootActivityOverview);
    return activityOverviewReadPresentation(
      activityOverviewReadState,
      focusedScopeKey === rootScopeKey
        ? [{ section: 'focused', scopeKey: focusedScopeKey, hasData: focusedHasData }]
        : [
            { section: 'root', scopeKey: rootScopeKey, hasData: rootHasData },
            { section: 'focused', scopeKey: focusedScopeKey, hasData: focusedHasData },
          ],
    );
  });
  let directManagedWorkstream = $derived(
    currentConversation?.managed_agent
      ? renderedActivityOverview?.workstreams.find(
          (node) => node.conversation_id === currentConversation?.conversation_id,
        ) ?? null
      : null,
  );
  let focusedWorkstream = $derived(focusedSessionId
    ? workstreamForSession(renderedActivityOverview?.workstreams ?? [], focusedSessionId)
      ?? (explicitFocusedIdentity?.session_id === focusedSessionId ? explicitFocusedIdentity : null)
    : directManagedWorkstream ?? renderedActivityOverview?.workstreams.find((node) => node.current) ?? null);
  let canonicalFocusedSessionId = $derived(
    focusedSessionId ? focusedWorkstream?.session_id ?? focusedSessionId : null
  );
  let treeFocusedSessionId = $derived(
    canonicalFocusedSessionId ?? directManagedWorkstream?.session_id ?? null,
  );
  let deepLinkConsumedFor: string | null = null;
  let chatShellElement = $state<HTMLElement | null>(null);
  let chatShellWidth = $state(0);
  let isTaskControlMode = $derived(page.url.searchParams.get('taskControl') === '1');
  function requestTaskAgentDockMinimize(): void {
    if (!isTaskControlMode || window.parent === window) return;
    window.parent.postMessage({ type: 'cognis:task-agent-dock:minimize' }, window.location.origin);
  }
  function taskControlFocusableElements(): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), iframe, [tabindex]:not([tabindex="-1"])'
    )).filter((element) => element.getClientRects().length > 0 && element.getAttribute('aria-hidden') !== 'true');
  }

  function handleTaskControlFocusBoundary(event: KeyboardEvent): void {
    if (!isTaskControlMode || event.key !== 'Tab' || window.parent === window) return;
    const elements = taskControlFocusableElements();
    const active = document.activeElement;
    const atBoundary = event.shiftKey ? active === elements[0] : active === elements.at(-1);
    if (!atBoundary) return;
    event.preventDefault();
    window.parent.postMessage({
      type: 'cognis:task-agent-dock:focus-boundary',
      direction: event.shiftKey ? 'backward' : 'forward'
    }, window.location.origin);
  }
  $effect(() => {
    const requestedView = page.url.searchParams.get('view');
    const loadedConversationId = currentConversation?.conversation_id;
    if (requestedView !== 'work' || !loadedConversationId || deepLinkConsumedFor === loadedConversationId) return;
    deepLinkConsumedFor = loadedConversationId;
    cancelActivityOverviewDemand();
    conversationInfoDrawer.mode = 'work';
    conversationInfoDrawer.setOpen(true);
  });
  $effect(() => {
    conversationInfoDrawer.hydrate(conversationInspectorFits(window.innerWidth));
  });
  $effect(() => {
    if (!chatShellElement || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => {
      chatShellWidth = entry?.contentRect.width ?? 0;
    });
    observer.observe(chatShellElement);
    return () => observer.disconnect();
  });
   let queuedCount = $state(0);
  let queuedMessages = $state<QueuedMessage[]>([]);
  let queueBusyId = $state<string | null>(null);
  let queueExpandedIds = $state<string[]>([]);
  let queueEditingId = $state<string | null>(null);
  let queueEditContent = $state('');
  let queueEditError = $state('');
  let chatV2Store = $state.raw(new ChatV2Store());
  let timelineController: TimelineController | null = null;
  let releaseTimeline: (() => void) | null = null;
  let stopTimelineOwnership: (() => void) | null = null;
  let stopTimelineMembership: (() => void) | null = null;
  let timelineMembershipRevision = 0;
  let timelineIdentityRequired = $state(false);
  let timelineIdentityError = $state('');
  let recoveringTimelineOwner = $state(false);
  let timelineOwnerRecovery: AbortController | null = null;
  let timelineLoadAbort = new AbortController();
  let pageAlive = true;
  onDestroy(() => {
    pageAlive = false;
    timelineViewportStates.invalidate();
    timelineLoadAbort.abort();
    timelineOwnerRecovery?.abort();
    stopTimelineOwnership?.();
    stopTimelineMembership?.();
    releaseTimeline?.();
  });

  function bindTimeline(conversationId: string): TimelineController {
    if (timelineIdentityRequired) throw new TimelineOwnershipError();
    if (timelineController?.scope.key === conversationTimelineScope(conversationId).key && timelineController.current) {
      chatV2Store = timelineController.store;
      return timelineController;
    }
    timelineLoadAbort.abort();
    timelineLoadAbort = new AbortController();
    releaseTimeline?.();
    stopTimelineOwnership?.();
    stopTimelineMembership?.();
    const acquired = acquireTimeline(conversationTimelineScope(conversationId));
    adoptTimeline(acquired);
    return acquired.controller;
  }

  function adoptTimeline(acquired: ReturnType<typeof acquireTimeline>): void {
    tailWindowRows = TIMELINE_WINDOW_TARGET_ROWS;
    stopTimelineMembership?.();
    timelineViewportStates.invalidateWindow();
    timelineController = acquired.controller;
    releaseTimeline = acquired.release;
    chatV2Store = timelineController.store;
    const current = acquired.controller;
    const attachedStore = current.store;
    let attached = true;
    const stop = attachedStore.observeMembership((before, after) => {
      if (!attached || !pageAlive || !current.current || current !== timelineController
        || currentConversation?.conversation_id !== current.scope.conversation_id
        || !timelineViewportStates.hasRetainedWindow(current.scope.key, conversationLoadRequestId)) return;
      const revision = ++timelineMembershipRevision;
      const anchor = userScrolledUp && !childView && timelineInitialPositionSettled ? readingAnchorForMutation() : null;
      const renderable = (items: readonly ChatV2TimelineItem[]) => items
        .filter(isRenderableTimelineItem).filter(item => !isPinnedTransientNotice(item));
      const beforeItems = renderable(before);
      const afterItems = renderable(after);
      const window = userScrolledUp
        ? reconcileTimelineMembership(timelineWindow, beforeItems, afterItems, loadingOlderMessages)
        : loadingOlderMessages
          ? { start: Math.max(0, afterItems.length - TIMELINE_WINDOW_MAX_ROWS * 3), end: null }
          : timelineWindow;
      return () => {
        if (!userScrolledUp && loadingOlderMessages) {
          tailWindowRows = Math.min(TIMELINE_WINDOW_MAX_ROWS * 3, afterItems.length);
        }
        lastRenderableVisibleCount = afterItems.length;
        if (attached && current.current && revision === timelineMembershipRevision) {
          void applyAnchoredTimelineWindow(window, anchor);
        }
      };
    });
    stopTimelineMembership = () => { attached = false; stop(); };
    stopTimelineOwnership = timelineController.onInvalidated(caught => {
      if (!pageAlive || timelineController !== acquired.controller) return;
      requireTimelineIdentity(caught);
    });
  }

  function requireTimelineIdentity(caught: TimelineOwnershipError): void {
      if (timelineIdentityRequired) return;
      timelineIdentityRequired = true;
      timelineIdentityError = caught.message;
      ++conversationLoadRequestId;
      timelineLoadAbort.abort(caught);
      olderMessagesAbortController?.abort(caught);
      timelineOwnerRecovery?.abort(caught);
      chatV2Lifecycle.release();
      if (activeConversationId) wsClient.unsubscribeConversation(activeConversationId);
      unsubscribeWs?.();
      unsubscribeWs = null;
      conversationPreloader.dispose();
      clearControllerRecovery();
      chatV2OutboxRetry.cancelAll();
      for (const timer of chatV2CanonicalRecoveryTimers.values()) clearTimeout(timer);
      chatV2CanonicalRecoveryTimers.clear();
      chatV2CanonicalRecoveryCoalescer.clear();
      conversationViewCache.clear();
      clearConversationViewState();
      currentConversation = null;
      initializing = false;
      initialConversationResolved = true;
      conversationSubloadsLoading = false;
      cachedConversationRefreshing = false;
      resetConversationSwitch();
      releaseTimeline?.();
  }

  async function recoverTimelineOwner(): Promise<void> {
    const current = timelineController;
    if (recoveringTimelineOwner) return;
    const conversationId = conversationIdFromRoute();
    if (!conversationId) return;
    const generation = conversationLoadRequestId;
    const controller = new AbortController();
    timelineOwnerRecovery = controller;
    recoveringTimelineOwner = true;
    try {
      const acquired = await recoverTimeline(conversationTimelineScope(conversationId), chatV2Api, controller.signal, current?.owner ?? pageDataOwner);
      if (!pageAlive || controller.signal.aborted || generation !== conversationLoadRequestId
        || conversationId !== conversationIdFromRoute()) {
        acquired.release();
        return;
      }
      stopTimelineOwnership?.();
      releaseTimeline?.();
      timelineLoadAbort = new AbortController();
      adoptTimeline(acquired);
      timelineIdentityRequired = false;
      timelineIdentityError = '';
      activeConversationId = '';
      conversationPreloader = new ConversationPreloadScheduler(preloadConversationView);
      unsubscribeWs = wsClient.subscribe(handleSocketEvent);
      await openConversation(conversationId);
    } catch (caught) {
      if (!pageAlive || controller.signal.aborted || generation !== conversationLoadRequestId) return;
      timelineIdentityError = caught instanceof Error ? caught.message : new TimelineOwnershipError().message;
    } finally {
      if (timelineOwnerRecovery === controller) timelineOwnerRecovery = null;
      if (pageAlive) recoveringTimelineOwner = false;
    }
  }
  const chatV2Lifecycle = new ChatV2ConversationLifecycle(wsClient);
  const focusedSessionIdentityLoader = new FocusedSessionIdentityLoader(
    (sessionId, signal) => api.sessions.detail(sessionId, { signal }),
  );
  onDestroy(() => focusedSessionIdentityLoader.dispose());
  let browserVisible = $state(false);
  let focusedDiagnostics = $state<FocusedSessionDiagnosticsState>({
    sessionId: null,
    contextUsage: null,
    freshness: 'unavailable',
  });
  const focusedDiagnosticsController = new FocusedSessionDiagnosticsController(
    wsClient,
    (scope, options) => chatV2Api.snapshot(scope, options),
    (state) => {
      focusedDiagnostics = state;
    },
    () => loadVisibleActivityOverview(true),
  );
  onDestroy(() => focusedDiagnosticsController.dispose());
  $effect(() => {
    const sessionId = focusedWorkstream?.session_id ?? null;
    const scope = sessionId
      ? sessionTimelineScope(sessionId, focusedWorkstream?.conversation_id ?? null)
      : null;
    const visible = browserVisible
      && conversationInfoDrawer.open
      && (headerInfoMode === 'overview' || headerInfoMode === 'context');
    focusedDiagnosticsController.select(scope, focusedWorkstream, visible);
  });
  const chatV2Outbox = typeof indexedDB === 'undefined'
    ? createMemoryChatV2Outbox()
    : createIndexedDbChatV2Outbox();
  let chatV2OutboxReady = false;
  let chatV2OutboxDraining = false;
  let chatV2PendingDrain: (() => Promise<void>) | null = null;
  const chatV2OutboxRetry = new OutboxRetryScheduler();
  onDestroy(() => {
    chatV2PendingDrain = null;
    chatV2OutboxRetry.cancelAll();
  });

  const TIMELINE_WINDOW_TARGET_ROWS = TIMELINE_WINDOW_TARGET_ROWS_HELPER;
  const TIMELINE_WINDOW_PAGE_ROWS = TIMELINE_WINDOW_PAGE_ROWS_HELPER;
  const TIMELINE_WINDOW_MAX_ROWS = TIMELINE_WINDOW_MAX_ROWS_HELPER;
  // Single window object over renderable rows. `start` is the first rendered
  // index; `end` null means "render to the tail" (live). Kept in sync with the
  // pure helpers in chat-page.ts so the logic is unit-testable.
  let timelineWindow = $state<TimelineWindow>({ start: 0, end: null });
  let tailWindowRows = TIMELINE_WINDOW_TARGET_ROWS;
  let timelineWindowRevision = 0;
  let timelineWindowPending = false;
  let timelineScrollWriteRevision = 0;
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
  const sidebarRevisionAdmission = new SidebarRevisionAdmission();
  // Realtime sidebar upserts patch rows immediately; this REST reconciliation
  // is the safety net. A running turn bumps the sidebar revision about once a
  // second and every revision change makes the server return a full
  // projection (~50 KB), so space reconciliations out instead of fetching one
  // per frame: that churn was saturating mobile links and delaying switches.
  const SIDEBAR_RECONCILIATION_MIN_INTERVAL_MS = 4_000;
  const sidebarCooldown = new RefreshCooldown();
  const sidebarReconciliation = new SidebarRefreshController(
    async (attempt) => {
      if (attempt.scope !== sidebarProjectionCacheKey()) {
        scheduleSidebarReconciliation('changed-sidebar-scope');
        return;
      }
      sidebarRevisionAdmission.selectScope(attempt.scope);
      if (attempt.full) await performSidebarLoad({ signal: attempt.signal });
      else await performSidebarResync('sidebar-invalidation', {
        force: true, recoveryAttempt: true, signal: attempt.signal,
      });
    }, sidebarCooldown, SIDEBAR_RECONCILIATION_MIN_INTERVAL_MS,
  );
  const waitingReconciliation = new SidebarRefreshController(
    performWaitingRefresh, sidebarCooldown, SIDEBAR_RECONCILIATION_MIN_INTERVAL_MS,
  );
  const sidebarRefreshLifecycle = new SidebarRefreshLifecycle(
    [sidebarReconciliation, waitingReconciliation], sidebarProjectionCacheKey,
  );
  onMount(() => sidebarRefreshLifecycle.start());
  $effect(() => {
    const scope = sidebarProjectionCacheKey();
    untrack(() => sidebarRefreshLifecycle.sync(scope));
  });
  const waitingActivity = new WaitingActivity();
  let waitingSnapshotScope = $state<string | null>(null);
  let sidebarSnapshotScope: string | null = null;
  const waitingSnapshotCache = new Map<string, Conversation[]>();
  let sidebarProjectionRefreshEpoch = 0;
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
  const CONVERSATION_VIEW_CACHE_LIMIT = 20;
  const SIDEBAR_PROJECTION_CACHE_LIMIT = 8;
  const sidebarProjectionCache = new Map<string, SidebarProjection>();
  const escalationTimeoutSeconds = 300;
  let escalations = $state<Escalation[]>([]);
  let selectedManagedChildConversationId = $derived(
    childView?.kind === 'managed' ? childView.conversationId : null,
  );
  let lastManagedInteractionScopeKey = $state<string | null>(null);
  let visibleEscalations = $derived(
    escalations.filter((item) => managedInteractionVisibleInScope(
      item.managed_origin_conversation_id,
      selectedManagedChildConversationId,
    )),
  );
  $effect(() => {
    const conversationId = currentConversation?.conversation_id ?? null;
    const scopeKey = conversationId
      ? `${conversationId}:${selectedManagedChildConversationId ?? 'parent'}`
      : null;
    if (
      !conversationId
      || !scopeKey
      || scopeKey === lastManagedInteractionScopeKey
    ) return;
    lastManagedInteractionScopeKey = scopeKey;
    escalationSettledAck = null;
    directQuestionAck = null;
    beginPendingNotificationRefresh(conversationId);
    void refreshPendingDirectQuestion();
  });
  let escalationBusyCallId = $state<string | null>(null);
  let escalationResolutionPending = $state<Escalation | null>(null);
  // Local, decision-correct acknowledgement shown immediately after a
  // successful REST resolve, before any WebSocket/state confirmation
  // arrives. Cleared once a newer escalation takes the slot or the
  // conversation-level escalation state resets.
  let escalationSettledAck = $state<{ callId: string; toolName: string | null; decision: 'approve' | 'deny' } | null>(null);
  // Call IDs resolved locally via REST success but not yet confirmed
  // removed by the backend (WebSocket ack or canonical state refresh).
  // Used to keep a settled escalation from being resurrected by a
  // slightly stale push frame, catch-up refresh, or state hydration.
  let locallySettledEscalationCallIds = $state<Set<string>>(new Set());
  let escalationError = $state('');
  let criticalEventStoreError = $state('');
  let controllerRecoveryPending = $state(false);
  let controllerRecoveryTimer: number | null = null;
  let controllerRecoveryAttempts = $state(0);
  let controllerRecoveryConversationId: string | null = null;
  // Notice precedence (see $lib/connectivity): while the connection is
  // degraded the global ConnectionBanner is the only connectivity notice, so
  // this page's history/session/escalation/refresh banners stay hidden. When
  // the connection is restored, failed loads are retried first and their
  // banners stay hidden until that retry settles; only errors that persist
  // with a healthy connection are shown. Critical event-store failures are
  // always shown.
  let connectivityRetryPending = $state(false);
  const connectivityOwnsNotices = $derived($connectivity.degraded || connectivityRetryPending);

  async function retryLoadsAfterConnectivityRestored(): Promise<void> {
    if (!currentConversation) {
      const routeConversationId = conversationIdFromRoute();
      if (routeConversationId && (initialLoadTimedOut || error)) void openConversation(routeConversationId);
      return;
    }
    const retrySubloads = Boolean(historyError || sessionsError);
    const retryEscalations = Boolean(escalationError);
    if (!retrySubloads && !retryEscalations) return;
    connectivityRetryPending = true;
    try {
      if (retrySubloads) await retryConversationSubloads();
      if (retryEscalations) await refreshEscalations();
    } finally {
      connectivityRetryPending = false;
    }
  }

  $effect(() => onConnectivityRestored(() => { void retryLoadsAfterConnectivityRestored(); }));
  let escalationCountdownTimer: number | null = null;
  let notificationRefreshTimer: number | null = null;
  let pushPromptDismissed = $state(hasDismissedWebPushPrompt());
  let pushSubscriptionKnownEnabled = $state(hasEnabledWebPush());
  let pushPromptBusy = $state(false);
  let pushPromptError = $state('');
  let pushDeliveryError = $state('');
  let awaitingAssistantStart = $state(false);
  let turnInProgress = $state(false);
  let cancelTurnPendingConversationId = $state<string | null>(null);
  let cancelTurnPending = $derived(
    currentConversation?.conversation_id === cancelTurnPendingConversationId
  );
  let retryWaitingTurn = $derived(
    chatV2Store.snapshot.runtime?.active_turn?.status === 'waiting'
      && Boolean(chatV2Store.snapshot.runtime?.active_turn?.retry_at)
      ? chatV2Store.snapshot.runtime.active_turn
      : null
  );
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
  let newChatAgentProfileMode = $state<'auto' | 'default' | null>(null);
  let newChatProjectId = $state('');
  let newChatCreating = $state(false);
  let newChatError = $state('');
  let newChatHandoff = $state<import('$lib/chat-v2/types').HandoffTimelineItem | null>(null);
  let newChatHandoffSource = $state('');
  let newChatCreated = $state<Conversation | null>(null);
  let editingTitle = $state(false);
  let editTitleValue = $state('');
  let titleSuggestionLoading = $state(false);
  let ignoreNextTitleBlur = $state(false);
  let sessionIdCopied = $state(false);
  let showAgentProfile = $state<'header' | 'sidebar' | null>(null);
  let quickProfileBusy = $state(false);
  let composerAdmissionBusy = $state(false);
  // The canonical snapshot omits runtime override sources, and the info-drawer
  // loader (loadSessionInfo) is invalidated by route lifecycle effects. Keep the
  // quick switcher's runtime selection per session so it survives navigation
  // (A -> B -> A) and fetch it independently of the drawer.
  type RuntimeSelection = import('$lib/types/api').RuntimeSelection;
  let quickProfileRuntimeBySession = $state<RuntimeBySession>({});
  const quickProfileRuntimeInFlight = new Set<string>();
  const quickProfileRuntimeFailures = new Map<string, number>();

  function recordQuickProfileRuntime(sid: string, selection: RuntimeSelection): void {
    const next = recordRuntimeForSession(quickProfileRuntimeBySession, sid, selection);
    if (next !== quickProfileRuntimeBySession) quickProfileRuntimeBySession = next;
  }

  // Every runtime update for the active session (command responses, drawer
  // loads, this loader) flows through latestRuntimeSelection; mirror it.
  $effect(() => {
    const latest = latestRuntimeSelection;
    if (latest) untrack(() => recordQuickProfileRuntime(latest.sessionId, latest.selection));
  });

  async function loadQuickProfileRuntime(sid: string): Promise<void> {
    quickProfileRuntimeInFlight.add(sid);
    try {
      const detail = await api.sessions.intarisDetail(sid);
      // Session-keyed storage is valid even if the user navigated away.
      if (detail.runtime_selection) recordQuickProfileRuntime(sid, detail.runtime_selection);
      else quickProfileRuntimeFailures.set(sid, QUICK_PROFILE_RUNTIME_MAX_FAILURES);
    } catch {
      // Indicator is best-effort; retry a bounded number of times.
      quickProfileRuntimeFailures.set(sid, (quickProfileRuntimeFailures.get(sid) ?? 0) + 1);
    } finally {
      quickProfileRuntimeInFlight.delete(sid);
    }
  }

  $effect(() => {
    const conversation = currentConversation;
    const sid = conversation?.active_session_id;
    if (!conversation || !sid || childView) return;
    const agent = conversationAgent(conversation);
    if (!agent || agentQuickProfileSelection(agent).options.length === 0) return;
    if (!shouldLoadRuntimeForSession(
      quickProfileRuntimeBySession, sid, quickProfileRuntimeInFlight, quickProfileRuntimeFailures
    )) return;
    void untrack(() => loadQuickProfileRuntime(sid));
  });

  async function selectQuickProfile(profileId: string): Promise<void> {
    if (!currentConversation || quickProfileBusy || composerAdmissionBusy || voiceTranscribing || turnInProgress || awaitingAssistantStart) return;
    const conversationId = currentConversation.conversation_id;
    quickProfileBusy = true;
    try {
      const response = await executeCommandWithRetry(conversationId, `/profile ${profileId}`);
      if (currentConversation?.conversation_id === conversationId) await applyCommandResponse(response);
    } catch (error) {
      addToast(asApiError(error).message, 'error', 4000, 'Unable to switch profile');
    } finally {
      quickProfileBusy = false;
    }
  }
  let childWorkstream = $derived.by(() => {
    if (!childView || !selectedChildWorkstream) return null;
    const overviewNode = childViewWorkstream(renderedActivityOverview?.workstreams ?? [], childView);
    return enrichChildWorkstream(selectedChildWorkstream, overviewNode);
  });
  let childManagedConversation = $state<Conversation | null>(null);
  let childManagedBusy = $state<string | null>(null);
  let childManagedError = $state('');
  $effect(() => {
    const hidden = Boolean(childView);
    const transitionGeneration = ++parentSurfaceTransitionGeneration;
    timelineInitialPositionSettled = false;
    if (hidden) {
      parentSurfaceWasHidden = true;
      return;
    }
    if (!parentSurfaceWasHidden) return;
    parentSurfaceWasHidden = false;
    const restorePausedPosition = userScrolledUp;
    const loadId = conversationLoadRequestId;
    const controller = timelineController;
    untrack(() => {
      void tick().then(() => {
        requestAnimationFrame(() => {
          if (!pageAlive || loadId !== conversationLoadRequestId || !controller?.current
            || !timelineViewportStates.hasRetainedWindow(controller.scope.key, loadId)
            || controller !== timelineController || transitionGeneration !== parentSurfaceTransitionGeneration || childView) return;
          if (restorePausedPosition) {
            writeProgrammaticScrollTop(parentSurfaceScrollTop);
            restoreTimelineAnchor(parentSurfaceAnchor);
          } else {
            scrollToBottom();
          }
          requestAnimationFrame(() => {
            if (pageAlive && loadId === conversationLoadRequestId && controller.current
              && controller === timelineController
              && timelineViewportStates.hasRetainedWindow(controller.scope.key, loadId)
              && transitionGeneration === parentSurfaceTransitionGeneration && !childView) {
              timelineInitialPositionSettled = true;
            }
          });
        });
      });
    });
  });
  let childBackgroundWork = $derived(
    directChildBackgroundWork(
      backgroundWork.items,
      workstreamSessionIds(childWorkstream),
    ),
  );
  $effect(() => {
    const sessionId = childView?.sessionId;
    const terminal = childWorkstream?.activity_state === 'closed'
      || isTerminalSessionStatus(childWorkstream?.status);
    if (!sessionId || !terminal || !runtimeActiveChildSessionIds.has(sessionId)) return;
    const next = new Set(runtimeActiveChildSessionIds);
    next.delete(sessionId);
    runtimeActiveChildSessionIds = next;
  });
  let subSessionUserScrolledUp = $state(false);
  let timelineEl = $state<HTMLDivElement | null>(null);
  let timelineContentEl = $state<HTMLDivElement | null>(null);
  let userScrolledUp = $state(false);
  let tailPinned = $state(false);
  const observedConversationReads = new ObservedConversationReadTracker({
    markRead: (conversationId) => api.conversations.markRead(conversationId),
    optimisticRead: (conversationId, readThrough) => {
      markConversationObservedByServiceWorker(conversationId, readThrough);
      markConversationReadLocally(conversationId, readThrough);
    },
    isObserved: (conversationId) => (
      typeof document !== 'undefined'
      && hostAllowsObservation()
      && !document.hidden
      && conversationIdFromRoute() === conversationId
    ),
    reconcile: () => scheduleSidebarReconciliation('observed-read-failed'),
  });
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
  let timelineInitialPositionSettled = $state(false);
  const timelineViewportStates = new TimelineViewportStateStore(CONVERSATION_VIEW_CACHE_LIMIT);
  let timelineViewportTransition: TimelineViewportTransition | null = null;
  let userScrollIntentUp = false;
  let lastTimelineTouchY: number | null = null;
  let lastTimelineScrollTop = $state(0);
  let footerChromeEl = $state<HTMLDivElement | null>(null);
  let selectedChannels = $state<string[]>(['web']);
  let chatSidebarCollapsed = $state(false);
  const CHAT_SIDEBAR_DEFAULT_WIDTH = 272;
  const CHAT_SIDEBAR_LEGACY_DEFAULT_WIDTH = 304;
  let chatSidebarWidth = $state(CHAT_SIDEBAR_DEFAULT_WIDTH);
  const CHAT_SIDEBAR_MIN_WIDTH = 240;
  const CHAT_SIDEBAR_MAX_WIDTH = 480;
  let compactSidebarForInspector = $derived(false);
  let canPinInspector = $derived(
    conversationInspectorFits(
      chatShellWidth - (
        chatSidebarCollapsed || isWindowMode
          ? 0
          : chatSidebarWidth
      ),
    )
  );
  let effectiveInspectorWidth = $derived(Math.min(
    conversationInfoDrawer.preferredWidth,
    Math.max(
      INSPECTOR_MIN_WIDTH,
      chatShellWidth
        - (chatSidebarCollapsed || isWindowMode ? 0 : chatSidebarWidth)
        - CHAT_PINNED_INSPECTOR_MIN_CHAT_WIDTH
        - CHAT_PINNED_INSPECTOR_GAP,
    ),
  ));
  let inspectorPresentation = $derived(conversationInfoDrawer.presentation(canPinInspector));
  let inspectorPinned = $derived(inspectorPresentation === 'pinned');
  function setInspectorFocus(focus: boolean) { conversationInfoDrawer.focus = focus; }
  function requestWorkSourceFocus() {
    if (headerInfoOpen && inspectorPresentation === 'pinned') setInspectorFocus(true);
  }
  const chatV2CanonicalRecoveryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const chatV2CanonicalRecoveryCoalescer = new ChatV2CanonicalRecoveryCoalescer();
  // Set when a recovery is requested while one is already in flight. The
  // in-flight recovery re-runs once on completion instead of being silently
  // dropped. Without this, a debounced mid-turn sync (whose server-side event
  // read predates the assistant-message flush) swallows the immediate settle
  // recovery, and the just-streamed final message stays missing until reload.
  const CHAT_V2_CANONICAL_RECOVERY_DEBOUNCE_MS = 1_000;

  function applyChatV2Snapshot(
    snapshot: ChatSnapshot,
    options: {
      resetLineage?: boolean;
      routeGeneration?: number;
    } = {}
  ): boolean {
    // Timeline state was admitted once by TimelineController. Only route
    // metadata, inspector state and viewport ownership are updated here.
    canonicalTimelineAuthority = acceptCanonicalTimelineBase(
      canonicalTimelineAuthority,
      snapshot.conversation.conversation_id,
      options.routeGeneration ?? conversationLoadRequestId,
    );
    conversationViewServerTimeMs = Date.parse(snapshot.server_time) || Date.now();
    lastConversationViewRefreshAt = Date.now();
    lastRuntimeSnapshotAt = Date.now();
    applyContextUsageSnapshot(chatV2Store.snapshot.runtime?.context_usage);
    applyLastGenerationSnapshot(chatV2Store.snapshot.runtime?.last_generation);
    const snapshotOverview = visibleSnapshotOverview(snapshot, inspectorScope?.key);
    const snapshotRoot = rootOverviewForConversation(snapshot.activity_overview ?? null, treeRootConversationId);
    if (snapshotRoot) rootActivityOverview = snapshotRoot;
    if (snapshotOverview) activityOverview = snapshotOverview;
    // Refresh the older-history cursor from the snapshot when we do not already
    // hold one, or when the caller explicitly signals a lineage reset (e.g. a
    // cursor_mismatch/reset_required fallback, or no sync cursor could be
    // established at all). Without this, a snapshot recovery (reconnect,
    // outbox drain, cancel) left olderMessagesCursor stale/empty and
    // subsequent scroll-up backfills became no-ops. A deeper cursor from prior
    // backfill is kept in the ordinary (non-reset) case. On a lineage reset a
    // previously held cursor may reference an event chain that no longer
    // exists (e.g. it was restored from a cached view of an older lineage), so
    // it must always be replaced from the fresh snapshot instead of preserved.
    const cursorResolution = resolveOlderMessagesCursorAfterSnapshot({
      currentCursor: olderMessagesCursor,
      beforeCursor: snapshot.timeline.before_cursor ?? null,
      hasMoreBefore: snapshot.timeline.has_more_before,
      resetLineage: options.resetLineage === true,
    });
    olderMessagesCursor = cursorResolution.olderMessagesCursor;
    hasOlderMessages = cursorResolution.hasOlderMessages;
    applyChatV2StateToTimeline();
    chatV2Lifecycle.acceptSnapshot(
      conversationTimelineScope(snapshot.conversation.conversation_id),
      snapshot.cursor,
    );
    return true;
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

  function canonicalTimelineBootstrapPending(conversationId: string): boolean {
    return (
      canonicalTimelineAuthority.conversationId === conversationId
      && canonicalTimelineAuthority.routeGeneration === conversationLoadRequestId
      && canonicalTimelineAuthority.status === 'loading'
      && !canonicalTimelineAuthority.baseAccepted
    );
  }

  // A->B->A recovery race: this conversationId-vs-route check alone does not
  // distinguish a genuinely fresh recovery for A from a late response to an
  // A recovery request that was still in flight when the user switched away
  // to B and back to A -- both see the same conversationId once back on A.
  // This is intentionally left as conversationId-only: the actual staleness
  // protection is the cursor_before check in
  // chat-v2/sync-engine.ts:applySyncLike (state.cursor is reset to the cached
  // entry's cursor by chatV2Store.restoreState() on cached restore). A late
  // response either matches that basis cursor -- meaning nothing else
  // advanced state in the meantime, so applying it is correct regardless of
  // the intervening B visit -- or it is rejected as 'cursor_mismatch' and
  // routed to a resetLineage snapshot. The
  // ChatV2CanonicalRecoveryCoalescer additionally coalesces concurrent
  // recoverChatV2Canonical() calls per conversationId, so a reissue after
  // cached restore never races a duplicate request against the in-flight
  // one. See sync-engine.test.ts: "rejects a late canonical-recovery sync
  // response for A that resolves after A->B->A switching advanced state past
  // its basis cursor" for the proof this relies on.



  async function applyChatV2Frame(frame: ChatRealtimeFrame): Promise<void> {
    if (!timelineController?.current) return;
    const previousStatus = chatV2Store.snapshot.syncStatus;
    const previouslyActive = chatV2Store.snapshot.runtime?.has_active_turn === true;
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
    if (
      result.outcome === 'applied'
      && previouslyActive
      && chatV2Store.snapshot.runtime?.has_active_turn !== true
    ) {
      void observedConversationReads.observe({
        conversationId: frame.conversation_id,
        version: frame.cursor_after,
        readThrough: frame.server_time,
      });
    }
  }

  async function recoverChatV2Canonical(
    conversationId: string,
    requestId?: number
  ): Promise<void> {
    const routeGeneration = requestId ?? conversationLoadRequestId;
    const conversation = currentConversation?.conversation_id === conversationId
      ? currentConversation : sidebarConversation(conversationId);
    if (appContactBindingId(conversation)) return;
    const ownerLease = documentCoordination().capture();
    if (
      !chatV2RecoveryMatchesRoute(conversationId)
      || isStaleConversationLoad(routeGeneration)
    ) return;
    await chatV2CanonicalRecoveryCoalescer.run(
      conversationId,
      () => performChatV2CanonicalRecovery(conversationId, routeGeneration),
    );
    if (!isStaleConversationLoad(routeGeneration) && !historyError) {
      await settleInitialCanonicalHistory(conversationId, routeGeneration);
      if (
        !isStaleConversationLoad(routeGeneration)
        && chatV2RecoveryMatchesRoute(conversationId)
        && chatV2OwnsActiveConversation(conversationId)
        && canonicalTimelineAuthority.conversationId === conversationId
        && canonicalTimelineAuthority.routeGeneration === routeGeneration
        && canonicalTimelineAuthority.status === 'ready'
      ) {
        try { ownerLease.assertCurrent(); } catch { return; }
        // Canonical completion does not wait for admission; the drain retains
        // this component's generation, outbox and account lease.
        void drainChatV2Outbox(conversationId).catch((error) => {
          if (!composerAlive || isStaleConversationLoad(routeGeneration)) return;
          try { ownerLease.assertCurrent(); } catch { return; }
          reportError(error);
        });
      }
    }
  }

  async function settleInitialCanonicalHistory(
    conversationId: string,
    routeGeneration: number,
  ): Promise<void> {
    if (
      isStaleConversationLoad(routeGeneration)
      || activeConversationId !== conversationId
    ) return;
    if (!canonicalTimelineBaseIsAccepted(
      canonicalTimelineAuthority,
      conversationId,
      routeGeneration,
    )) {
      scheduleChatV2CanonicalRecovery(conversationId, { immediate: true });
      return;
    }
    try {
      const status = await backfillInitialVisibleHistory({
        maxPages: 8,
        getState: () => ({
          visibleCount: renderableVisibleItems.length,
          hasMoreBefore: hasOlderMessages,
          beforeCursor: olderMessagesCursor,
        }),
        loadPage: (before) => bindTimeline(conversationId).older(before, timelineLoadAbort.signal),
        applyPage: (response) => {
          if (!response) return false;
          applyChatV2StateToTimeline();
          olderMessagesCursor = response.before_cursor ?? null;
          hasOlderMessages = Boolean(response.has_more_before && olderMessagesCursor);
          saveCurrentConversationView();
          return true;
        },
        isCurrent: () => (
          !isStaleConversationLoad(routeGeneration)
          && activeConversationId === conversationId
        ),
      });
      if (status === 'visible' || status === 'exhausted' || status === 'budget_exhausted') {
        setCanonicalTimelineAuthority(conversationId, routeGeneration, 'ready');
      } else if (status === 'rejected') {
        void recoverChatV2Canonical(conversationId, routeGeneration);
      }
    } catch (caughtError) {
      if (
        !isStaleConversationLoad(routeGeneration)
        && activeConversationId === conversationId
      ) {
        historyError = asApiError(caughtError).message;
        markControllerUnavailable(caughtError, conversationId);
        setCanonicalTimelineAuthority(conversationId, routeGeneration, 'error');
      }
      throw caughtError;
    }
  }

  async function performChatV2CanonicalRecovery(
    conversationId: string,
    routeGeneration: number,
  ): Promise<void> {
    const matchesLoad = () => (
      chatV2RecoveryMatchesRoute(conversationId)
      && !isStaleConversationLoad(routeGeneration)
    );
    if (!matchesLoad()) return;
    try {
      const snapshot = await bindTimeline(conversationId).load({ signal: timelineLoadAbort.signal });
      if (!matchesLoad()) return;
      if (snapshot) applyChatV2Snapshot(snapshot, { resetLineage: true, routeGeneration });
      applyContextUsageSnapshot(chatV2Store.snapshot.runtime?.context_usage);
      applyLastGenerationSnapshot(chatV2Store.snapshot.runtime?.last_generation);
      applyChatV2StateToTimeline();
      canonicalTimelineAuthority = acceptCanonicalTimelineBase(
        canonicalTimelineAuthority,
        conversationId,
        routeGeneration,
      );
      canonicalTimelineApplyRevision += 1;
      if (chatV2Store.snapshot.cursor) {
        wsClient.updateChatV2Cursor(conversationId, chatV2Store.snapshot.cursor);
      }
      historyError = '';
      criticalEventStoreError = '';
      // A successful incremental sync is proof the conversation's runtime
      // view is now current, exactly like a full snapshot. Without bumping
      // these markers here, a cached-restore reconciliation that lands via
      // this branch (rather than a fallback snapshot) leaves them at their
      // pre-switch values; if that stale timestamp is already older than
      // STALE_RUNTIME_REFRESH_MS, the periodic stale-runtime guard fires a
      // redundant full refreshConversationView() moments later, undoing the
      // point of reconciling incrementally.
      lastConversationViewRefreshAt = Date.now();
      lastRuntimeSnapshotAt = Date.now();
      } catch (error) {
        if (!matchesLoad()) return;
        const apiError = asApiError(error);
        if (apiError.status === 503 || apiError.code === 'event_store_unavailable') {
          historyError = apiError.message;
          setCanonicalTimelineAuthority(conversationId, routeGeneration, 'error');
          markControllerUnavailable(error);
          return;
        }
        historyError = apiError.message;
        setCanonicalTimelineAuthority(conversationId, routeGeneration, 'error');
      }
  }

  async function recoverChatV2Snapshot(
    conversationId: string,
    options: { resetLineage?: boolean; replaceUnconditionally?: boolean } = {},
    requestId?: number,
  ): Promise<boolean> {
    const routeGeneration = requestId ?? conversationLoadRequestId;
    const matchesLoad = () => (
      chatV2RecoveryMatchesRoute(conversationId)
      && !isStaleConversationLoad(routeGeneration)
    );
    if (!matchesLoad()) return false;
    try {
        const snapshot = await bindTimeline(conversationId).load({
          signal: timelineLoadAbort.signal,
          forceSnapshot: true,
        });
        if (!matchesLoad()) return false;
        if (!snapshot) {
          applyChatV2StateToTimeline();
          return true;
        }
        if (applyChatV2Snapshot(snapshot, {
          ...options,
          routeGeneration,
        })) {
          historyError = '';
          criticalEventStoreError = '';
          canonicalTimelineApplyRevision += 1;
          return true;
        }
      scheduleChatV2CanonicalRecovery(conversationId);
      return false;
    } catch (error) {
      if (!matchesLoad()) return false;
      const failureMessage = asApiError(error).message;
      if (conversationIdFromRoute() !== conversationId) {
        reportError(error);
        return false;
      }
      historyError = failureMessage;
      setCanonicalTimelineAuthority(conversationId, routeGeneration, 'error');
      if (markControllerUnavailable(error)) {
        return false;
      }
      if (chatV2Store.snapshot.conversationId === conversationId) {
        if (chatV2Store.visibleItems.length === 0) {
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
      // Connectivity failures belong to the global banner; the load is
      // retried when the connection is restored.
      if (!isConnectivityError(error)) addToast(failureMessage, 'error');
      return false;
    }
  }

  function cancelActiveTurnWithChatV2(): Promise<boolean> {
    if (!currentConversation || !turnInProgress) return Promise.resolve(true);
    if (cancelTurnPending) return Promise.resolve(false);
    const cancelConversationId = currentConversation.conversation_id;
    cancelTurnPendingConversationId = cancelConversationId;
    return chatV2Api.cancelTurn(cancelConversationId, {
      client_txn_id: crypto.randomUUID()
    }).then(async (response) => {
      if (chatV2OwnsActiveConversation(cancelConversationId)) {
        chatV2Store.applyCancel(response);
        applyChatV2StateToTimeline();
      }
      await recoverChatV2Snapshot(cancelConversationId);
      return true;
    }).catch((caughtError) => {
      addToast(asApiError(caughtError).message, 'error');
      return false;
    }).finally(() => {
      if (cancelTurnPendingConversationId === cancelConversationId) {
        cancelTurnPendingConversationId = null;
      }
    });
  }

  function scheduleChatV2CanonicalRecovery(
    conversationId: string,
    { immediate = false }: { immediate?: boolean } = {},
  ): void {
    if (
      !chatV2OwnsActiveConversation(conversationId)
      && !canonicalTimelineBootstrapPending(conversationId)
    ) return;
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
    if (!composerAlive || !chatV2OwnsActiveConversation(conversationId)) return;
    const routeGeneration = conversationLoadRequestId;
    const ownerLease = documentCoordination().capture();
    const outbox = chatV2Outbox;
    const isCurrent = () => {
      if (!composerAlive || isStaleConversationLoad(routeGeneration)
        || !chatV2OwnsActiveConversation(conversationId)) return false;
      try { ownerLease.assertCurrent(); return true; } catch { return false; }
    };
    if (!isCurrent()) return;
    const run = async () => {
      if (!isCurrent()) return;
      chatV2OutboxDraining = true;
      chatV2OutboxRetry.cancel(conversationId);
      let sentAny = false;
      let retryRemaining = false;
      try {
        const result = await drainConversationOutbox({
          outbox, conversationId, isCurrent,
          onAdmitted: (_entry, dispatched) => {
            if (dispatched.kind === 'message' && isCurrent()) {
              chatV2Store.applySend(dispatched.response);
            }
          },
        });
        sentAny = result.sentAny;
        retryRemaining = result.retryRemaining;
      } catch (caughtError) {
        reportError(caughtError);
      } finally {
        chatV2OutboxDraining = false;
        const pending = chatV2PendingDrain;
        chatV2PendingDrain = null;
        if (pending) await pending();
      }
      if (!isCurrent()) return;
      if (retryRemaining) {
        scheduleChatV2OutboxRetry(conversationId);
      } else {
        chatV2OutboxRetry.reset(conversationId);
      }
      if (sentAny) {
        await recoverChatV2Snapshot(conversationId);
      }
    };
    if (chatV2OutboxDraining) {
      // Coalesce to the latest eligible caller, retaining its own lease and
      // generation rather than recreating a request from the finishing drain.
      chatV2PendingDrain = run;
      return;
    }
    await run();
  }

  /**
   * Show messages still waiting in the durable outbox (sent offline or
   * before a reload) as pending/failed bubbles in their conversation.
   */
  async function showPendingOutboxMessages(conversationId: string): Promise<void> {
    let entries: OutboxEntry[];
    try {
      entries = (await chatV2Outbox.list(conversationId))
        .filter((entry) => entry.status !== 'acked')
        .sort((a, b) => a.created_at.localeCompare(b.created_at));
    } catch {
      return;
    }
    if (entries.length === 0 || !chatV2OwnsActiveConversation(conversationId)) return;
    const visibleClientIds = new Set(
      chatV2Store.visibleItems
        .map((item) => (item.kind === 'message' ? item.client_message_id : null))
        .filter((id): id is string => Boolean(id)),
    );
    let changed = false;
    for (const entry of entries) {
      if (visibleClientIds.has(entry.client_message_id)) continue;
      chatV2Store.addOptimisticUser({
        content: entry.content,
        attachments: entry.attachments,
        clientMessageId: entry.client_message_id,
        chatMode: entry.chat_mode ?? undefined,
      });
      if (entry.retryable === false) chatV2Store.markOptimisticUserFailed(entry.client_message_id);
      changed = true;
    }
    if (changed) applyChatV2StateToTimeline();
  }

  function scheduleChatV2OutboxRetry(conversationId: string): void {
    chatV2OutboxRetry.schedule(conversationId, () => {
      if (currentConversation?.conversation_id !== conversationId) return;
      void drainChatV2Outbox(conversationId);
    });
  }

  // Synchronize page-level state (queue, turn flags) from the Chat v2 store.
  // The rendered timeline reads `chatV2Store.visibleItems` reactively, so no
  // imperative timeline mutation is needed here. Scroll position is owned by
  // the Chat v2 viewport (anchor/follow), not by replacement side effects.
  function applyChatV2StateToTimeline(): void {
    const projection = deriveChatV2ViewProjection(chatV2Store.snapshot);
    queuedMessages = visibleQueuedMessages(
      chatV2Store.visibleQueue.messages as QueuedMessage[]
    ).filter((message) => !(retryWaitingTurn && message.status === 'recoverable'));
    queuedCount = queuedMessages.length;
    turnInProgress = projection.turnInProgress;
    awaitingAssistantStart = projection.awaitingAssistantStart;
    currentActiveTurnId = projection.currentActiveTurnId;
    const conversationId = chatV2Store.snapshot.conversationId;
    const runtimePatch = currentConversation?.is_readonly_for_caller
      ? null : chatV2RuntimeConversationPatch(chatV2Store.snapshot);
    const runtimeGeneratedAt = chatV2Store.snapshot.runtime?.generated_at ?? null;
    const activeTurnId = chatV2Store.snapshot.runtime?.has_active_turn
      ? chatV2Store.snapshot.runtime.active_turn?.turn_id ?? null
      : null;
    if (conversationId && runtimeGeneratedAt) {
      const currentState = conversationRuntimeLifecycle.get(conversationId);
      if (
        !currentState
        || Date.parse(runtimeGeneratedAt) >= Date.parse(currentState.updatedAt ?? '')
      ) {
        conversationRuntimeLifecycle.set(conversationId, {
          updatedAt: runtimeGeneratedAt,
          activeTurnId,
        });
      }
    }
    if (
      runtimePatch
      && conversationId
      && currentConversation?.conversation_id === conversationId
      && (
        currentConversation.has_active_turn !== runtimePatch.has_active_turn
        || currentConversation.active_turn_chat_mode !== runtimePatch.active_turn_chat_mode
        || currentConversation.active_turn_chat_mode_source !== runtimePatch.active_turn_chat_mode_source
      )
    ) {
      patchConversationInList(conversationId, runtimePatch);
    }
    activeTurnChatMode = runtimePatch?.has_active_turn
      ? normalizeChatModeTone(runtimePatch.active_turn_chat_mode)
      : 'default';
    shrinkTailWindowIfPinned();
    lastRenderableVisibleCount = renderableVisibleItems.length;
  }

  function applyChatV2RuntimeToTimeline(): void {
    // Runtime overlay items are part of `chatV2Store.visibleItems`; the native
    // renderer picks them up reactively. Only page-level flags are synced here.
    applyChatV2StateToTimeline();
  }

  const projectedConversationId = $derived(currentConversation?.conversation_id ?? null);
  $effect(() => {
    const state = chatV2Store.snapshot;
    if (!state.cursor || state.conversationId !== projectedConversationId) return;
    // Another surface may advance the shared owner without this route making
    // a request. Keep route-local flags and pagination aligned with that owner.
    untrack(() => {
      olderMessagesCursor = state.beforeCursor;
      hasOlderMessages = state.hasMoreBefore;
      applyChatV2StateToTimeline();
    });
  });

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
    if (response.result_type === 'recap' || response.result_type === 'handoff') {
      scheduleChatV2CanonicalRecovery(response.conversation_id, { immediate: true });
      return;
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
    const runtimeSelection = data.runtime_selection;
    const activeSessionId = currentConversation?.active_session_id;
    if (
      activeSessionId
      && runtimeSelection
      && typeof runtimeSelection === 'object'
      && typeof (runtimeSelection as { revision?: unknown }).revision === 'number'
    ) {
      const incoming = runtimeSelection as import('$lib/types/api').RuntimeSelection;
      const currentSelection = (
        latestRuntimeSelection?.sessionId === activeSessionId
          ? latestRuntimeSelection.selection
          : null
      );
      latestRuntimeSelection = {
        sessionId: activeSessionId,
        selection: newestRuntimeSelection(currentSelection, incoming)
      };
      if (sessionInfo && currentConversation) {
        sessionInfo = mergeRuntimeSelection(sessionInfo, latestRuntimeSelection.selection);
        setSessionInfo(currentConversation.conversation_id, activeSessionId, sessionInfo);
      }
    }
    if (currentConversation && typeof data.executor_id === 'string') {
      patchConversationInList(currentConversation.conversation_id, {
        active_executor_id: data.executor_id,
        active_executor_source: 'user'
      });
    }
    if (
      currentConversation
      && response.conversation_id === currentConversation.conversation_id
      && Object.hasOwn(data, 'maximum_outcome_override')
      && (data.maximum_outcome_override === 'approve' || data.maximum_outcome_override === null)
    ) {
      const platformData = { ...(currentConversation.context?.platform_data ?? {}) };
      if (data.maximum_outcome_override === 'approve') {
        platformData.maximum_outcome_override = 'approve';
      } else {
        delete platformData.maximum_outcome_override;
      }
      const context = { ...currentConversation.context, platform_data: platformData };
      currentConversation = { ...currentConversation, context };
      patchConversationInList(currentConversation.conversation_id, { context });
      if (sessionInfo && currentConversation.active_session_id === sessionInfo.intaris_session_id) {
        sessionInfo = {
          ...sessionInfo,
          maximum_outcome: data.maximum_outcome_override === 'approve'
            ? 'approve'
            : conversationAgent(currentConversation)?.capabilities?.maximum_outcome ?? null
        };
        setSessionInfo(currentConversation.conversation_id, currentConversation.active_session_id, sessionInfo);
      }
    }
    if (data.command === '/profile' && typeof data.resolved_agent_profile_id === 'string') {
      // The durable projection owns coalescing and turn boundaries; do not
      // append a second speculative card while its canonical update arrives.
      scheduleChatV2CanonicalRecovery(response.conversation_id, { immediate: true });
      return;
    }
    appendChatV2LocalSystemMessage(
      response.text,
      `rest:${response.result_type}:${response.client_txn_id}`,
      typeof data.notice_id === 'string' ? data.notice_id : undefined
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

  const forkAssistantMessage: ForkAssistantMessage = async (sourceSessionId, sourceSeq) => {
    if (!currentConversation) return;
    try {
      const fork = await chatV2Api.forkAssistantMessage(currentConversation.conversation_id, {
        source_session_id: sourceSessionId,
        source_seq: sourceSeq
      });
      addToast('Conversation forked.', 'success');
      await goto(conversationUrl(fork.conversation_id));
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to fork conversation');
      throw caughtError;
    }
  };
  setContext(forkAssistantMessageContext, forkAssistantMessage);

  let sessionInfo = $state<SessionInfoData | null>(null);
  let sessionInfoLoading = $state(false);
  let sessionInfoRequestId = 0;
  let latestRuntimeSelection = $state<{
    sessionId: string;
    selection: import('$lib/types/api').RuntimeSelection;
  } | null>(null);
  let sessionNarrativeExpanded = $state(false);

  interface ConversationViewCacheEntry {
    conversation: Conversation;
    sessions: Session[];
    queuedCount: number;
    queuedMessages: QueuedMessage[];
    contextUsage: ContextUsage | null;
    sessionInfo: SessionInfoData | null;
    visibleStartIndex: number;
    scrollTop: number;
    userScrolledUp: boolean;
    viewportAnchor: TimelineViewportAnchor | null;
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
    diagnosticsOwnerSessionId = null;
    contextUsage = null;
    lastGenerationPerformance = null;
  }

  function isStaleSessionInfoLoad(requestId: number, conversationId: string, sessionId: string): boolean {
    return (
      requestId !== sessionInfoRequestId ||
      currentConversation?.conversation_id !== conversationId ||
      (focusedSessionId ?? currentConversation?.active_session_id) !== sessionId
    );
  }

  function formatTokenCount(value: number | null | undefined): string {
    if (typeof value !== 'number' || !Number.isFinite(value)) return 'n/a';
    return value.toLocaleString();
  }

  function performanceMeasurementLabel(performance: GenerationPerformanceSnapshot | null): string {
    return performance?.measured_at
      ? `Measured ${new Date(performance.measured_at).toLocaleString()}`
      : 'Local runtime telemetry is not available for this model.';
  }

  function performanceHardwareLabel(performance: GenerationPerformanceSnapshot | null): string {
    if (!performance) return '';
    return [
      performance.processor ? `Processor ${performance.processor}` : null,
      performance.gpu_residency ? `GPU residency ${performance.gpu_residency}` : null,
      performance.digest ? `Digest ${performance.digest}` : null,
    ].filter(Boolean).join(' · ');
  }

  function optionalPerformanceMetrics(performance: GenerationPerformanceSnapshot | null) {
    return performance ? localPerformanceMetrics(performance) : [];
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
    if (!usage) return 'text-muted';
    const contextWindowPercentage = contextWindowUsagePercentage(usage);
    if (contextWindowPercentage > 95) return 'text-danger';
    if (contextWindowPercentage > 85) return 'text-danger/80';
    if (contextWindowPercentage > 65) return 'text-warning';
    return 'text-success';
  }

  function contextUsageStroke(usage: ContextUsage | null): string {
    if (!usage) return 'rgb(var(--color-muted))';
    const contextWindowPercentage = contextWindowUsagePercentage(usage);
    if (contextWindowPercentage > 95) return 'rgb(var(--color-danger))';
    if (contextWindowPercentage > 85) return 'rgb(var(--color-danger) / 0.8)';
    if (contextWindowPercentage > 65) return 'rgb(var(--color-warning))';
    return 'rgb(var(--color-success))';
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

  function activeAgentProfileLabel(): string | null {
    const profileId = currentConversation?.agent_profile_id;
    if (profileId) return profileId;
    return contextUsageProfileLabel(activeContextUsage());
  }

  function activeContextUsage(): ContextUsage | null {
    const sessionId = focusedSessionId ?? currentConversation?.active_session_id ?? null;
    return diagnosticsOwnerSessionId === sessionId ? contextUsage : null;
  }

  function activeLastGeneration(): GenerationPerformanceSnapshot | null {
    const sessionId = focusedSessionId ?? currentConversation?.active_session_id ?? null;
    return diagnosticsOwnerSessionId === sessionId ? lastGenerationPerformance : null;
  }

  function applyContextUsageSnapshot(
    usage: ContextUsage | null | undefined,
    ownerSessionId = currentConversation?.active_session_id ?? null,
  ): void {
    if (!acceptsSessionDiagnostics(
      focusedSessionId,
      currentConversation?.active_session_id ?? null,
      ownerSessionId,
    )) return;
    if (!usage) return;
    const previousOwnerSessionId = diagnosticsOwnerSessionId;
    diagnosticsOwnerSessionId = ownerSessionId;
    contextUsage = previousOwnerSessionId === ownerSessionId
      ? mergeContextUsage(contextUsage, usage)
      : usage;
    if (sessionInfo?.intaris_session_id === ownerSessionId) {
      sessionInfo = mergeSessionContextUsage(sessionInfo, contextUsage);
      if (currentConversation) {
        setSessionInfo(currentConversation.conversation_id, ownerSessionId!, sessionInfo);
      }
    }
  }

  function applyLastGenerationSnapshot(
    performance: GenerationPerformanceSnapshot | null | undefined,
    ownerSessionId = currentConversation?.active_session_id ?? null,
  ): void {
    if (!acceptsSessionDiagnostics(
      focusedSessionId,
      currentConversation?.active_session_id ?? null,
      ownerSessionId,
    )) return;
    if (performance === undefined) return;
    const previousOwnerSessionId = diagnosticsOwnerSessionId;
    diagnosticsOwnerSessionId = ownerSessionId;
    lastGenerationPerformance = previousOwnerSessionId === ownerSessionId
      ? mergeLatestPerformance(lastGenerationPerformance, performance)
      : performance;
    if (sessionInfo?.intaris_session_id === ownerSessionId) {
      sessionInfo = { ...sessionInfo, last_generation: lastGenerationPerformance };
    }
  }

  function applyCachedSessionDiagnostics(sessionId: string | null, info: SessionInfoData | null): void {
    const diagnostics = diagnosticsForSession(sessionId, info);
    const previousOwnerSessionId = diagnosticsOwnerSessionId;
    diagnosticsOwnerSessionId = diagnostics.ownerSessionId;
    contextUsage = previousOwnerSessionId === diagnostics.ownerSessionId
      ? mergeContextUsage(contextUsage, diagnostics.contextUsage)
      : diagnostics.contextUsage;
    lastGenerationPerformance = previousOwnerSessionId === diagnostics.ownerSessionId
      ? mergeLatestPerformance(lastGenerationPerformance, diagnostics.lastGeneration)
      : diagnostics.lastGeneration;
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
      if (percent > 130) return 'bg-danger';
      if (percent > 110) return 'bg-danger/80';
      if (percent > 85) return 'bg-warning';
      return 'bg-success';
    }
    if (label === 'Within-turn burst') {
      if (percent > 100) return 'bg-danger';
      if (percent > 95) return 'bg-danger/80';
      if (percent > 80) return 'bg-warning';
      return 'bg-success';
    }
    if (label === 'Hard target') {
      if (percent > 100) return 'bg-danger';
      if (percent > 95) return 'bg-danger/80';
      if (percent > 85) return 'bg-warning';
      return 'bg-success';
    }
    if (label === 'Effective prompt budget' || label === 'Loop pressure') {
      if (percent > 98) return 'bg-danger';
      if (percent > 90) return 'bg-danger/80';
      if (percent > 75) return 'bg-warning';
      return 'bg-success';
    }
    if (percent > 95) return 'bg-danger';
    if (percent > 85) return 'bg-danger/80';
    if (percent > 65) return 'bg-warning';
    return 'bg-success';
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

  function toggleHeaderInfo(event?: MouseEvent): void {
    if (!conversationInfoDrawer.open) {
      headerInfoTrigger = event?.currentTarget instanceof HTMLElement
        ? event.currentTarget
        : document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      conversationInfoDrawer.mode = 'overview';
      conversationInfoDrawer.contextOpen = false;
      conversationInfoDrawer.setOpen(true);
      void loadVisibleActivityOverview();
    } else {
      cancelActivityOverviewDemand();
      conversationInfoDrawer.contextOpen = false;
      conversationInfoDrawer.setOpen(false);
    }
    if (conversationInfoDrawer.open && conversationInfoDrawer.tab === 'session' && !sessionInfo) {
      void loadSessionInfo();
    }
  }

   const inspectorScope = $derived(
    currentConversation
      ? childView
        ? childViewScope(childView)
        : requestedInspectorScope
      : null
   );
   const canManageInspectorConversationLifecycle = $derived(
     canShowConversationLifecycleActions(
       inspectorScope,
       currentConversation?.conversation_id ?? null,
       childView !== null,
       isTaskControlMode,
       currentConversation ? isAgentDirectConversation(currentConversation) : false,
     )
   );
  const rootOverviewEpoch = new RootOverviewRequestEpoch();
  let rootRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  const treeReconciliation = new SerialInvalidationCoalescer(async () => {
    if (
      !activityOverviewIsVisible(headerInfoOpen, headerInfoMode)
      || document.hidden
    ) return;
    await loadVisibleActivityOverview(true);
  });

  function scheduleTreeRefresh(): void {
    if (!activityOverviewIsVisible(headerInfoOpen, headerInfoMode)) return;
    if (rootRefreshTimer) return;
    rootRefreshTimer = setTimeout(() => {
      rootRefreshTimer = null;
      if (document.hidden) return;
      treeReconciliation.invalidate();
    }, 40);
  }

  function cancelScheduledTreeRefresh(): void {
    if (!rootRefreshTimer) return;
    clearTimeout(rootRefreshTimer);
    rootRefreshTimer = null;
  }

  async function loadRootActivityOverview(force = false): Promise<boolean> {
    if (!treeRootConversationId) return false;
    cancelRootOverviewLoad();
    const rootId = treeRootConversationId;
    const scope = conversationTimelineScope(rootId);
    if (force) invalidateActivityOverview(scope.key);
    const cached = getActivityOverviewEntry(scope);
    if (cached) {
      const valid = rootOverviewForConversation(cached.value, rootId);
      if (valid) rootActivityOverview = valid;
      if (!force && cached.state === 'fresh' && valid) return true;
    }
    const rootRead = beginActivityOverviewRead(
      activityOverviewReadState,
      'root',
      scope.key,
      Boolean(rootOverviewForConversation(rootActivityOverview, rootId)),
    );
    activityOverviewReadState = rootRead.state;
    let rootApplied = false;
    let rootError: string | null = null;
    const controller = new AbortController();
    rootOverviewAbortController = controller;
    const token = rootOverviewEpoch.begin(rootId);
    try {
      rootApplied = await promoteRootOverview(
        rootOverviewEpoch,
        token,
        () => treeRootConversationId,
        () => requestActivityOverview(
          scope,
          (signal) => chatV2Api.activityOverview(scope, { signal }),
          {
            signal: controller.signal,
            ...(force ? {
              exactLoader: (signal: AbortSignal) => chatV2Api.activityOverview(
                scope,
                { detail: 'full', signal },
              ),
              onExact: (exact: ActivityOverviewResponse) => {
                if (!rootOverviewEpoch.isCurrent(token, treeRootConversationId)) return;
                const valid = rootOverviewForConversation(exact, rootId);
                if (valid) rootActivityOverview = valid;
              },
            } : {}),
          },
        ),
        (next) => {
          const valid = rootOverviewForConversation(next, rootId);
          if (valid) rootActivityOverview = valid;
        },
      );
      return rootApplied;
    } catch (caughtError) {
      if (!rootOverviewEpoch.isCurrent(token, treeRootConversationId)) return false;
      if (rootId !== currentConversation?.conversation_id) {
        const fallbackId = currentConversation?.conversation_id;
        if (!fallbackId) return false;
        const fallbackScope = conversationTimelineScope(fallbackId);
        try {
          rootApplied = await promoteRootOverview(
            rootOverviewEpoch,
            token,
            () => treeRootConversationId,
            () => requestActivityOverview(
              fallbackScope,
              (signal) => chatV2Api.activityOverview(fallbackScope, { signal }),
              {
                signal: controller.signal,
                ...(force ? {
                  exactLoader: (signal: AbortSignal) => chatV2Api.activityOverview(
                    fallbackScope,
                    { detail: 'full', signal },
                  ),
                  onExact: (exact: ActivityOverviewResponse) => {
                    if (!rootOverviewEpoch.isCurrent(token, treeRootConversationId)) return;
                    const valid = rootOverviewForConversation(exact, fallbackId);
                    if (valid) rootActivityOverview = valid;
                  },
                } : {}),
              },
            ),
            (fallback) => {
              const valid = rootOverviewForConversation(fallback, fallbackId);
              if (valid) rootActivityOverview = valid;
            },
          );
          return rootApplied;
        } catch (fallbackError) {
          // Retain the currently rendered root. A failed fallback must not
          // clear or promote data for a newer tree-root conversation.
          if (!isActivityOverviewAbort(fallbackError)) {
            rootError = fallbackError instanceof Error
              ? fallbackError.message
              : 'Unable to load activity overview';
          }
          return false;
        }
      }
      if (!isActivityOverviewAbort(caughtError)) {
        rootError = caughtError instanceof Error
          ? caughtError.message
          : 'Unable to load activity overview';
      }
      return false;
    } finally {
      if (rootOverviewAbortController === controller) {
        rootOverviewAbortController = null;
      }
      activityOverviewReadState = settleActivityOverviewRead(
        activityOverviewReadState,
        rootRead.token,
        {
          applied: rootApplied,
          hasData: Boolean(rootOverviewForConversation(rootActivityOverview, rootId)),
          error: rootError,
        },
      );
    }
  }

  function cancelRootOverviewLoad(): void {
    rootOverviewAbortController?.abort();
    rootOverviewAbortController = null;
    activityOverviewReadState = cancelActivityOverviewRead(activityOverviewReadState, 'root');
  }

  $effect(() => {
    if (
      focusedSessionId
      && canonicalFocusedSessionId
      && focusedSessionId !== canonicalFocusedSessionId
    ) {
      focusedSessionId = canonicalFocusedSessionId;
    }
  });

  async function loadActivityOverview(force = false): Promise<boolean> {
    if (!inspectorScope) return false;
    const requestedScope = inspectorScope;
    cancelActivityOverviewLoad();
    if (force) invalidateActivityOverview(requestedScope.key);
    const cached = getActivityOverviewEntry(requestedScope);
    if (cached) {
      activityOverview = cached.value;
      const cachedRoot = rootOverviewForConversation(cached.value, treeRootConversationId);
      if (cachedRoot) rootActivityOverview = cachedRoot;
      if (!force && cached.state === 'fresh') return true;
    }
    const generation = ++activityOverviewGeneration;
    const focusedRead = beginActivityOverviewRead(
      activityOverviewReadState,
      'focused',
      requestedScope.key,
      activityOverview?.scope.key === requestedScope.key,
    );
    activityOverviewReadState = focusedRead.state;
    const controller = new AbortController();
    activityOverviewAbortController = controller;
    let focusedApplied = false;
    let focusedError: string | null = null;
    try {
      const next = await requestActivityOverview(
        requestedScope,
        (signal) => chatV2Api.activityOverview(requestedScope, { signal }),
        {
          signal: controller.signal,
          ...(force ? {
            exactLoader: (signal: AbortSignal) => chatV2Api.activityOverview(
              requestedScope,
              { detail: 'full', signal },
            ),
            onExact: (exact: ActivityOverviewResponse) => {
              if (
                inspectorScope?.key !== requestedScope.key
                || generation !== activityOverviewGeneration
              ) return;
              activityOverview = exact;
              const exactRoot = rootOverviewForConversation(exact, treeRootConversationId);
              if (exactRoot) rootActivityOverview = exactRoot;
            },
          } : {}),
        },
      );
      if (inspectorScope?.key === requestedScope.key && generation === activityOverviewGeneration) {
        activityOverview = next;
        const nextRoot = rootOverviewForConversation(next, treeRootConversationId);
        if (nextRoot) rootActivityOverview = nextRoot;
        focusedApplied = true;
        return true;
      }
      return false;
    } catch (caughtError) {
      if (inspectorScope?.key === requestedScope.key && generation === activityOverviewGeneration) {
        if (!isActivityOverviewAbort(caughtError)) {
          focusedError = caughtError instanceof Error
            ? caughtError.message
            : 'Unable to load activity overview';
        }
      }
      return false;
    } finally {
      if (activityOverviewAbortController === controller) {
        activityOverviewAbortController = null;
      }
      activityOverviewReadState = settleActivityOverviewRead(
        activityOverviewReadState,
        focusedRead.token,
        {
          applied: focusedApplied,
          hasData: activityOverview?.scope.key === requestedScope.key,
          error: focusedError,
        },
      );
    }
  }

  function cancelActivityOverviewLoad(): void {
    activityOverviewGeneration += 1;
    activityOverviewAbortController?.abort();
    activityOverviewAbortController = null;
    activityOverviewReadState = cancelActivityOverviewRead(
      activityOverviewReadState,
      'focused',
    );
  }

  function cancelActivityOverviewDemand(): void {
    cancelScheduledTreeRefresh();
    cancelRootOverviewLoad();
    cancelActivityOverviewLoad();
  }

  async function loadVisibleActivityOverview(force = false): Promise<boolean> {
    if (!activityOverviewIsVisible(headerInfoOpen, headerInfoMode) || !inspectorScope) return false;
    const focusedScopeKey = inspectorScope.key;
    const rootScopeKey = treeRootConversationId
      ? conversationTimelineScope(treeRootConversationId).key
      : null;
    if (!rootScopeKey) return false;
    return reconcileRenderedOverviewSources({
      focusedScopeKey,
      rootScopeKey,
      currentFocusedScopeKey: () => inspectorScope?.key ?? null,
      loadRoot: () => loadRootActivityOverview(force),
      loadFocused: () => loadActivityOverview(force),
    });
  }

  onDestroy(cancelActivityOverviewLoad);
  onDestroy(cancelRootOverviewLoad);
  onDestroy(cancelScheduledTreeRefresh);
  onDestroy(() => treeReconciliation.dispose());
  onDestroy(() => sidebarReconciliation.dispose());
  onDestroy(() => waitingReconciliation.dispose());

  function openInspectorWork(category: WorkCategory = 'files', sessionId?: string, focus?: WorkInitialFocus): void {
    cancelActivityOverviewDemand();
    if (sessionId) {
      explicitFocusedIdentity = workstreamForSession(
        renderedActivityOverview?.workstreams ?? [], sessionId,
      );
      focusedSessionId = canonicalWorkstreamSessionId(
        renderedActivityOverview?.workstreams ?? [],
        sessionId,
      );
    }
    focusedWorkCategory = category;
    focusedWorkInitialFocus = focus ?? null;
    conversationInfoDrawer.mode = 'work';
    conversationInfoDrawer.setOpen(true);
    void category;
  }

  function focusInspectorSession(sessionId: string, scopeOverride?: TimelineScope): void {
    const canonicalSessionId = canonicalWorkstreamSessionId(
      renderedActivityOverview?.workstreams ?? [],
      sessionId,
    ) ?? sessionId;
    explicitFocusedIdentity = workstreamForSession(
      renderedActivityOverview?.workstreams ?? [], canonicalSessionId,
    ) ?? (selectedChildWorkstream?.session_id === canonicalSessionId ? selectedChildWorkstream : null);
    focusedSessionId = canonicalSessionId;
    if (!explicitFocusedIdentity) {
      void focusedSessionIdentityLoader.resolve(canonicalSessionId)
        .then((identity) => {
          if (!identity || focusedSessionId !== canonicalSessionId) return;
          explicitFocusedIdentity = identity;
          if (childView?.sessionId === canonicalSessionId) {
            selectedChildWorkstream = identity;
          }
        })
        .catch(() => {
          // Ownership and availability failures remain fail-closed while the
          // exact transcript and authoritative tree reconcile independently.
        });
    }
    const nextScope = scopeOverride ?? resolvedWorkSubtreeScope(
      currentConversation?.conversation_id ?? '',
      canonicalSessionId,
      scopedRootActivityOverview?.workstreams ?? [],
      explicitFocusedIdentity,
    );
    if (!nextScope) {
      cancelActivityOverviewLoad();
      activityOverview = null;
      return;
    }
    if (
      !activityOverviewReadState.focused.pending
      || activityOverviewReadState.focused.scopeKey !== nextScope.key
    ) {
      cancelActivityOverviewLoad();
    }
    activityOverview = getActivityOverview(nextScope);
    const conversationId = nextScope.conversation_id;
    sessionInfo = conversationId ? getSessionInfo(conversationId, canonicalSessionId) : null;
    applyCachedSessionDiagnostics(canonicalSessionId, sessionInfo);
    void tick().then(() => loadVisibleActivityOverview());
  }

  $effect(() => {
    const scopeKey = inspectorScope?.key;
    const conversationScopeKey = treeRootConversationId
      ? conversationTimelineScope(treeRootConversationId).key
      : null;
    const fallbackScopeKey = currentConversation?.conversation_id
      ? conversationTimelineScope(currentConversation.conversation_id).key
      : null;
    if (typeof window === 'undefined' || !scopeKey) return;
    const invalidateOverview = (event: Event): void => {
      const detail = (event as CustomEvent<{
        scopeKey?: string;
        workRevision?: number;
        overviewAdvanced?: boolean;
      }>).detail;
      const invalidatedKey = detail?.scopeKey;
      if (!workInvalidationTouchesTree(
        invalidatedKey,
        scopeKey,
        conversationScopeKey,
        scopedRootActivityOverview?.workstreams ?? [],
      )) return;
      let overviewAdvanced = detail?.overviewAdvanced !== false;
      for (const affectedScopeKey of activityOverviewInvalidationScopeKeys(
        scopeKey,
        conversationScopeKey,
        fallbackScopeKey,
      )) {
        if (!affectedScopeKey || affectedScopeKey === invalidatedKey) continue;
        overviewAdvanced = invalidateActivityOverview(
          affectedScopeKey,
          detail?.workRevision,
        ) || overviewAdvanced;
      }
      if (!overviewAdvanced) return;
      if (document.hidden) return;
      scheduleTreeRefresh();
    };
    window.addEventListener('cognis:work-invalidated', invalidateOverview);
    return () => window.removeEventListener('cognis:work-invalidated', invalidateOverview);
  });

  function toggleWorkView(event?: MouseEvent): void {
    if (headerInfoOpen && headerInfoMode === 'work') {
      closeHeaderInfo();
      return;
    }
    headerInfoTrigger = event?.currentTarget instanceof HTMLElement
      ? event.currentTarget
      : document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelActivityOverviewDemand();
    conversationInfoDrawer.mode = 'work';
    conversationInfoDrawer.setOpen(true);
  }

  function toggleContextInfo(event?: MouseEvent): void {
    if (headerInfoOpen && headerInfoMode === 'context') {
      closeHeaderInfo();
      return;
    }
    headerInfoTrigger = event?.currentTarget instanceof HTMLElement
      ? event.currentTarget
      : document.activeElement instanceof HTMLElement ? document.activeElement : null;
    conversationInfoDrawer.mode = 'overview';
    conversationInfoDrawer.setOpen(true);
    if (!sessionInfo) {
      void loadSessionInfo();
    }
    loadVisibleActivityOverview();
  }

  function closeHeaderInfo(restoreFocus = true): void {
    cancelActivityOverviewDemand();
    conversationInfoDrawer.close();
    const trigger = headerInfoTrigger;
    headerInfoTrigger = null;
    if (!restoreFocus) return;
    requestAnimationFrame(() => {
      trigger?.focus();
    });
  }

  $effect(() => {
    const conversationId = currentConversation?.conversation_id ?? null;
    const sessionId = currentConversation?.active_session_id ?? null;
    if (conversationId === headerInfoConversationId) return;
    childNavigationGeneration += 1;
    cancelScheduledTreeRefresh();
    cancelRootOverviewLoad();
    cancelActivityOverviewLoad();
    headerInfoConversationId = conversationId;
    invalidateSessionInfo();
    focusedSessionId = null;
    const rootScope = conversationId ? conversationTimelineScope(conversationId) : null;
    rootActivityOverview = rootScope ? getActivityOverview(rootScope) : null;
     activityOverview = rootActivityOverview;
     childView = null;
     selectedChildWorkstream = null;
    if (conversationId && sessionId && headerInfoOpen && headerInfoMode === 'session') {
      void untrack(() => loadSessionInfo());
    }
    if (conversationId && activityOverviewIsVisible(headerInfoOpen, headerInfoMode)) {
      untrack(() => loadVisibleActivityOverview());
    }
  });

  function applyQueuedMessageSnapshot(messages: QueuedMessage[], count = messages.length): void {
    chatV2Store.replaceQueue({ messages, queued_count: count });
    const visibleMessages = chatV2Store.visibleQueue.messages as QueuedMessage[];
    // Optimistic local user messages are reconciled by the Chat v2 store; only
    // the persisted optimistic drafts need clearing here.
    for (const message of visibleMessages) {
      if (message.client_message_id) {
        removeOptimisticUserMessageDraft(directQuestionDraftConversationId() ?? routeConversationId, message.client_message_id);
      }
    }
    const liveQueueIds = new Set(visibleMessages.map((message) => message.queue_id));
    queueExpandedIds = queueExpandedIds.filter((queueId) => liveQueueIds.has(queueId));
    const editedMessage = queueEditingId
      ? visibleMessages.find((message) => message.queue_id === queueEditingId)
      : null;
    if (queueEditingId && (!editedMessage || editedMessage.status === 'committing')) {
      queueEditingId = null;
      queueEditContent = '';
      queueEditError = '';
    }
    applyChatV2StateToTimeline();
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
    const conversationGeneration = conversationLoadRequestId;
    const queuedMessage = queuedMessages.find((message) => message.queue_id === queueId);
    const confirmed = await confirmAction({
      title: 'Delete queued message?',
      message: 'This message will not be sent. Other queued messages will keep their current order.',
      confirmLabel: 'Delete',
      variant: 'danger'
    });
    if (
      !confirmed
      || currentConversation?.conversation_id !== conversationId
      || conversationLoadRequestId !== conversationGeneration
    ) return;
    queueBusyId = queueId;
    try {
      const response = await chatV2Api.deleteQueuedMessage(conversationId, queueId, {
        client_txn_id: crypto.randomUUID()
      });
      if (!shouldApplyChatMutationResponse(
        conversationId,
        currentConversation?.conversation_id ?? null,
        chatV2Store.snapshot.conversationId,
      )) return;
      chatV2Store.deleteQueuedAdmission(queueId, queuedMessage?.client_message_id);
      chatV2Store.applyQueueMutation(response);
      applyChatV2StateToTimeline();
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error');
      await recoverChatV2Snapshot(conversationId);
    } finally {
      queueBusyId = null;
    }
  }

  function startQueuedMessageEdit(message: QueuedMessage): void {
    if (!message.can_edit || message.status === 'committing') return;
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
    const queuedMessage = queuedMessages.find((message) => message.queue_id === queueId);
    if (!queuedMessage?.can_edit || queuedMessage.status === 'committing') {
      cancelQueuedMessageEdit();
      return;
    }
    queueBusyId = queueId;
    queueEditError = '';

    try {
      const response = await chatV2Api.updateQueuedMessage(conversationId, queueId, {
        client_txn_id: crypto.randomUUID(),
        content,
      });
      if (!shouldApplyChatMutationResponse(
        conversationId,
        currentConversation?.conversation_id ?? null,
        chatV2Store.snapshot.conversationId,
      )) return;
      chatV2Store.updateQueuedAdmission(queueId, content, queuedMessage);
      chatV2Store.applyQueueMutation(response);
      applyChatV2StateToTimeline();
      if (queueEditingId === queueId) {
        cancelQueuedMessageEdit();
      }
    } catch (caughtError) {
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
  let diagnosticsOwnerSessionId = $state<string | null>(null);
  const NON_TERMINAL_SESSION_STATES = new Set(['active', 'idle', 'running']);

  let pendingDirectQuestion = $state<PendingDirectQuestion | null>(null);
  let pendingDirectQuestionVisible = $derived(
    Boolean(pendingDirectQuestion)
    && managedInteractionVisibleInScope(
      pendingDirectQuestion?.managedOrigin?.originConversationId,
      selectedManagedChildConversationId,
    ),
  );
  let directQuestionAnswers = $state<QuestionDraftAnswers>({});
  let directQuestionPageIndex = $state(0);
  let directQuestionCollapsed = $state(false);
  let pendingCredentialRequest = $state<Notification | null>(null);
  let directQuestionSubmitting = $state(false);
  let directQuestionSubmittingNotificationId = $state<string | null>(null);
  let locallySettledDirectQuestionIds = $state<Set<string>>(new Set());
  let authoritativelySettledDirectQuestionIds = $state<Set<string>>(new Set());
  // Internal submit/cancel marker hides the pending form while REST resolves.
  // Completed responses are displayed only in the timeline, not the composer.
  // Kept notification-scoped so a delayed terminal event for a different
  // notification cannot clear or be mistaken for this one. Cleared whenever
  // a genuinely new/different pending question replaces the acked one.
  let directQuestionAck = $state<DirectQuestionAck | null>(null);
  let ongoingWorkDrawerOpen = $state(false);
  let retainedChatTodos = $state<ChatTodo[]>([]);
  let backendConversationState = $state<ConversationStateEnvelope | null>(null);

  const sessionIds = new Set<string>();

  let unsubscribeWs: (() => void) | null = null;
   let visibilityHandler: (() => void) | null = null;
  let focusHandler: (() => void) | null = null;
  let pageShowHandler: ((event: PageTransitionEvent) => void) | null = null;
  let onlineHandler: (() => void) | null = null;
  let pageHideHandler: (() => void) | null = null;
  let foregroundSyncTimer: number | null = null;
  let historySectionDayTimer: number | null = null;
  let conversationLoadRequestId = 0;
  let olderMessagesAbortController: AbortController | null = null;
  onDestroy(() => olderMessagesAbortController?.abort());
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
    // Reconcile whenever the user opted in on this device, even if the last
    // check found the subscription gone: it is silently recreated.
    if (!wantsWebPush()) return;
    pushSubscriptionKnownEnabled = await reconcileWebPushSubscription();
    const status = await api.push.status().catch(() => null);
    pushDeliveryError = status?.last_error ?? '';
  }

  function isReadOnly(conversation: Conversation | null): boolean {
    if (!conversation) return true;
    if (conversation.is_readonly_for_caller) return true;
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
    return activeRootSessionLineageIds(
      sessions,
      currentConversation?.active_session_id,
    ).size;
  }

  function backingSessionBadgeLabel(): string | null {
    const count = backingSessionCount();
    return count > 1 ? `${count} backing sessions` : null;
  }

  let backingSessionBadge = $derived(backingSessionBadgeLabel());

  let activeSessionLineage = $derived(
    activeRootSessionLineageIds(sessions, currentConversation?.active_session_id),
  );

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
    managedOriginSource?: Parameters<typeof sanitizeManagedQuestionOrigin>[0],
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
      kind,
      managedOrigin: sanitizeManagedQuestionOrigin(managedOriginSource),
    };
  }

  function pendingDirectQuestionFromQuestionSet(
    notificationId: string,
    stepName: string | undefined,
    questions: unknown,
    context: unknown,
    managedOriginSource?: Parameters<typeof sanitizeManagedQuestionOrigin>[0],
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
      managedOriginSource,
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

  function markDirectQuestionSettledLocally(notificationId: string): void {
    const next = new Set(locallySettledDirectQuestionIds);
    next.add(notificationId);
    locallySettledDirectQuestionIds = next;
  }

  function markDirectQuestionSettledAuthoritatively(notificationId: string): void {
    markDirectQuestionSettledLocally(notificationId);
    const next = new Set(authoritativelySettledDirectQuestionIds);
    next.add(notificationId);
    authoritativelySettledDirectQuestionIds = next;
  }

  function beginDirectQuestionSubmission(notificationId: string): void {
    directQuestionSubmitting = true;
    directQuestionSubmittingNotificationId = notificationId;
  }

  function clearDirectQuestionSubmission(notificationId?: string): void {
    if (
      notificationId
      && directQuestionSubmittingNotificationId
      && directQuestionSubmittingNotificationId !== notificationId
    ) {
      return;
    }
    directQuestionSubmitting = false;
    directQuestionSubmittingNotificationId = null;
  }

  function unmarkDirectQuestionSettledLocally(notificationId: string): void {
    if (!locallySettledDirectQuestionIds.has(notificationId)) return;
    const next = new Set(locallySettledDirectQuestionIds);
    next.delete(notificationId);
    locallySettledDirectQuestionIds = next;
  }

  function settleDirectQuestionLocally(notificationId: string): void {
    const wasPendingQuestion = pendingDirectQuestion?.notificationId === notificationId;
    if (wasPendingQuestion) {
      clearActiveDirectQuestionDraft();
      pendingDirectQuestion = null;
      directQuestionAnswers = {};
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
    }
    clearDirectQuestionSubmission(notificationId);
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

  function notificationManagedOriginSource(notification: Notification): {
    managed_conversation_title?: string | null;
    managed_target_agent_id?: string | null;
    managed_origin_conversation_id?: string | null;
  } {
    const payload = notification.payload as Record<string, unknown>;
    return {
      managed_conversation_title: payload.managed_conversation_title as string | null | undefined,
      managed_target_agent_id: payload.managed_target_agent_id as string | null | undefined,
      managed_origin_conversation_id: payload.managed_origin_conversation_id as string | null | undefined,
    };
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
      notificationManagedOriginSource(notification),
      );
    }
    return pendingDirectQuestionFromQuestionSet(
      notification.notification_id,
      notification.step_name ?? undefined,
      notification.payload.questions,
      notification.payload.context,
      notificationManagedOriginSource(notification),
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
  let projectedConversationWork = $derived.by(() => (
    directChildBackgroundWork(backgroundWork.items, activeSessionLineage)
  ));
  let currentCycleWork = $derived.by(() => (
    currentConversation
      ? currentCycleDelegations(
          chatV2Store.visibleItems,
          chatV2Store.cycleStates,
          currentActiveTurnId,
          currentConversation.conversation_id,
        )
      : []
  ));
  let currentConversationWork = $derived.by(() => (
    mergeCurrentCycleDelegations(projectedConversationWork, currentCycleWork)
  ));
  let shouldShowOngoingWorkDrawer = $derived(
    chatTodos.length > 0 || currentConversationWork.length > 0
  );
  let currentConversationHasActiveBackgroundWork = $derived(
    currentConversationWork.some(backgroundWorkItemIsRunning)
  );
  const observedBackgroundCommandCalls = new Set<string>();
  $effect(() => {
    for (const item of chatV2Store.visibleItems) {
      if (
        item.kind !== 'tool_call'
        || item.tool_name !== 'bash'
        || item.arguments?.run_in_background !== true
        || item.result_preview == null
        || observedBackgroundCommandCalls.has(item.call_id)
      ) continue;
      observedBackgroundCommandCalls.add(item.call_id);
      void resyncSidebarData('background-command-started', { force: true });
    }
  });
  let chatTodoCounts = $derived.by(() => ({
    inProgress: activeChatTodos.filter((todo) => todo.status === 'in_progress').length,
    pending: activeChatTodos.filter((todo) => todo.status === 'pending').length,
  }));

  function conversationHasActiveBackgroundWork(conversationId: string): boolean {
    return backgroundWork.items.some((item) => (
      item.controller_conversation_id === conversationId
      && backgroundWorkItemIsRunning(item)
    ));
  }

  function isBackgroundTargetConversation(conversationId: string): boolean {
    return backgroundWork.items.some((item) => (
      item.kind === 'managed_conversation'
      && item.target_conversation_id === conversationId
    ));
  }

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
      const previousStateVersion = backendConversationState?.state_version ?? 0;
      if (state.state_version > previousStateVersion) {
        beginPendingNotificationRefresh(conversationId);
      }
      backendConversationState = state;
      const pendingFlags = conversationPendingSnapshotFlags(state);
      turnInProgress = conversationStateTurnActivity({
        currentActive: turnInProgress,
        snapshotActive: Boolean(state.active_turn?.has_active_turn),
        hasPendingInput: pendingFlags.hasAnyPendingInput,
        chatV2OwnsConversation,
      });
      if (!pendingFlags.hasDirectQuestion) {
        const settledQuestionId = pendingDirectQuestion?.notificationId;
        if (
          settledQuestionId
          && canonicalStateCanSettleDirectQuestion(state.state_version, pendingDirectQuestion)
        ) {
          markDirectQuestionSettledAuthoritatively(settledQuestionId);
          pendingDirectQuestion = null;
          clearDirectQuestionSubmission(settledQuestionId);
        }
      } else {
        const canonicalSummary = state.pending?.auth_challenge ?? state.pending?.pending_input;
        const canonicalQuestion = directQuestionFromPendingSummary(canonicalSummary);
        if (
          canonicalQuestion
          && !locallySettledDirectQuestionIds.has(canonicalQuestion.notificationId)
        ) {
          canonicalQuestion.observedStateVersion = state.state_version;
          if (shouldResetPendingDirectQuestionForm(
            pendingDirectQuestion?.notificationId,
            canonicalQuestion.notificationId,
          )) {
            directQuestionAnswers = restoreDirectQuestionDraft(canonicalQuestion);
            directQuestionPageIndex = 0;
            directQuestionCollapsed = false;
            clearDirectQuestionSubmission();
            directQuestionAck = null;
          }
          pendingDirectQuestion = canonicalQuestion;
        } else if (
          canonicalSummary?.notification_type === 'step_question'
          && canonicalSummary.task_id
          && canonicalStateCanSettleDirectQuestion(state.state_version, pendingDirectQuestion)
        ) {
          if (pendingDirectQuestion?.notificationId) {
            markDirectQuestionSettledAuthoritatively(pendingDirectQuestion.notificationId);
          }
          pendingDirectQuestion = null;
          directQuestionAnswers = {};
          directQuestionPageIndex = 0;
          directQuestionCollapsed = false;
          clearDirectQuestionSubmission();
        }
      }
      if (!pendingFlags.hasCredentialRequest) {
        pendingCredentialRequest = null;
      }
      if (!pendingFlags.hasEscalation) {
        escalations = [];
        escalationBusyCallId = null;
        escalationError = '';
        escalationResolutionPending = null;
        escalationSettledAck = null;
        locallySettledEscalationCallIds = new Set();
        stopEscalationCountdown();
      } else {
        // Hydrate the active escalation directly from the canonical
        // conversation-state snapshot/delta. This must not depend on a
        // legacy `escalation` push frame having arrived: a missed frame
        // (reconnect, cold load, background tab) must not delay or hide
        // the approval prompt. Push events and the REST catch-up path
        // remain compatible latency/recovery paths and are deduplicated
        // by call_id below.
        const hydrated = escalationFromPendingSummary(state.pending?.escalation);
        if (hydrated && !isEscalationExpired(hydrated)) {
          const excludedCallIds = new Set(locallySettledEscalationCallIds);
          if (escalationResolutionPending?.call_id) {
            excludedCallIds.add(escalationResolutionPending.call_id);
          }
          const nextEscalations = mergeHydratedEscalation(
            escalations,
            hydrated,
            excludedCallIds,
          );
          if (nextEscalations !== escalations) {
            escalations = nextEscalations;
            startEscalationCountdown();
          }
        }
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
      void recoverChatV2Canonical(
        event.conversation_id,
        conversationLoadRequestId,
      );
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
        const mergedConversation = mergeConversationPreservingActivity(currentConversation, conversation);
        currentConversation = mergedConversation;
        if (!chatV2OwnsActiveConversation(conversation.conversation_id)) {
          turnInProgress = mergedConversation.has_active_turn;
          activeTurnChatMode = mergedConversation.has_active_turn
            ? normalizeChatModeTone(mergedConversation.active_turn_chat_mode)
            : 'default';
        }
      }
      if (isAgentDirectConversation(conversation)) {
        patchAgentDirectChat(conversation);
      }
    }
    conversations = mergeSidebarConversationRows(conversations, items, { reset });
    reconcileOpenConversationRuntime();
  }

  function reconcileOpenConversationRuntime(): void {
    if (!currentConversation || !chatV2OwnsActiveConversation(currentConversation.conversation_id)) return;
    const pendingAdmission = awaitingAssistantStart
      ? optimisticConversationTurnPatch(activeTurnChatMode)
      : null;
    const reconcile = (row: Conversation) =>
      reconcileConversationWithChatRuntime(row, chatV2Store.snapshot, pendingAdmission);
    const reconciled = reconcile(currentConversation);
    if (reconciled.has_active_turn !== currentConversation.has_active_turn) {
      // List/sidebar revisions and runtime fences are different clocks.
      // Resolve disagreements through the runtime owner, never metadata time.
      scheduleChatV2CanonicalRecovery(currentConversation.conversation_id, { immediate: true });
    }
    currentConversation = reconciled;
    conversations = conversations.map(reconcile);
    agentDirectChats = agentDirectChats.map((item) => ({
      ...item, conversation: reconcile(item.conversation),
    }));
    turnInProgress = reconciled.has_active_turn;
    activeTurnChatMode = reconciled.has_active_turn
      ? normalizeChatModeTone(reconciled.active_turn_chat_mode)
      : 'default';
  }

  function patchAgentDirectChat(conversation: Conversation): void {
    if (!isAgentDirectConversation(conversation) && !appContactBindingId(conversation)) return;
    const agent = agents.find((candidate) => candidate.agent_id === conversation.agent_id);
    if (!agent) return;
    mergeAgentDirectChats([{ agent, conversation }]);
  }

  function mergeAgentDirectChats(items: AgentDirectChat[]): void {
    const merged = new Map(agentDirectChats.map((item) => [item.agent.agent_id, item]));
    for (const item of items) {
      applyConversationStateSnapshot(item.conversation.conversation_state, { patchConversationRows: false });
      const existing = merged.get(item.agent.agent_id);
      if (existing && !shouldReplaceAgentSidebarConversation(existing.conversation, item.conversation)) continue;
      merged.set(item.agent.agent_id, existing?.conversation.conversation_id === item.conversation.conversation_id
        ? {
            agent: item.agent,
            conversation: mergeConversationPreservingActivity(existing.conversation, item.conversation),
          }
        : item);
    }
    agentDirectChats = sortAgentDirectChats([...merged.values()]);
    reconcileOpenConversationRuntime();
  }

  function observePendingNotificationServerPush(conversationId: string | null | undefined): void {
    if (!conversationId) return;
    pendingNotificationEpoch += 1;
    pendingNotificationEpochByConversation.set(conversationId, pendingNotificationEpoch);
  }

  const conversationRuntimeLifecycle = new Map<string, {
    updatedAt: string | null;
    activeTurnId: string | null;
  }>();
  function orderedConversationRuntimePatch(
    conversationId: string,
    event: Parameters<typeof orderedConversationUpdatedRowPatch>[0],
  ): ReturnType<typeof orderedConversationUpdatedRowPatch> {
    const currentState = conversationRuntimeLifecycle.get(conversationId) ?? {
      updatedAt: null,
      activeTurnId: null,
    };
    const result = orderedConversationUpdatedRowPatch(event, currentState);
    conversationRuntimeLifecycle.set(conversationId, result.state);
    return result;
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
    updateWaitingCaches(conversationId, item => mergeConversationRowPatch(item, patch));
    if (Object.prototype.hasOwnProperty.call(patch, 'pending_notification_types') && options.pendingNotificationSource === 'server') {
      observePendingNotificationServerPush(conversationId);
    }
    const index = conversations.findIndex((conversation) => conversation.conversation_id === conversationId);
    const now = new Date().toISOString();
    waitingConversations = waitingConversations.map((item) => item.conversation_id === conversationId
      ? mergeConversationRowPatch(item, {
          ...patch,
          ...(options.touchUpdatedAt ? { updated_at: patch.updated_at ?? now } : {}),
          ...(options.touchLastMessageAt ? { last_message_at: patch.last_message_at ?? now } : {}),
        })
      : item);
    if (index < 0) {
      if (waitingConversations.some((item) => item.conversation_id === conversationId)) {
        rememberSidebarProjection();
        return;
      }
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
    { markCurrentDeleted = false, canonicalRemoval = false }: {
      markCurrentDeleted?: boolean; canonicalRemoval?: boolean;
    } = {}
  ): void {
    // Filter eviction is local; only a canonical tombstone removes all scopes.
    if (canonicalRemoval) updateWaitingCaches(conversationId, () => null);
    const previousConversationCount = conversations.length;
    const previousWaitingCount = waitingConversations.length;
    const previousDirectCount = agentDirectChats.length;
    conversations = removeSidebarConversationRow(conversations, conversationId);
    waitingConversations = removeSidebarConversationRow(waitingConversations, conversationId);
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
    if (previousConversationCount !== conversations.length || previousDirectCount !== agentDirectChats.length
      || previousWaitingCount !== waitingConversations.length) {
      rememberSidebarProjection();
    }
  }

  function applySidebarConversationUpsert(
    conversation: Conversation,
    revision?: string,
  ): void {
    const conversationId = conversation.conversation_id;
    const admission = sidebarRevisionAdmission.admitPush(conversationId, revision);
    if (admission.reconcile) scheduleSidebarReconciliation('sidebar-upsert-gap');
    if (!admission.apply) return;
    sidebarNetworkProjectionApplied = true;
    updateWaitingCaches(conversationId, item => mergeAuthoritativeSidebarConversation(item, conversation));
    waitingConversations = waitingConversations.map((item) => item.conversation_id === conversationId
      ? mergeAuthoritativeSidebarConversation(item, conversation)
      : item);
    observePendingNotificationServerPush(conversationId);
    applyConversationStateSnapshot(conversation.conversation_state, { patchConversationRows: false });
    if (currentConversation?.conversation_id === conversationId) {
      currentConversation = mergeAuthoritativeSidebarConversation(currentConversation, conversation);
      if (!chatV2OwnsActiveConversation(conversationId)) {
        turnInProgress = currentConversation.has_active_turn;
        activeTurnChatMode = currentConversation.has_active_turn
          ? normalizeChatModeTone(currentConversation.active_turn_chat_mode)
          : 'default';
      }
    }
    if (conversationMatchesSidebarProjectionFilter(conversation, currentSidebarProjectionFilter())) {
      if (isAgentDirectConversation(conversation) || appContactBindingId(conversation)) {
        const existingDirect = agentDirectChats.find(
          (item) => item.conversation.conversation_id === conversationId
        );
        patchAgentDirectChat(
          mergeAuthoritativeSidebarConversation(existingDirect?.conversation, conversation)
        );
      } else {
        const existingIndex = conversations.findIndex(
          (item) => item.conversation_id === conversationId
        );
        const authoritative = mergeAuthoritativeSidebarConversation(
          existingIndex >= 0 ? conversations[existingIndex] : null,
          conversation,
        );
        const next = [...conversations];
        if (existingIndex >= 0) next[existingIndex] = authoritative;
        else next.push(authoritative);
        conversations = sortConversationsByActivity(next);
      }
      rememberSidebarProjection();
    } else {
      removeConversationFromSidebar(conversationId);
    }
    if (conversation.has_unread) {
      void observedConversationReads.observe({
        conversationId,
        version: conversation.last_message_at ?? conversation.updated_at ?? revision ?? 'initial',
        readThrough: conversation.last_message_at ?? null,
      });
    }
    reconcileOpenConversationRuntime();
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
      timelineController?.persist();
      void timelineController?.reconcileAdmissions();
      if (conversationViewSaveTimer !== null && typeof window !== 'undefined') {
        window.clearTimeout(conversationViewSaveTimer);
        conversationViewSaveTimer = null;
      }
      const scopeKey = conversationTimelineScope(currentConversation.conversation_id).key;
      const capturedAnchor = userScrolledUp ? captureTimelineAnchor() : null;
      const viewportAnchor = capturedAnchor
        ? {
            rowKey: capturedAnchor.key,
            offsetTop: capturedAnchor.top,
            scrollTop: capturedAnchor.scrollTop,
          }
        : null;
      timelineViewportStates.save(scopeKey, {
        mode: userScrolledUp ? 'paused' : 'following',
        anchor: viewportAnchor,
        scrollTop: timelineEl?.scrollTop ?? lastTimelineScrollTop,
      });
      touchConversationViewCache(currentConversation.conversation_id, {
        conversation: { ...currentConversation },
        sessions: sessions.map((session) => ({ ...session })),
        // Keep the live Chat v2 overlay when switching within this tab. A
        // snapshot only carries scheduler state, not an idle-checkpoint
        // compaction item, so discarding this overlay made the compaction card
        // and the optimistic user message disappear until canonical history
        // eventually caught up.
        queuedCount,
        queuedMessages: queuedMessages.map((message) => ({ ...message })),
        contextUsage,
        sessionInfo: sessionInfo ? { ...sessionInfo } : null,
        visibleStartIndex,
        scrollTop: timelineEl?.scrollTop ?? lastTimelineScrollTop,
        userScrolledUp,
        viewportAnchor,
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
      persistConversationView(currentConversation.conversation_id);
    } catch (error) {
      console.warn('Failed to save conversation view cache', error);
    }
  }

  // Device (IndexedDB) copy of the in-memory view cache, used for instant
  // cold starts and offline reading. Live runtime overlays are settled so a
  // relaunch never shows a stale "streaming" turn; reconnect/sync refreshes.
  const PERSISTED_VIEW_HYDRATE_BUDGET_MS = 600;

  /**
   * Copy of a non-current (e.g. preloaded) view's state with live runtime
   * overlays settled, so a relaunch never restores a finished turn as live.
   */
  function persistConversationView(conversationId: string): void {
    const entry = conversationViewCache.get(conversationId);
    if (!entry || isReadOnly(entry.conversation)) return;
    try {
      const persisted: ConversationViewCacheEntry = JSON.parse(JSON.stringify({
        ...entry,
        turnInProgress: false,
        awaitingAssistantStart: false,
        userScrolledUp: false,
        viewportAnchor: null,
      }));
       void offlineCache.put('conversation-view', conversationId, persisted, pageDataOwner);
    } catch (error) {
      console.warn('Failed to persist conversation view', error);
    }
  }

  async function hydratePersistedConversationView(conversationId: string): Promise<boolean> {
    // Hydration is also used by background preloads. Only an accepted foreground
    // restore may bind the controller and replace the visible store/scroll owner.
    const acquired = acquireTimeline(conversationTimelineScope(conversationId));
    const controller = acquired.controller;
    try {
      await controller.hydrate();
      const persisted = await Promise.race([
        offlineCache.get<ConversationViewCacheEntry>('conversation-view', conversationId, pageDataOwner),
        new Promise<null>((resolve) => setTimeout(() => resolve(null), PERSISTED_VIEW_HYDRATE_BUDGET_MS)),
      ]);
      if (!controller.current) throw controller.ownerSignal.reason ?? new TimelineOwnershipError();
      if (
        !pageAlive
        || !persisted
        || conversationViewCache.has(conversationId)
        || persisted.conversation?.conversation_id !== conversationId
      ) return false;
      const { chatV2State: _legacy, ...metadata } = persisted as ConversationViewCacheEntry & { chatV2State?: unknown };
      touchConversationViewCache(conversationId, metadata);
      return true;
    } finally {
      acquired.release();
    }
  }

  // ---- Background preloading -------------------------------------------
  // Conversations with activity are brought up to date in the background
  // (cursor sync on the cached view, snapshot when uncached or reset) so
  // opening them paints current content without a loading/refresh state.
  const PRELOAD_IDLE_WARM_COUNT = 5;

  function sidebarConversation(conversationId: string): Conversation | null {
    return waitingConversations.find((item) => item.conversation_id === conversationId)
      ?? conversations.find((item) => item.conversation_id === conversationId)
      ?? agentDirectChats.find((item) => item.conversation.conversation_id === conversationId)?.conversation
      ?? null;
  }

  async function preloadConversationView(conversationId: string, signal: AbortSignal): Promise<void> {
    if (conversationId === currentConversation?.conversation_id) return;
    const listed = sidebarConversation(conversationId);
    if (!listed || isReadOnly(listed) || appContactBindingId(listed)) return;
    if (!conversationViewCache.has(conversationId)) {
      await hydratePersistedConversationView(conversationId);
    }
    const existing = conversationViewCache.get(conversationId) ?? null;
    const acquired = acquireTimeline(conversationTimelineScope(conversationId));
    const store = acquired.controller.store;
    try { await acquired.controller.load({ signal }); } finally { acquired.release(); }
    const olderCursor = store.snapshot.beforeCursor;
    const hasOlder = store.snapshot.hasMoreBefore;
    // The user may have opened it meanwhile; its own load path owns it now.
    if (signal.aborted || conversationId === currentConversation?.conversation_id) return;
    const latest = conversationViewCache.get(conversationId);
    if (latest && latest !== existing) return;
    const state = store.snapshot;
    const projection = deriveChatV2ViewProjection(state);
    touchConversationViewCache(conversationId, {
      conversation: { ...(sidebarConversation(conversationId) ?? listed) },
      sessions: existing?.sessions ?? [],
      queuedCount: existing?.queuedCount ?? 0,
      queuedMessages: existing?.queuedMessages ?? [],
      contextUsage: existing?.contextUsage ?? null,
      sessionInfo: existing?.sessionInfo ?? null,
      visibleStartIndex: existing?.userScrolledUp ? existing.visibleStartIndex : 0,
      scrollTop: existing?.userScrolledUp ? existing.scrollTop : 0,
      userScrolledUp: existing?.userScrolledUp ?? false,
      viewportAnchor: existing?.userScrolledUp ? existing.viewportAnchor : null,
      turnInProgress: projection.turnInProgress,
      awaitingAssistantStart: false,
      activeSessionLastSeq: existing?.activeSessionLastSeq ?? 0,
      olderMessagesCursor: olderCursor,
      hasOlderMessages: hasOlder,
      activeTurnChatMode: existing?.activeTurnChatMode ?? 'default',
      fetchedAt: Date.now(),
    });
    persistConversationView(conversationId);
  }

  let conversationPreloader = new ConversationPreloadScheduler(preloadConversationView);
  onDestroy(() => conversationPreloader.dispose());
  let idlePreloadWarmed = false;
  $effect(() => {
    if (idlePreloadWarmed || !initialConversationResolved || conversations.length === 0) return;
    idlePreloadWarmed = true;
    scheduleIdlePreloadWarmup();
  });

  /** Back in the foreground: bring chats with running turns up to date. */
  function preloadActiveConversations(): void {
    const currentId = currentConversation?.conversation_id;
    for (const item of conversations) {
      if (item.conversation_id !== currentId && item.has_active_turn) {
        requestActivityPreload(item.conversation_id, true);
      }
    }
  }

  function requestActivityPreload(conversationId: string, activeTurn: boolean): void {
    conversationPreloader.request(conversationId, { priority: 'activity', activeTurn });
  }

  /** After the page settles, warm the most recent conversations when idle. */
  function scheduleIdlePreloadWarmup(): void {
    if (typeof window === 'undefined') return;
    const run = () => {
      const currentId = currentConversation?.conversation_id;
      const candidates = conversations
        .filter((item) => item.conversation_id !== currentId && !isReadOnly(item))
        .filter((item) => {
          const cached = conversationViewCache.get(item.conversation_id);
          return !cached || Date.now() - cached.fetchedAt > 120_000;
        })
        .slice(0, PRELOAD_IDLE_WARM_COUNT);
      for (const item of candidates) {
        conversationPreloader.request(item.conversation_id, { priority: 'idle' });
      }
    };
    const idle = (window as Window & { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number })
      .requestIdleCallback;
    if (idle) idle(run, { timeout: 5_000 });
    else window.setTimeout(run, 3_000);
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
    const canonical = peekTimeline(conversationTimelineScope(conversationId));
    if (!entry || !canonical?.cursor) return null;
    touchConversationViewCache(conversationId, entry);
    activeConversationId = conversationId;
    currentConversation = { ...entry.conversation };
    sessions = entry.sessions.map((session) => ({ ...session }));
    bindTimeline(conversationId);
    // Derive queue/turn-flag/active-turn-id projections through the same
    // projection path used for live snapshots and sync frames
    // (applyChatV2StateToTimeline -> deriveChatV2ViewProjection), instead of
    // re-assigning the cached scalar fields directly. This guarantees
    // currentActiveTurnId (and everything else the projection derives)
    // reflects conversationId's own just-restored state immediately -- never
    // a value left over from whatever conversation was open before the
    // switch, which cached scalar fields alone cannot guarantee if the two
    // ever drifted apart.
    applyChatV2StateToTimeline();
    contextUsage = entry.contextUsage;
    sessionInfo = entry.sessionInfo ? { ...entry.sessionInfo } : null;
    activeSessionLastSeq = entry.activeSessionLastSeq;
    olderMessagesCursor = chatV2Store.snapshot.beforeCursor;
    hasOlderMessages = chatV2Store.snapshot.hasMoreBefore;
    chatV2Lifecycle.acceptSnapshot(conversationTimelineScope(conversationId), chatV2Store.snapshot.cursor!);
    activeTurnChatMode = entry.activeTurnChatMode ?? 'default';
    // The cache entry only carries durable per-conversation view state. Any
    // transient open/send/modal state from whatever conversation was
    // previously active must not bleed into the restored conversation -- this
    // is the same guarantee clearConversationViewState() gives the full
    // (uncached) reload path. None of these are keyed by conversation, so they
    // are always reset on restore rather than restored from cache.
    lastGenerationPerformance = null;
    pendingDirectQuestion = null;
    pendingCredentialRequest = null;
    directQuestionPageIndex = 0;
    directQuestionCollapsed = false;
    directQuestionSubmitting = false;
    directQuestionSubmittingNotificationId = null;
    locallySettledDirectQuestionIds = new Set();
    authoritativelySettledDirectQuestionIds = new Set();
    directQuestionAck = null;
    escalations = [];
    escalationError = '';
    escalationResolutionPending = null;
    escalationSettledAck = null;
    locallySettledEscalationCallIds = new Set();
    lastRecoverableMessage = '';
    lastRecoverableTurnId = null;
    suppressedRecoverableTurnId = null;
     editingTitle = false;
     childView = null;
     selectedChildWorkstream = null;
    const viewportState = timelineViewportTransition?.state ?? {
      mode: entry.userScrolledUp ? 'paused' as const : 'following' as const,
      anchor: entry.viewportAnchor ?? null,
      scrollTop: entry.scrollTop,
    };
    timelineWindow = viewportState.mode === 'paused'
      ? {
          start: Math.min(entry.visibleStartIndex, Math.max(0, renderableVisibleItems.length - TIMELINE_WINDOW_TARGET_ROWS)),
          end: null,
        }
      : reconcileLiveTailWindow(
          { start: entry.visibleStartIndex, end: null },
          renderableVisibleItems.length,
          TIMELINE_WINDOW_TARGET_ROWS,
        );
    userScrolledUp = viewportState.mode === 'paused';
    establishTimelineWindow();
    tailPinned = !userScrolledUp;
    timelineInitialPositionSettled = false;
    initialLoadTimedOut = false;
    mergeConversationList([entry.conversation]);
    return entry;
  }

  function clearConversationViewState(): void {
    timelineViewportStates.invalidateWindow();
    sessions = [];
    sessionIds.clear();
    // Reset the canonical Chat v2 store immediately so the outgoing
    // conversation's content cannot flash into the incoming conversation.
    // Do not reset a timeline retained by another surface.
    chatV2Store = new ChatV2Store();
    timelineWindow = { start: 0, end: null };
    tailWindowRows = TIMELINE_WINDOW_TARGET_ROWS;
    timelineWindowRevision += 1;
    timelineWindowPending = false;
    timelineReadingPosition = null;
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
    directQuestionSubmittingNotificationId = null;
    locallySettledDirectQuestionIds = new Set();
    authoritativelySettledDirectQuestionIds = new Set();
    directQuestionAck = null;
    escalations = [];
    escalationError = '';
    escalationResolutionPending = null;
    escalationSettledAck = null;
    locallySettledEscalationCallIds = new Set();
    lastRecoverableMessage = '';
    lastRecoverableTurnId = null;
    suppressedRecoverableTurnId = null;
     editingTitle = false;
     childView = null;
     selectedChildWorkstream = null;
    // Opening/switching a conversation lands at the tail (latest message).
    // Pinning here makes the post-load refresh auto-scroll to bottom.
    userScrolledUp = false;
    tailPinned = true;
    lastTimelineScrollTop = 0;
    timelineInitialPositionSettled = false;
  }

  function beginTimelineViewportTransition(conversationId: string, generation: number): void {
    const scopeKey = conversationTimelineScope(conversationId).key;
    // Route ownership supersedes retained parent↔child restoration. A pending
    // callback for the old conversation must never write into the new scope.
    parentSurfaceTransitionGeneration += 1;
    parentSurfaceWasHidden = false;
    parentSurfaceAnchor = null;
    timelineViewportTransition = timelineViewportStates.begin(scopeKey, generation);
    timelineInitialPositionSettled = false;
    programmaticScrollTarget = null;
    userScrollIntentUp = false;
    lastTimelineTouchY = null;
  }

  async function settleTimelineViewportTransition(
    conversationId: string,
    generation: number,
  ): Promise<boolean> {
    const scopeKey = conversationTimelineScope(conversationId).key;
    if (!timelineViewportStates.isCurrent(scopeKey, generation)) return false;
    await tick();
    if (
      isStaleConversationLoad(generation)
      || currentConversation?.conversation_id !== conversationId
      || !timelineViewportStates.isCurrent(scopeKey, generation)
      || !timelineEl
    ) return false;

    const state = timelineViewportTransition?.state;
    if (state?.mode === 'paused') {
      writeProgrammaticScrollTop(state.scrollTop);
      if (state.anchor) {
        const row = timelineEl.querySelector<HTMLElement>(
          `[data-timeline-row-key="${cssEscape(state.anchor.rowKey)}"]`,
        );
        if (row) {
          const viewportTop = timelineEl.getBoundingClientRect().top;
          const rowTop = row.getBoundingClientRect().top - viewportTop;
          writeProgrammaticScrollTop(
            anchoredScrollTop({
              currentScrollTop: timelineEl.scrollTop,
              anchorTopBefore: state.anchor.offsetTop,
              anchorTopAfter: rowTop,
            }),
          );
        }
      }
      userScrolledUp = true;
      tailPinned = false;
    } else {
      userScrolledUp = false;
      tailPinned = true;
      writeProgrammaticScrollTop(timelineEl.scrollHeight);
    }
    lastTimelineScrollTop = timelineEl.scrollTop;
    timelineInitialPositionSettled = true;
    timelineViewportStates.settle(scopeKey, generation);
    timelineViewportTransition = null;
    return true;
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
      waitingConversations.find((conversation) => conversation.conversation_id === conversationId)
      ?? conversations.find((conversation) => conversation.conversation_id === conversationId)
      ?? agentDirectChats.find((item) => item.conversation.conversation_id === conversationId)?.conversation
      ?? null
    );
  }

  function markConversationReadLocally(conversationId: string | null | undefined, readAt?: string | null): void {
    if (!conversationId || !hostAllowsObservation()) return;
    const conversation = sidebarConversationById(conversationId);
    const readFloor = conversationReadAcknowledgementAt(conversation, readAt);
    patchConversationInList(conversationId, {
      has_unread: false,
      ...(readFloor ? { last_read_at: readFloor } : {}),
    });
  }

  async function loadConversationPage(reset = false): Promise<void> {
    if (reset) {
      await loadSidebarProjection();
      return;
    }
    const initialKey = sidebarProjectionCacheKey();
    conversationListLoadCount += 1;
    conversationListLoading = true;
    try {
      const response = await api.conversations.list(reset ? null : conversationCursor, {
        contextTypes: selectedChannels.length > 0 ? selectedChannels : null,
        agentIds: selectedAgentIds.length > 0 ? selectedAgentIds : null,
        projectId: selectedProjectId || null,
        includeShared: includeSharedChats,
        status: selectedConversationStatus,
      });
      if (initialKey !== sidebarProjectionCacheKey()) return;
      mergeConversationList(response.items, { reset });
      conversationCursor = response.cursor;
      conversationsHasMore = response.has_more;
    } finally {
      conversationListLoadCount = Math.max(0, conversationListLoadCount - 1);
      conversationListLoading = conversationListLoadCount > 0;
    }
  }

  async function loadConversationTitleSearch(
    query: string,
    reset = true,
    expectedFilterKey = sidebarProjectionCacheKey(),
  ): Promise<void> {
    const normalizedQuery = query.trim();
    if (!normalizedQuery || conversationSearchSubmitted) return;
    const requestId = ++conversationTitleSearchRequestId;
    conversationTitleSearchLoading = true;
    conversationTitleSearchError = '';
    try {
      const response = await api.conversations.list(
        reset ? null : conversationTitleSearchCursor,
        {
          contextTypes: selectedChannels.length > 0 ? selectedChannels : null,
          agentIds: selectedAgentIds.length > 0 ? selectedAgentIds : null,
          projectId: selectedProjectId || null,
          includeShared: includeSharedChats,
          status: selectedConversationStatus,
          query: normalizedQuery,
        },
      );
      if (
        requestId !== conversationTitleSearchRequestId
        || normalizedQuery !== conversationSearch.trim()
        || conversationSearchSubmitted
        || expectedFilterKey !== sidebarProjectionCacheKey()
      ) return;
      if (reset) {
        conversationTitleSearchResults = response.items;
      } else {
        const merged = new Map<string, Conversation>();
        for (const conversation of [...conversationTitleSearchResults, ...response.items]) {
          merged.set(conversation.conversation_id, conversation);
        }
        conversationTitleSearchResults = Array.from(merged.values());
      }
      conversationTitleSearchCursor = response.cursor;
      conversationTitleSearchHasMore = response.has_more;
    } catch (caughtError) {
      if (
        requestId === conversationTitleSearchRequestId
        && normalizedQuery === conversationSearch.trim()
        && !conversationSearchSubmitted
        && expectedFilterKey === sidebarProjectionCacheKey()
      ) {
        if (reset) {
          conversationTitleSearchResults = [];
          conversationTitleSearchCursor = null;
          conversationTitleSearchHasMore = false;
        }
        conversationTitleSearchError = asApiError(caughtError).message;
      }
    } finally {
      if (requestId === conversationTitleSearchRequestId) {
        conversationTitleSearchLoading = false;
      }
    }
  }

  function sidebarProjectionCacheKey(): string {
    return JSON.stringify({
      owner: auth.getSnapshot().user?.email,
      channels: [...selectedChannels].sort(),
      agents: [...selectedAgentIds].sort(),
      project: selectedProjectId,
      includeShared: includeSharedChats,
      status: selectedConversationStatus,
    });
  }

  function currentSidebarProjectionFilter() {
    return {
      selectedChannels,
      selectedAgentIds,
      selectedConversationStatus,
      selectedProjectId,
    };
  }

  function rememberSidebarProjection(key = sidebarProjectionCacheKey()): void {
    // Independent Waiting completion must not cache old-scope sidebar rows
    // under a newly selected scope before that sidebar has loaded.
    if (sidebarSnapshotScope !== key) return;
    rememberSidebarProjectionSnapshot(
      sidebarProjectionCache,
      key,
      {
        agents,
        agent_direct_chats: agentDirectChats,
        waiting_conversations: waitingSnapshotScope === key ? waitingConversations : undefined,
        conversations: {
          items: conversations,
          cursor: conversationCursor,
          has_more: conversationsHasMore,
        },
        context_types: availableChannelTypes,
        sidebar_revision: sidebarRevisionAdmission.authoritativeRestCursor,
        sync_timestamp: lastSidebarSyncTimestamp,
        background_work: backgroundWork,
      },
      SIDEBAR_PROJECTION_CACHE_LIMIT,
    );
    const snapshot = sidebarProjectionCache.get(key);
    if (snapshot) {
      try {
        // Device copy for cold start/offline. Drop the revision so a stale
        // copy can never advance revision admission past the server.
        void offlineCache.put('sidebar', key, JSON.parse(JSON.stringify({
          ...snapshot,
          sidebar_revision: null,
        })), pageDataOwner);
      } catch {
        // Non-serializable or quota issue: device cache is best effort.
      }
    }
  }

  function updateWaitingCaches(
    conversationId: string, update: (row: Conversation) => Conversation | null,
  ): void {
    for (const [key, rows] of waitingSnapshotCache) {
      waitingSnapshotCache.set(key, updateSidebarRow(rows, conversationId, update));
    }
    for (const projection of sidebarProjectionCache.values()) {
      if (projection.waiting_conversations) {
        projection.waiting_conversations = updateSidebarRow(
          projection.waiting_conversations, conversationId, update,
        );
      }
    }
  }

  function applySidebarProjection(
    projection: SidebarProjection,
    options: { reset?: boolean; revisionAdmitted?: boolean } = {},
  ): boolean {
    if (!options.revisionAdmitted) {
      const admission = sidebarRevisionAdmission.admitProjection(projection.sidebar_revision);
      if (!admission.apply) return false;
    }
    const reset = options.reset ?? true;
    sidebarSnapshotScope = sidebarProjectionCacheKey();
    const protectedIds = sidebarRevisionAdmission.newerConversationIds(projection.sidebar_revision);
    const protectedDirectChats = agentDirectChats.filter(
      item => protectedIds.has(item.conversation.conversation_id)
        && conversationMatchesSidebarProjectionFilter(item.conversation, currentSidebarProjectionFilter()),
    );
    const incomingDirectChats = projection.agent_direct_chats.filter(
      item => !protectedIds.has(item.conversation.conversation_id),
    );
    sidebarNetworkProjectionApplied = true;
    if (reset || projection.agents.length > 0) agents = projection.agents;
    if (reset) {
      agentDirectChats = sortAgentDirectChats([...incomingDirectChats, ...protectedDirectChats]);
    } else if (projection.agent_direct_chats.length > 0) {
      mergeAgentDirectChats(incomingDirectChats);
    }
    if (reset || projection.context_types.length > 0) applyAvailableChannelTypes(projection.context_types);
    backgroundWork = mergeSidebarBackgroundWork(backgroundWork, projection);
    mergeConversationList(overlaySidebarRows(
      projection.conversations.items, reset ? conversations.filter(
        item => conversationMatchesSidebarProjectionFilter(item, currentSidebarProjectionFilter()),
      ) : [], protectedIds,
    ), { reset });
    for (const removedId of projection.removed_conversation_ids ?? []) {
      if (!protectedIds.has(removedId)) removeConversationFromSidebar(removedId);
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
    sidebarRevisionAdmission.recordProjection(
      projection.sidebar_revision,
      [
        ...projection.conversations.items.map((item) => item.conversation_id),
        ...projection.agent_direct_chats.map((item) => item.conversation.conversation_id),
        ...(projection.removed_conversation_ids ?? []),
      ],
    );
    return true;
  }

  // True once any live (memory/network) projection has been applied. A
  // device-cached sidebar is only a cold-start placeholder before that.
  let sidebarNetworkProjectionApplied = false;
  const SIDEBAR_DEVICE_HYDRATE_BUDGET_MS = 800;
  let sidebarDeviceHydration: Promise<boolean> | null = null;

  /**
   * Paint the device-cached sidebar on cold start. Independent of network
   * refresh epochs (reconnect/invalidation resyncs must not cancel it) and
   * applied only while nothing live has been shown. The copy carries no
   * revision or sync timestamp, so the next server load is a full reset and
   * revision admission is never advanced by stale data.
   */
  function hydrateSidebarFromDevice(): Promise<boolean> {
    if (sidebarDeviceHydration) return sidebarDeviceHydration;
    const key = sidebarProjectionCacheKey();
    const startedAt = performance.now();
    sidebarDeviceHydration = (async () => {
      const email = auth.getSnapshot().user?.email;
      if (email && !offlineCache.currentOwner) await offlineCache.setOwner(pageDataOwner);
      const persisted = await offlineCache.get<SidebarProjection>('sidebar', key, pageDataOwner);
      if (!persisted || sidebarNetworkProjectionApplied || key !== sidebarProjectionCacheKey()) {
        return false;
      }
      const reset = true;
      agents = persisted.agents ?? [];
      agentDirectChats = persisted.agent_direct_chats ?? [];
      applyAvailableChannelTypes(persisted.context_types ?? []);
      mergeConversationList(persisted.conversations?.items ?? [], { reset });
      if (!waitingSnapshotCache.has(key)) {
        waitingConversations = persisted.waiting_conversations ?? [];
      }
      conversationCursor = persisted.conversations?.cursor ?? null;
      conversationsHasMore = persisted.conversations?.has_more ?? false;
      sidebarProjectionLoaded = true;
      sidebarProjectionFailed = false;
      conversationListLoading = false;
      recordPerf({ kind: 'sidebar', path: 'device', ms: performance.now() - startedAt });
      return true;
    })().catch(() => false);
    return sidebarDeviceHydration;
  }

  function applyCachedSidebarProjection(key = sidebarProjectionCacheKey()): boolean {
    const cached = sidebarProjectionCache.get(key);
    if (!cached) return false;
    return applySidebarProjection(cached);
  }

  function applyAvailableChannelTypes(contextTypes: string[]): void {
    const types = new Set(contextTypes.map((contextType) => contextType.toLowerCase()));
    for (const selectedChannel of selectedChannels) {
      types.add(selectedChannel.toLowerCase());
    }
    availableChannelTypes = [...types].sort();
  }

  async function loadSidebarProjection(
    options: { recoveryAttempt?: boolean; manual?: boolean } = {},
  ): Promise<void> {
    if (isWorkspaceReplica()) return;
    sidebarRefreshLifecycle.sync();
    const scope = sidebarProjectionCacheKey();
    await Promise.all([
      sidebarReconciliation.request(scope, { full: true, urgent: true, manual: options.manual }),
      waitingReconciliation.request(scope, { urgent: true, manual: options.manual }),
    ]);
  }

  async function performWaitingRefresh(attempt: RefreshAttempt): Promise<void> {
    const key = attempt.scope;
    const recoverChangedScope = () => {
      void waitingReconciliation.request(sidebarProjectionCacheKey(), { urgent: true });
    };
    if (key !== sidebarProjectionCacheKey()) {
      recoverChangedScope();
      return;
    }
    if (waitingSnapshotScope !== key) {
      waitingSnapshotScope = key;
      waitingConversations = (waitingSnapshotCache.get(key)
        ?? sidebarProjectionCache.get(key)?.waiting_conversations ?? []).filter(
        item => conversationMatchesSidebarProjectionFilter(item, currentSidebarProjectionFilter()),
      );
      waitingLoadError = null;
    }
    const sequence = sidebarRevisionAdmission.pushSequence;
    try {
      const items = selectedConversationStatus === 'active'
        ? await api.conversations.waiting({
          contextTypes: selectedChannels.length > 0 ? [...selectedChannels] : null,
          agentIds: selectedAgentIds.length > 0 ? [...selectedAgentIds] : null,
          projectId: selectedProjectId || null,
        }, { signal: attempt.signal, activity: waitingActivity, scope: key }) : [];
      if (attempt.signal.aborted) return;
      if (key !== sidebarProjectionCacheKey()) {
        recoverChangedScope();
        return;
      }
      waitingConversations = reconcileWaitingSnapshot(
        mergeWaitingConversations(conversations, waitingConversations),
        overlaySidebarRows(items, waitingConversations, sidebarRevisionAdmission.pushedSince(sequence)),
      );
      waitingLoadError = null;
      waitingSnapshotCache.delete(key);
      waitingSnapshotCache.set(key, waitingConversations);
      if (waitingSnapshotCache.size > SIDEBAR_PROJECTION_CACHE_LIMIT) {
        waitingSnapshotCache.delete(waitingSnapshotCache.keys().next().value!);
      }
      rememberSidebarProjection();
    } catch (error) {
      if (attempt.signal.aborted) return;
      if (key !== sidebarProjectionCacheKey()) {
        recoverChangedScope();
        return;
      }
      waitingLoadError = asApiError(error).message;
      throw error;
    }
  }

  async function performSidebarLoad(
    options: { recoveryAttempt?: boolean; signal: AbortSignal },
  ): Promise<void> {
    const requestEpoch = ++sidebarProjectionRefreshEpoch;
    conversationListLoadCount += 1;
    const initialKey = sidebarProjectionCacheKey();
    const hadCachedProjection = applyCachedSidebarProjection(initialKey);
    conversationListLoading = conversationListLoadCount > 1 || !hadCachedProjection;
    const loadStartedAt = performance.now();
    if (hadCachedProjection) {
      recordPerf({ kind: 'sidebar', path: 'memory', ms: 0 });
    } else if (!sidebarNetworkProjectionApplied) {
      // Cold start: show the device-cached sidebar while the network loads.
      void hydrateSidebarFromDevice();
    }
    const load = () => api.conversations.sidebar(null, {
      contextTypes: selectedChannels.length > 0 ? selectedChannels : null,
      agentIds: selectedAgentIds.length > 0 ? selectedAgentIds : null,
      projectId: selectedProjectId || null,
      includeShared: includeSharedChats,
      status: selectedConversationStatus,
    }, { signal: options.signal, invalidationOwner: 'refresh' });
    try {
      const response = await load();
      if (options.signal.aborted) return;
      recordPerf({ kind: 'sidebar', path: 'network', ms: performance.now() - loadStartedAt });
      if (!shouldApplySidebarProjectionRefresh({
        requestEpoch,
        currentEpoch: sidebarProjectionRefreshEpoch,
      })) {
        ensureSidebarRecoveryIfBehind('superseded-sidebar-load', options);
        return;
      }
      if (initialKey !== sidebarProjectionCacheKey()) {
        scheduleSidebarReconciliation('changed-sidebar-filter');
        return;
      }
      const admission = sidebarRevisionAdmission.admitProjection(response.sidebar_revision);
      if (!admission.apply) {
        ensureSidebarRecoveryIfBehind('rejected-sidebar-load', options);
        return;
      }
      agents = response.agents;
      if (restoreSelectedAgent()) {
        scheduleSidebarReconciliation('changed-restored-sidebar-filter');
        return;
      }
      if (applySidebarProjection(response, { revisionAdmitted: true })) {
        rememberSidebarProjection();
      }
    } catch (caughtError) {
      if (options.signal.aborted || initialKey !== sidebarProjectionCacheKey()) return;
      if (!shouldApplySidebarProjectionRefresh({
        requestEpoch,
        currentEpoch: sidebarProjectionRefreshEpoch,
      })) return;
      sidebarProjectionFailed = true;
      ensureSidebarRecoveryIfBehind('failed-sidebar-load', options);
      throw caughtError;
    } finally {
      conversationListLoadCount = Math.max(0, conversationListLoadCount - 1);
      conversationListLoading = conversationListLoadCount > 0;
    }
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
    await loadConversationPage(true);
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

  function markConversationObservedByServiceWorker(
    conversationId: string,
    observedAt: string | null | undefined,
  ): void {
    if (!observedAt || typeof navigator === 'undefined' || !navigator.serviceWorker?.controller) return;
    navigator.serviceWorker.controller.postMessage({
      type: 'CONVERSATION_OBSERVED',
      conversation_id: conversationId,
      observed_at: observedAt,
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

    function beginConversationLoad(routeConversationId?: string): number {
      if (routeConversationId) {
        olderMessagesAbortController?.abort();
        olderMessagesAbortController = null;
        loadingOlderMessages = false;
        conversationLoadRequestId = nextConversationLoadId(conversationLoadRequestId);
       canonicalTimelineAuthority = beginCanonicalTimelineAuthority(
         routeConversationId,
         conversationLoadRequestId,
       );
     }
     return conversationLoadRequestId;
   }

   function setCanonicalTimelineAuthority(
     conversationId: string,
     requestId: number,
     status: 'loading' | 'ready' | 'error',
   ): void {
     if (isStaleConversationLoad(requestId) || activeConversationId !== conversationId) return;
     canonicalTimelineAuthority = transitionCanonicalTimelineAuthority(
       canonicalTimelineAuthority,
       conversationId,
       requestId,
       status,
     );
   }

   function canonicalTimelineIsReady(conversationId: string): boolean {
     return canonicalTimelineAuthorityIsReady(
       canonicalTimelineAuthority,
       conversationId,
       conversationLoadRequestId,
     );
   }

  function isStaleConversationLoad(requestId: number): boolean {
    return !pageAlive || timelineController?.current === false
      || !isCurrentConversationLoad(requestId, conversationLoadRequestId);
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

  async function terminateManagedConversation(): Promise<void> {
    if (!currentConversation || managedActionBusy) return;
    const id = currentConversation.conversation_id;
    managedActionBusy = 'close';
    managedActionError = '';
    try {
      const result = await performManagedAction(currentConversation, 'close');
      if (result) {
        patchConversationInList(id, result.conversation);
        if (currentConversation?.conversation_id === id) currentConversation = result.conversation;
      }
    } catch (error) {
      if (currentConversation?.conversation_id === id) managedActionError = asApiError(error).message;
    } finally {
      if (currentConversation?.conversation_id === id) managedActionBusy = null;
    }
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
    if (!childWorkstream) return undefined;
    return agents.find((agent) => agent.agent_id === childWorkstream?.agent_id);
  }

  function subSessionStatus(): string | null {
    return childWorkstream?.status ?? null;
  }

  function shouldPollSubSession(): boolean {
    const status = subSessionStatus();
    return Boolean(childView && (!status || NON_TERMINAL_SESSION_STATES.has(status)));
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
    findOnlySearchConversationId = null;
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

  /** Timeline item that renders a commented source, if it is loaded. */
  function commentSourceItemId(source: FeedbackSourceRef): string | null {
    if (source.kind === 'document') {
      return renderableVisibleItems.find(item => item.kind === 'assistant_deliverable' && item.deliverable_id === source.content_ref)?.id ?? null;
    }
    if (source.kind === 'transcript' && source.conversation_id !== currentConversation?.conversation_id) return null;
    return renderableVisibleItems.some(item => item.id === source.source_item_id) ? source.source_item_id : null;
  }
  /**
   * Bring a commented source into the rendered timeline: widen the window when it is loaded,
   * otherwise page older history in until it appears or history is exhausted.
   */
  async function revealCommentSource(source: FeedbackSourceRef): Promise<boolean> {
    const conversationId = currentConversation?.conversation_id;
    if (!conversationId || (source.kind === 'transcript' && source.conversation_id !== conversationId)) return false;
    for (let page = 0; page < 40; page++) {
      if (currentConversation?.conversation_id !== conversationId) return false;
      const id = commentSourceItemId(source);
      if (id) { scrollToTimelineItem(id); await tick(); return true; }
      if (!hasOlderMessages || !olderMessagesCursor) return false;
      while (loadingOlderMessages) await new Promise(resolve => setTimeout(resolve, 50));
      const before = renderableVisibleItems.length;
      await loadOlder();
      await tick();
      if (renderableVisibleItems.length === before && !hasOlderMessages) return false;
    }
    return false;
  }
  onMount(() => commentWorkspace.setRevealer(revealCommentSource));

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
    if (result.source === 'server' && !renderableVisibleItems.some(item => item.id === result.targetId)) {
      void tick().then(() => {
        if (chatSearchOpen && chatSearchResults[chatSearchSelectedIndex] === result) {
          jumpToActiveTimelineStart({ scrollTop: 0, rowKey: null });
        }
      });
      return;
    }
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
    if (!chatSearchOpen) chatSearchOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    findOnlySearchConversationId = null;
    chatSearchOpen = true;
    seededChatSearchSession = null;
    seededChatSearchRef = null;
    if (seed) chatSearchQuery = seed;
    seededChatSearchRan = true;
    void tick().then(() => void runChatSearch());
  }

  function closeChatSearch(): void {
    chatSearchOpen = false;
    chatSearchResults = [];
    seededChatSearchSession = null;
    seededChatSearchRef = null;
    const opener = chatSearchOpener;
    chatSearchOpener = null;
    void tick().then(() => { if (opener?.isConnected) opener.focus({ preventScroll: true }); });
  }

  function openCurrentConversationInSeparateWindow(): void {
    if (!currentConversation || typeof window === 'undefined') return;
    if (openNativeConversationWindow(currentConversation.conversation_id)) return;
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

  async function resyncSidebarData(
    reason: string,
    options: { force?: boolean; recoveryAttempt?: boolean } = {},
  ): Promise<void> {
    scheduleSidebarReconciliation(reason, options);
  }

  async function performSidebarResync(
    reason: string,
    options: { force?: boolean; recoveryAttempt?: boolean; signal: AbortSignal },
  ): Promise<void> {
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
      projectId: selectedProjectId || null,
      includeShared: includeSharedChats,
      status: selectedConversationStatus,
    };
    const filterKey = sidebarProjectionCacheKey();
    const requestEpoch = ++sidebarProjectionRefreshEpoch;
    try {
      const isDeltaRequest = Boolean(lastSidebarSyncTimestamp);
      const response = isDeltaRequest
        ? await api.conversations.sidebar(null, filters, {
            changedSince: lastSidebarSyncTimestamp,
            sidebarRevision: sidebarRevisionAdmission.authoritativeRestCursor,
            signal: options.signal,
            invalidationOwner: 'refresh',
          })
        : await api.conversations.sidebar(null, filters, { signal: options.signal, invalidationOwner: 'refresh' });
      if (options.signal.aborted) return;
      if (!shouldApplySidebarProjectionRefresh({
        requestEpoch,
        currentEpoch: sidebarProjectionRefreshEpoch,
      })) {
        ensureSidebarRecoveryIfBehind('superseded-sidebar-resync', options);
        return;
      }
      if (filterKey !== sidebarProjectionCacheKey()) {
        scheduleSidebarReconciliation('changed-sidebar-resync-filter');
        return;
      }
      if (response.full_resync_required) {
        void sidebarReconciliation.request(filterKey, { full: true });
        return;
      }
      const applied = applySidebarProjection(response, {
        reset: response.is_delta === undefined ? !isDeltaRequest : !response.is_delta,
      });
      if (!applied) ensureSidebarRecoveryIfBehind('rejected-sidebar-resync', options);
      if (applied) rememberSidebarProjection();
    } catch (error) {
      if (options.signal.aborted || filterKey !== sidebarProjectionCacheKey()) return;
      if (!shouldApplySidebarProjectionRefresh({
        requestEpoch,
        currentEpoch: sidebarProjectionRefreshEpoch,
      })) {
        ensureSidebarRecoveryIfBehind('superseded-failed-sidebar-resync', options);
        return;
      }
      console.debug('Sidebar resync failed', { reason, error });
      sidebarProjectionFailed = true;
      throw error;
    }
  }

  function ensureSidebarRecoveryIfBehind(
    reason: string,
    options: { recoveryAttempt?: boolean } = {},
  ): void {
    if (!options.recoveryAttempt && sidebarRevisionAdmission.needsReconciliation) {
      scheduleSidebarReconciliation(reason);
    }
  }

  function scheduleSidebarReconciliation(reason: string, options: { force?: boolean } = {}): void {
    sidebarRefreshLifecycle.sync();
    // User-driven filter changes must not wait for the realtime spacing.
    const changed = reason.startsWith('changed-');
    const scope = sidebarProjectionCacheKey();
    void sidebarReconciliation.request(scope, {
      full: changed || options.force, urgent: changed || options.force,
    });
    void waitingReconciliation.request(scope, { urgent: changed || options.force });
  }

  async function forceRefreshConversationHistory(): Promise<void> {
    if (conversationListRefreshing) return;
    conversationListRefreshing = true;
    resetConversationSearchResults();
    try {
      await loadSidebarProjection({ manual: true });
    } finally {
      conversationListRefreshing = false;
    }
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
        arguments_display:
          item.payload.arguments_display
          && typeof item.payload.arguments_display === 'object'
          && !Array.isArray(item.payload.arguments_display)
            ? item.payload.arguments_display as Record<string, unknown>
            : null,
        decision: 'escalate',
        resolved: false,
        reasoning: typeof item.payload.reasoning === 'string' ? item.payload.reasoning : null,
        risk: typeof item.payload.risk === 'string' ? item.payload.risk : null,
        timeout_seconds:
          typeof item.payload.timeout_seconds === 'number'
            ? item.payload.timeout_seconds
            : escalationTimeoutSeconds,
        received_at: item.created_at ? Date.parse(item.created_at) : Date.now(),
        managed_conversation_title:
          typeof item.payload.managed_conversation_title === 'string'
            ? item.payload.managed_conversation_title
            : null,
        managed_target_agent_id:
          typeof item.payload.managed_target_agent_id === 'string'
            ? item.payload.managed_target_agent_id
            : null,
        managed_origin_conversation_id:
          typeof item.payload.managed_origin_conversation_id === 'string'
            ? item.payload.managed_origin_conversation_id
            : null
      }) satisfies Escalation);
    const pendingEscalations = filtered.filter(
      // A locally settled escalation (REST resolve already succeeded) must
      // not be resurrected by a REST catch-up refresh that has not yet
      // observed the backend resolution.
      (item) => !isEscalationExpired(item) && !locallySettledEscalationCallIds.has(item.call_id)
    );
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
      (item) =>
        item.notification_type === 'credential_request'
        && item.status === 'pending'
        && managedInteractionVisibleInScope(
          typeof item.payload.managed_origin_conversation_id === 'string'
            ? item.payload.managed_origin_conversation_id
            : null,
          selectedManagedChildConversationId,
        ),
    ) ?? null;
    // Send routing resolves the notification id from pendingDirectQuestion
    // or a fresh notifications fetch (resolveDirectQuestionNotificationId),
    // so no per-tool annotation on the canonical timeline is required.
    const directQuestion = pendingStepNotifications
      .filter((item) => item.task_id === null)
      .map((item) => pendingDirectQuestionFromNotification(item))
      .filter(
        (item): item is PendingDirectQuestion =>
          item !== null
          && !locallySettledDirectQuestionIds.has(item.notificationId)
          && managedInteractionVisibleInScope(
            item.managedOrigin?.originConversationId,
            selectedManagedChildConversationId,
          ),
      )[0] ?? null;
    if (directQuestion) {
      directQuestion.observedStateVersion = backendConversationState?.state_version ?? 0;
    }
    if (
      directQuestionSubmitting
      && pendingDirectQuestion
      && locallySettledDirectQuestionIds.has(pendingDirectQuestion.notificationId)
    ) {
      return;
    }
    if (shouldResetPendingDirectQuestionForm(
      pendingDirectQuestion?.notificationId,
      directQuestion?.notificationId,
    )) {
      directQuestionAnswers = restoreDirectQuestionDraft(directQuestion);
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
      // A genuinely new/different notification must display normally, even
      // if a settled acknowledgement for a previous one is still showing.
      // A transition to "no pending question" (settled elsewhere) keeps the
      // acknowledgement instead of clearing it.
      if (directQuestion) directQuestionAck = null;
    }
    pendingDirectQuestion = directQuestion;
    if (directQuestion === null) {
      clearDirectQuestionSubmission();
      directQuestionAnswers = {};
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
    }
  }

  async function refreshEscalations(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    const conversationId = currentConversation.conversation_id;
    const requestEpoch = beginPendingNotificationRefresh(conversationId);
    const loadRequestId = conversationLoadRequestId;
    const applies = () => shouldApplyPendingNotificationConsumerRefresh({
      activeConversationId: currentConversation?.conversation_id,
      refreshConversationId: conversationId,
      requestEpoch,
      currentEpoch: pendingNotificationEpochByConversation.get(conversationId),
      loadRequestId,
      activeLoadRequestId: conversationLoadRequestId,
    });
    try {
      const taskId = conversationTaskId(currentConversation);
      const notifications = taskId
        ? await api.notifications.list(null, { taskId })
        : await api.notifications.list(conversationId);
      if (!applies()) return;
      applyEscalationNotifications(notifications, taskId);
      escalationError = '';
    } catch (caughtError) {
      if (!applies()) return;
      escalationError = asApiError(caughtError).message;
      markControllerUnavailable(caughtError);
    }
  }

  async function refreshPendingDirectQuestion(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    const conversationId = currentConversation.conversation_id;
    const requestEpoch = beginPendingNotificationRefresh(conversationId);
    const loadRequestId = conversationLoadRequestId;
    try {
      const notifications = await api.notifications.list(conversationId);
      if (!shouldApplyPendingNotificationConsumerRefresh({
        activeConversationId: currentConversation?.conversation_id,
        refreshConversationId: conversationId,
        requestEpoch,
        currentEpoch: pendingNotificationEpochByConversation.get(conversationId),
        loadRequestId,
        activeLoadRequestId: conversationLoadRequestId,
      })) return;
      applyPendingDirectQuestionNotifications(notifications);
    } catch {
      // Ignore notification refresh failures here — they should not block chat.
    }
  }

  async function refreshPendingNotifications(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    const conversationId = currentConversation.conversation_id;
    const requestEpoch = beginPendingNotificationRefresh(conversationId);
    const loadRequestId = conversationLoadRequestId;
    const applies = () => shouldApplyPendingNotificationConsumerRefresh({
      activeConversationId: currentConversation?.conversation_id,
      refreshConversationId: conversationId,
      requestEpoch,
      currentEpoch: pendingNotificationEpochByConversation.get(conversationId),
      loadRequestId,
      activeLoadRequestId: conversationLoadRequestId,
    });
    try {
      const taskId = conversationTaskId(currentConversation);
      const conversationNotifications = await api.notifications.list(conversationId);
      const escalationNotifications = taskId
        ? await api.notifications.list(null, { taskId })
        : conversationNotifications;
      if (!applies()) return;
      applyEscalationNotifications(escalationNotifications, taskId);
      applyPendingDirectQuestionNotifications(conversationNotifications);
    } catch (caughtError) {
      if (!applies()) return;
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
    criticalEventStoreError = '';
  }

  function markControllerUnavailable(caughtError: unknown, conversationId?: string): boolean {
    const apiError = asApiError(caughtError);
    const isEventStoreFailure = apiError.code.startsWith('event_store_');
    if (apiError.status !== 503 && !isEventStoreFailure) return false;
    if (isEventStoreFailure) {
      criticalEventStoreError = apiError.message;
    }
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
        if (
          controllerRecoveryPending
          && (sessionsError || historyError || escalationError || criticalEventStoreError)
        ) {
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
    if (sessionsError || historyError || escalationError || criticalEventStoreError) {
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
    timelineWindowRevision += 1;
    timelineWindowPending = false;
    timelineReadingPosition = null;
    tailWindowRows = TIMELINE_WINDOW_TARGET_ROWS;
    // Window over renderable rows only (matches displayedTimeline slicing).
    // Live tail: no upper bound, start a target-sized page from the end.
    timelineWindow = {
      start: Math.max(0, renderableVisibleItems.length - TIMELINE_WINDOW_TARGET_ROWS),
      end: null,
    };
    establishTimelineWindow();
  }

  function establishTimelineWindow(): void {
    if (pageAlive && timelineController?.current
      && chatV2Store.snapshot.cursor !== null
      && timelineController.scope.conversation_id === currentConversation?.conversation_id) {
      timelineViewportStates.establishWindow(timelineController.scope.key, conversationLoadRequestId);
    }
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
      // Membership transitions already map the reading range by identity.
      // Retain the count fallback for initial view/cache positioning.
      timelineWindow = freezeTailWindow(timelineWindow, total, lastRenderableVisibleCount);
      establishTimelineWindow();
      return;
    }
    timelineWindow = reconcileLiveTailWindow(
      timelineWindow,
      total,
      tailWindowRows,
    );
    establishTimelineWindow();
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
    if (!timelineEl || childView || !timelineInitialPositionSettled) return;
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
    if (!timelineEl || childView) return;
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
        timelineInitialPositionSettled = true;
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
  type TimelineScrollAnchor = TimelineReadingAnchor;
  let timelineReadingPosition: TimelineScrollAnchor | null = null;

  function captureTimelineAnchor(): TimelineScrollAnchor | null {
    return timelineEl ? captureTimelineReadingAnchor(timelineEl) : null;
  }

  function readingAnchorForMutation(): TimelineScrollAnchor | null {
    // Reuse the reader's position across overlapping layout and membership
    // commits, but never reuse it after input has moved the viewport.
    return timelineEl && timelineReadingPosition
      && Math.abs(timelineEl.scrollTop - timelineReadingPosition.scrollTop) < 1
      ? timelineReadingPosition : captureTimelineAnchor();
  }

  function restoreTimelineAnchor(anchor: TimelineScrollAnchor | null): void {
    if (!timelineEl || !anchor) return;
    const target = timelineReadingScrollTop(timelineEl, anchor);
    if (target === null) {
      lastTimelineScrollTop = timelineEl.scrollTop;
      timelineReadingPosition = captureTimelineAnchor();
      return;
    }
    writeProgrammaticScrollTop(target);
    timelineReadingPosition = captureTimelineAnchor();
  }

  async function applyAnchoredTimelineWindow(
    next: TimelineWindow,
    anchor = readingAnchorForMutation(),
  ): Promise<void> {
    const revision = ++timelineWindowRevision;
    const viewport = timelineEl;
    const loadId = conversationLoadRequestId;
    const transition = parentSurfaceTransitionGeneration;
    const controller = timelineController;
    timelineWindowPending = true;
    timelineReadingPosition = anchor;
    timelineWindow = protectTimelineReadingWindow(next, renderableVisibleItems, anchor);
    await tick();
    if (revision === timelineWindowRevision) timelineWindowPending = false;
    if (revision !== timelineWindowRevision || viewport !== timelineEl
      || loadId !== conversationLoadRequestId || childView || !pageAlive
      || controller !== timelineController || !controller?.current
      || transition !== parentSurfaceTransitionGeneration) return;
    restoreTimelineAnchor(anchor);
  }

  function cssEscape(value: string): string {
    if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
      return CSS.escape(value);
    }
    return value.replace(/["\\]/g, '\\$&');
  }

  function writeProgrammaticScrollTop(value: number): void {
    if (!timelineEl) return;
    const revision = ++timelineScrollWriteRevision;
    programmaticScroll = true;
    timelineEl.scrollTop = value;
    programmaticScrollTarget = timelineEl.scrollTop;
    lastTimelineScrollTop = timelineEl.scrollTop;
    requestAnimationFrame(() => {
      if (revision !== timelineScrollWriteRevision) return;
      programmaticScroll = false;
      requestAnimationFrame(() => {
        if (revision === timelineScrollWriteRevision) programmaticScrollTarget = null;
      });
    });
  }

  function handleTimelineScroll(): void {
    if (!timelineEl || childView || programmaticScroll || timelineWindowPending || !timelineInitialPositionSettled) return;
    const currentScrollTop = timelineEl.scrollTop;
    const pagingDirection = timelinePagingDirection(currentScrollTop, lastTimelineScrollTop,
      timelineEl.scrollHeight - timelineEl.clientHeight);
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
    timelineReadingPosition = userScrolledUp ? captureTimelineAnchor() : null;
    if (!hasHiddenNewerRows && isNearScrollBottom(nextState.distanceFromBottom, CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX)) {
      userScrollIntentUp = false;
      lastTimelineTouchY = null;
    }

    lastTimelineScrollTop = currentScrollTop;

    if (
      timelineInitialPositionSettled
      && pagingDirection < 0
      && currentScrollTop <= 24
      && (visibleStartIndex > 0 || hasOlderMessages)
    ) {
      void loadOlder();
      return;
    }

    // Downward re-expansion: when the user scrolls back toward the tail and
    // newer rows are still unmounted, grow the window so they remount in place.
    // Without this the tail stays hidden until an explicit jump-to-bottom.
    if (pagingDirection > 0 && hasHiddenNewerRows
      && distanceFromBottom <= CHAT_TIMELINE_EXPAND_DOWN_THRESHOLD_PX) {
      void applyAnchoredTimelineWindow(expandWindowDown(timelineWindow, renderableVisibleItems.length));
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
    if (event.deltaY < 0) {
      markUserScrollIntentUp();
      clearUserScrollIntentSoon();
      if (timelineInitialPositionSettled && timelineEl && timelineEl.scrollTop <= 24) void loadOlder();
    } else if (event.deltaY > 0) {
      expandTimelineAtBottom();
    }
  }

  function expandTimelineAtBottom(): void {
    if (!timelineInitialPositionSettled || !timelineEl || timelineWindowPending
      || childView || !timelineWindowHasHiddenTail(timelineWindow, renderableVisibleItems.length)
      || timelineEl.scrollHeight - timelineEl.clientHeight - timelineEl.scrollTop > 24) return;
    void applyAnchoredTimelineWindow(expandWindowDown(timelineWindow, renderableVisibleItems.length));
  }

  function handleTimelinePointerDown(event: PointerEvent): void {
    if (event.target === event.currentTarget) {
      userScrollIntentUp = true;
      clearUserScrollIntentSoon();
    }
    if (inspectorPresentation === 'overlay') closeHeaderInfo(false);
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
      if (timelineInitialPositionSettled && timelineEl && timelineEl.scrollTop <= 24) void loadOlder();
    } else if (currentY < lastTimelineTouchY - CHAT_USER_SCROLL_DELTA_THRESHOLD_PX) {
      expandTimelineAtBottom();
    }
    lastTimelineTouchY = currentY;
  }

  function handleTimelineTouchEnd(): void {
    lastTimelineTouchY = null;
    clearUserScrollIntentSoon();
  }

  function handleTimelineKeydown(event: KeyboardEvent): void {
    if ((event.target as HTMLElement | null)?.closest('input, textarea, button, a, [contenteditable="true"]')) return;
    if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) {
      markUserScrollIntentUp();
      clearUserScrollIntentSoon();
      if (timelineInitialPositionSettled && timelineEl && timelineEl.scrollTop <= 24) {
        event.preventDefault();
        void loadOlder();
      }
    } else if (['ArrowDown', 'PageDown', 'End'].includes(event.key)) {
      expandTimelineAtBottom();
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
    return agents.filter((agent) => !agent.is_contact_only && agent.status === 'active' && agent.agent_type === 'primary');
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

  async function persistSelectedChannels(): Promise<void> {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedChannel, JSON.stringify(selectedChannels));
    resetConversationSearchResults();
    await loadConversationPage(true);
  }

  function restoreSelectedChannel(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedChannel);
    selectedChannels = stored === null ? ['web'] : parseStoredMultiFilter(stored);
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
    const storedWidth = Number(window.localStorage.getItem(CHAT_STORAGE_KEYS.sidebarWidth));
    if (Number.isFinite(storedWidth)) {
      chatSidebarWidth = storedWidth === CHAT_SIDEBAR_LEGACY_DEFAULT_WIDTH
        ? CHAT_SIDEBAR_DEFAULT_WIDTH
        : Math.max(CHAT_SIDEBAR_MIN_WIDTH, Math.min(CHAT_SIDEBAR_MAX_WIDTH, storedWidth));
      if (storedWidth === CHAT_SIDEBAR_LEGACY_DEFAULT_WIDTH) {
        window.localStorage.setItem(CHAT_STORAGE_KEYS.sidebarWidth, String(chatSidebarWidth));
      }
    }
  }

  function setChatSidebarWidth(nextWidth: number): void {
    chatSidebarWidth = Math.max(CHAT_SIDEBAR_MIN_WIDTH, Math.min(CHAT_SIDEBAR_MAX_WIDTH, nextWidth));
    window.localStorage.setItem(CHAT_STORAGE_KEYS.sidebarWidth, String(chatSidebarWidth));
  }

  function startChatSidebarResize(event: PointerEvent): void {
    const startX = event.clientX;
    const startWidth = chatSidebarWidth;
    const move = (next: PointerEvent) => setChatSidebarWidth(startWidth + next.clientX - startX);
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
  }

  function resizeChatSidebarWithKeyboard(event: KeyboardEvent): void {
    const step = event.shiftKey ? 40 : 10;
    if (event.key === 'ArrowLeft') setChatSidebarWidth(chatSidebarWidth - step);
    else if (event.key === 'ArrowRight') setChatSidebarWidth(chatSidebarWidth + step);
    else if (event.key === 'Home') setChatSidebarWidth(CHAT_SIDEBAR_MIN_WIDTH);
    else if (event.key === 'End') setChatSidebarWidth(CHAT_SIDEBAR_MAX_WIDTH);
    else return;
    event.preventDefault();
  }

  function toggleChatSidebar(): void {
    if (isWindowMode) return;
    chatSidebarCollapsed = !chatSidebarCollapsed;
    if (!chatSidebarCollapsed) refreshOpenedConversationList();
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(CHAT_STORAGE_KEYS.sidebarCollapsed, chatSidebarCollapsed ? '1' : '0');
    }
  }

  function openMobileList(): void {
    if (isWindowMode) return;
    mobileDrawerPreviouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    conversationFiltersOpen = false;
    mobileListOpen = true;
    refreshOpenedConversationList();
  }

  function refreshOpenedConversationList(): void {
    sidebarRefreshLifecycle.sync();
    void sidebarReconciliation.request(sidebarProjectionCacheKey(), { maxAgeMs: 30_000 });
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
    const sid = focusedSessionId ?? currentConversation?.active_session_id;
    if (!conversationId || !sid) {
      invalidateSessionInfo();
      return;
    }
    const cached = getSessionInfo(conversationId, sid);
    if (cached) {
      sessionInfo = latestRuntimeSelection?.sessionId === sid
        ? mergeRuntimeSelection(cached, latestRuntimeSelection.selection)
        : cached;
      applyCachedSessionDiagnostics(sid, cached);
    }
    const requestId = nextSessionInfoRequestId();
    sessionInfoLoading = true;
    try {
      const detail = await api.sessions.intarisDetail(sid);
      if (isStaleSessionInfoLoad(requestId, conversationId, sid)) {
        return;
      }
      let nextSessionInfo = mergeSessionInfo(
        sessionInfo?.intaris_session_id === sid ? sessionInfo : null,
        sessionInfoFromDetail(detail),
      );
      if (detail.runtime_selection) {
        latestRuntimeSelection = {
          sessionId: sid,
          selection: newestRuntimeSelection(
            latestRuntimeSelection?.sessionId === sid
              ? latestRuntimeSelection.selection
              : null,
            detail.runtime_selection
          )
        };
      }
      if (latestRuntimeSelection?.sessionId === sid) {
        nextSessionInfo = mergeRuntimeSelection(nextSessionInfo, latestRuntimeSelection.selection);
      }
      setSessionInfo(conversationId, sid, nextSessionInfo);
      sessionInfo = nextSessionInfo;
      applyContextUsageSnapshot(nextSessionInfo.context_usage, sid);
      applyLastGenerationSnapshot(nextSessionInfo.last_generation, sid);
    } catch {
      // Retain the last rendered session details when a refresh fails.
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
      if (sessionInfo?.intaris_session_id === sid) {
        sessionInfo = mergeSessionInfo(sessionInfo, sessionInfoFromDetail(detail));
        setSessionInfo(conversationId, sid, sessionInfo);
      }
    } catch {
      // Context diagnostics are opportunistic; the Info panel can still load details on demand.
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
        reloadHistory
          ? bindTimeline(conversationId).load({ signal: timelineLoadAbort.signal }).then(snapshot => ({ snapshot }))
          : Promise.resolve(null),
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

    const timelineLoaded = chatV2SnapshotResult.status === 'fulfilled' && chatV2SnapshotResult.value !== null;
    const chatV2Snapshot = chatV2SnapshotResult.status === 'fulfilled'
      ? chatV2SnapshotResult.value?.snapshot
      : null;
    if (reloadHistory && !timelineLoaded) {
      const failure = chatV2SnapshotResult.status === 'rejected'
        ? chatV2SnapshotResult.reason
        : new Error('Chat v2 snapshot unavailable');
      const failureMessage = asApiError(failure).message;
      historyError = failureMessage;
      if (markControllerUnavailable(failure)) {
        return;
      }
      if (!chatV2OwnsActiveConversation(conversationId) || chatV2Store.visibleItems.length === 0) {
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
      if (!isConnectivityError(failure)) addToast(failureMessage, 'error');
      return;
    }
    let chatV2SnapshotApplied = false;
    if (reloadHistory && chatV2Snapshot) {
      // Capture the scroll position immediately before the snapshot mutates
      // the timeline — the user's position NOW, not where they were when the
      // network fetch started.
      if (wantsPreserveScroll && userScrolledUp && timelineEl) {
        preservedScrollTop = timelineEl.scrollTop;
      }
      chatV2SnapshotApplied = applyChatV2Snapshot(chatV2Snapshot, {
        routeGeneration: requestId,
      });
      if (chatV2SnapshotApplied) {
        void showPendingOutboxMessages(conversationId).then(() => drainChatV2Outbox(conversationId));
        nextActiveSessionId = chatV2Snapshot.conversation.active_session_id ?? nextActiveSessionId;
        setConversationTurnIndicator(currentConversation?.conversation_id, turnInProgress);
      } else {
        scheduleChatV2CanonicalRecovery(conversationId);
      }
    } else if (reloadHistory && timelineLoaded) {
      chatV2SnapshotApplied = true;
      applyChatV2StateToTimeline();
      olderMessagesCursor = chatV2Store.snapshot.beforeCursor;
      hasOlderMessages = chatV2Store.snapshot.hasMoreBefore;
      canonicalTimelineAuthority = acceptCanonicalTimelineBase(canonicalTimelineAuthority, conversationId, requestId);
      chatV2Lifecycle.acceptSnapshot(conversationTimelineScope(conversationId), chatV2Store.snapshot.cursor!);
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

    if (reloadHistory && chatV2Snapshot && chatV2SnapshotApplied) {
      criticalEventStoreError = '';
      if (preservedScrollTop !== null) {
        timelineWindow = {
          start: Math.min(preservedVisibleStartIndex, Math.max(0, renderableVisibleItems.length - TIMELINE_WINDOW_TARGET_ROWS)),
          end: null,
        };
        establishTimelineWindow();
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
      escalationSettledAck = null;
      locallySettledEscalationCallIds = new Set();
      pendingDirectQuestion = null;
      pendingCredentialRequest = null;
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
      directQuestionSubmitting = false;
      directQuestionSubmittingNotificationId = null;
      locallySettledDirectQuestionIds = new Set();
      authoritativelySettledDirectQuestionIds = new Set();
      directQuestionAck = null;
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
    const conversationId = currentConversation.conversation_id;
    error = '';
    const retryScope = getConversationRetryScope({
      sessionsError,
      historyError,
    });
    if (retryScope.history) {
      setCanonicalTimelineAuthority(conversationId, requestId, 'loading');
    }
    conversationSubloadsLoading = true;
    try {
      const reload = () => reloadConversationSubloads(
        conversationId,
        requestId,
        {
          reloadSessions: retryScope.sessions,
          reloadHistory: retryScope.history,
          resubscribe: false,
        },
      );
      await (
        retryScope.history
          ? chatV2CanonicalRecoveryCoalescer.run(conversationId, reload)
          : reload()
      );
      if (retryScope.history && !isStaleConversationLoad(requestId)) {
        if (historyError) {
          setCanonicalTimelineAuthority(conversationId, requestId, 'error');
        } else {
          await settleInitialCanonicalHistory(conversationId, requestId);
        }
      }
    } catch (caughtError) {
      if (retryScope.history && !isStaleConversationLoad(requestId)) {
        historyError = asApiError(caughtError).message;
        setCanonicalTimelineAuthority(conversationId, requestId, 'error');
      }
      throw caughtError;
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

  // Project readers get no live socket fan-out for another member's chat, so
  // refresh the durable snapshot while a shared read-only conversation is open.
  const SHARED_READER_REFRESH_MS = 8_000;
  $effect(() => {
    const conversationId = currentConversation?.is_readonly_for_caller
      ? currentConversation.conversation_id
      : null;
    // Hidden tabs do not poll; the visibility handler refreshes on return.
    if (!conversationId || !browserVisible || typeof window === 'undefined') return;
    const timer = window.setInterval(() => {
      if (currentConversation?.conversation_id === conversationId) {
        void refreshConversationView('shared-reader-poll');
      }
    }, SHARED_READER_REFRESH_MS);
    return () => window.clearInterval(timer);
  });

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
      await chatV2CanonicalRecoveryCoalescer.run(
        currentConversation.conversation_id,
        () => reloadConversationSubloads(currentConversation!.conversation_id, requestId, {
          reloadSessions: true,
          reloadHistory: true,
          resubscribe: true,
          preserveScroll: true,
        }),
      );

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
    const opening = openConversationView(conversationId);
    const requestId = conversationLoadRequestId;
    try {
      await opening;
    } catch (caught) {
      if (!pageAlive || requestId !== conversationLoadRequestId
        || conversationId !== conversationIdFromRoute()) return;
      if (caught instanceof TimelineOwnershipError) {
        requireTimelineIdentity(caught);
      } else if (!(caught instanceof DOMException && caught.name === 'AbortError')) {
        error = asApiError(caught).message;
        initializing = false;
        initialConversationResolved = true;
        conversationSubloadsLoading = false;
        cachedConversationRefreshing = false;
        stopInitialLoadTimeout();
        resetConversationSwitch();
      }
    }
  }

  async function openConversationView(conversationId: string): Promise<void> {
    if (timelineIdentityRequired) {
      // Navigation still supersedes explicit verification while the identity
      // panel owns rendering; do not revive an obsolete route after it settles.
      ++conversationLoadRequestId;
      timelineOwnerRecovery?.abort();
      return;
    }
    if (!conversationId) {
      initializing = false;
      resetConversationSwitch();
      initialConversationResolved = true;
      return;
    }

    let reversedSwitchGeneration: number | null = null;
    if (switchingConversationId && switchingConversationId !== conversationId) {
      const routeGeneration = beginConversationLoad(conversationId);
      beginTimelineViewportTransition(conversationId, routeGeneration);
      resetConversationSwitch();
      reversedSwitchGeneration = routeGeneration;
    }

    if (
      conversationId === activeConversationId
      && currentConversation
      && canonicalTimelineIsReady(conversationId)
      && !historyError
      && !sessionsError
      && !chatV2CanonicalRecoveryCoalescer.has(conversationId)
    ) {
      if (
        reversedSwitchGeneration !== null
        && !await settleTimelineViewportTransition(conversationId, reversedSwitchGeneration)
      ) return;
      initializing = false;
      resetConversationSwitch();
      initialConversationResolved = true;
      return;
    }

    if (conversationId === activeConversationId && currentConversation) {
      if (appContactBindingId(currentConversation)) {
        initializing = false;
        initialConversationResolved = true;
        stopInitialLoadTimeout();
        resetConversationSwitch();
        return;
      }
      const requestId = conversationLoadRequestId;
      setCanonicalTimelineAuthority(conversationId, requestId, 'loading');
      historyError = '';
      cachedConversationRefreshing = true;
      try {
        await recoverChatV2Canonical(conversationId, requestId);
      } catch (caughtError) {
        if (!isStaleConversationLoad(requestId)) {
          historyError = asApiError(caughtError).message;
          setCanonicalTimelineAuthority(conversationId, requestId, 'error');
        }
      } finally {
        if (!isStaleConversationLoad(requestId)) {
          cachedConversationRefreshing = false;
          await settleTimelineViewportTransition(conversationId, requestId);
          resetConversationSwitch();
        }
      }
      return;
    }

    const requestId = beginConversationLoad(conversationId);
    const clientPerformance = createClientPerformanceTiming(
      performance.now(),
      () => performance.now(),
      (metric, durationMs) => {
        void chatV2Api.clientPerformance(metric, durationMs);
      }
    );
    const previousConversationId = activeConversationId;
    if (previousConversationId && previousConversationId !== conversationId) {
      // A genuine switch away from `previousConversationId` starts here.
      // Any pending-notification consumer (refreshEscalations,
      // refreshPendingDirectQuestion, refreshPendingNotifications) still
      // in flight for it must be treated as stale immediately, even before
      // this switch resolves and even if nothing else ever refreshes or
      // pushes state for that conversation again (A→B→A).
      observePendingNotificationServerPush(previousConversationId);
    }
    if (
      previousConversationId
      && currentConversation?.conversation_id === previousConversationId
      && !conversationMatchesSidebarProjectionFilter(
        currentConversation,
        currentSidebarProjectionFilter()
      )
    ) {
      // A deep-linked managed conversation is intentionally made visible while
      // open, even when it falls outside the current sidebar filter. Restore
      // the filtered projection as soon as the user leaves it.
      removeConversationFromSidebar(previousConversationId);
    }
    if (
      controllerRecoveryConversationId !== null
      && controllerRecoveryConversationId !== conversationId
    ) {
      clearControllerRecovery();
    }
    const isInitialLoad = !initialConversationResolved && !currentConversation;
    const switchRequestId = beginConversationSwitch(conversationId, !isInitialLoad);
    conversationPreloader.setExcluded(conversationId);
    saveCurrentConversationView();
    beginTimelineViewportTransition(conversationId, requestId);

    showAgentProfile = null;
    error = '';
    historyError = '';
    sessionsError = '';
    escalationError = '';
    escalationResolutionPending = null;
    escalationSettledAck = null;
    locallySettledEscalationCallIds = new Set();
    cachedConversationRefreshing = false;
    invalidateSessionInfo();
    mobileListOpen = false;

    chatV2Lifecycle.release();
    if (previousConversationId) {
      wsClient.unsubscribeConversation(previousConversationId);
    }

    // A cached view is restored for every conversation kind, including
    // agent-direct: it paints the previously-seen timeline/state immediately
    // (see restoreConversationView()), which is the primary win for slow
    // repeat switching into long agent-direct conversations. Any transient
    // open/send/modal state from the previously active conversation that is
    // not part of the cache entry is reset inside restoreConversationView()
    // itself, so cached restore cannot leak conversation A's ephemeral state
    // into conversation B.
    if (!conversationViewCache.has(conversationId)) {
      // Cold start / PWA relaunch: paint the device-cached view first.
      try {
        await hydratePersistedConversationView(conversationId);
      } catch (caught) {
        if (timelineIdentityRequired || isStaleConversationLoad(requestId)) return;
        throw caught;
      }
      if (isStaleConversationLoad(requestId)) return;
    }
    const cachedView = restoreConversationView(conversationId);
    if (cachedView) {
      if (appContactBindingId(cachedView.conversation)) {
        activeConversationId = conversationId;
        currentConversation = cachedView.conversation;
        timelineInitialPositionSettled = true;
        initializing = false;
        initialConversationResolved = true;
        stopInitialLoadTimeout();
        resetConversationSwitch();
        return;
      }
      // Keep an empty cached view in its loading state until canonical history
      // has confirmed whether this is genuinely a new conversation.
      cachedViewFetchedAt = cachedView.fetchedAt ?? 0;
      cachedViewHasContent = chatV2Store.visibleItems.length > 0;
      cachedConversationRefreshing = true;
      if (isAgentDirectConversation(cachedView.conversation)) {
        await clearSelectedAgentFilterForDirectChat();
        if (isStaleConversationLoad(requestId)) {
          return;
        }
      }
      initializing = false;
      initialConversationResolved = true;
      stopInitialLoadTimeout();
      error = '';
      historyError = '';
      sessionsError = '';
      mobileListOpen = false;
      await tick();
      if (isStaleConversationLoad(requestId)) {
        return;
      }
      if (await settleTimelineViewportTransition(conversationId, requestId)) {
        finishConversationSwitch(conversationId, switchRequestId);
      }
      clientPerformance.cachedRestore();
      void showPendingOutboxMessages(conversationId);
      wsClient.subscribeConversation(
        conversationId,
        cachedView.activeSessionLastSeq,
        cachedView.conversation.active_session_id ?? null,
        { replaceCursor: true },
      );
      markConversationReadLocally(conversationId);
       api.conversations.markRead(conversationId).catch(() => {});
       conversationSubloadsLoading = true;
       setCanonicalTimelineAuthority(conversationId, requestId, 'loading');
       // A restored, non-empty canonical timeline with a sync cursor only
       // needs a cursor delta; empty views still hydrate from a snapshot.
       const trustedBase = Boolean(
         chatV2Store.snapshot.conversationId === conversationId
         && chatV2Store.snapshot.cursor
         && chatV2Store.visibleItems.length > 0
       );
       const switchStartedAt = performance.now();
       let refreshPath = 'error';
       const canonicalTimelineRefresh = startCachedTimelineRefresh(
         async () => {
            const appliedBefore = canonicalTimelineApplyRevision;
             await recoverChatV2Canonical(conversationId, requestId);
             refreshPath = 'shared-timeline';
             if (!isStaleConversationLoad(requestId) && historyError) {
               throw new Error(historyError);
             }
             clientPerformance.timelineFresh(
               !isStaleConversationLoad(requestId)
                 && canonicalTimelineApplyRevision > appliedBefore
             );
          },
         () => {
           recordPerf({
             kind: 'switch',
             path: refreshPath,
             ms: performance.now() - switchStartedAt,
             detail: cachedRefreshBannerVisible ? 'banner' : undefined,
           });
           cachedConversationRefreshing = false;
         },
         () => !isStaleConversationLoad(requestId),
        );
       void canonicalTimelineRefresh.catch((caughtError) => {
         if (isStaleConversationLoad(requestId)) return;
         if (isOfflineError(caughtError)) {
           // Offline: keep showing the saved view; reconnect resyncs it.
           setCanonicalTimelineAuthority(conversationId, requestId, 'error');
           historyError = '';
           return;
         }
         setCanonicalTimelineAuthority(conversationId, requestId, 'error');
         historyError = asApiError(caughtError).message;
         reportError(caughtError);
       });
      if (trustedBase) {
        // The cached conversation metadata is already shown; let the timeline
        // delta use the link first instead of racing it on slow networks.
        await canonicalTimelineRefresh.catch(() => undefined);
        if (isStaleConversationLoad(requestId)) return;
      }
      try {
        const [conversation] = await Promise.all([
          api.conversations.detail(conversationId, { includeState: false }),
          reloadConversationSubloads(conversationId, requestId, {
            reloadSessions: true,
            reloadHistory: false,
            resubscribe: true,
            preserveScroll: true,
          }),
        ]);
        if (isStaleConversationLoad(requestId)) {
          return;
        }
        currentConversation = { ...conversation, has_unread: false };
        if (shouldInsertDirectlyLoadedConversation(currentConversation, currentSidebarProjectionFilter())) {
          mergeConversationList([currentConversation]);
        } else {
          removeConversationFromSidebar(currentConversation.conversation_id);
        }
        patchAgentDirectChat(currentConversation);
        persistLastOpenedConversation(currentConversation);
        void refreshQueuedMessages(conversationId);
        // Timeline reconciliation starts immediately above and runs independently
        // from conversation/session metadata. Its banner clears on canonical
        // completion rather than waiting for either metadata request.
        saveCurrentConversationView();
        markConversationReadLocally(conversationId);
        patchConversationInList(conversationId, {
          active_session_id: currentConversation?.active_session_id,
        });
      } catch (caughtError) {
        if (!isStaleConversationLoad(requestId)) {
          if (isOfflineError(caughtError)) {
            // Cached view stays usable; reconnect refreshes metadata.
            cachedConversationRefreshing = false;
            return;
          }
          if (markControllerUnavailable(caughtError, conversationId)) {
            error = '';
            return;
          }
          historyError = asApiError(caughtError).message;
        }
      } finally {
        if (!isStaleConversationLoad(requestId)) {
          conversationSubloadsLoading = false;
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

       if (appContactBindingId(conversation)) {
         activeConversationId = conversationId;
         currentConversation = conversation;
         error = '';
         conversationSubloadsLoading = false;
         timelineInitialPositionSettled = true;
         persistLastOpenedConversation(conversation);
         mergeConversationList([conversation]);
         markConversationReadLocally(conversationId);
         void api.conversations.markRead(conversationId).catch(() => {});
         return;
       }
       activeConversationId = conversationId;
       setCanonicalTimelineAuthority(conversationId, requestId, 'loading');
       currentConversation = conversation;
      error = '';
      initialLoadTimedOut = false;
      persistLastOpenedConversation(conversation);
      if (shouldInsertDirectlyLoadedConversation(conversation, currentSidebarProjectionFilter())) {
        mergeConversationList([conversation]);
      } else {
        removeConversationFromSidebar(conversation.conversation_id);
      }
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
         await chatV2CanonicalRecoveryCoalescer.run(
           conversationId,
           () => reloadConversationSubloads(conversationId, requestId, {
             reloadSessions: true,
             reloadHistory: true,
             resubscribe: true,
           }),
          );
          if (!isStaleConversationLoad(requestId) && !historyError) {
            await settleInitialCanonicalHistory(conversationId, requestId);
          }
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
        has_unread: hostAllowsObservation() ? false : currentConversation?.has_unread,
      };
      markConversationReadLocally(conversationId);
      patchConversationInList(conversationId, {
        active_session_id: currentConversation.active_session_id,
      });
    } catch (caughtError) {
      if (isStaleConversationLoad(requestId)) {
        return;
      }
      if (isOfflineError(caughtError)) {
        // Keep the last-opened pointer: this conversation is fine, the
        // device just never cached it.
        error = 'This conversation is not available offline yet. It will load when you are back online.';
        initializing = false;
        initialConversationResolved = true;
        resetConversationSwitch();
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
      chatV2Store = new ChatV2Store();
      olderMessagesCursor = null;
      hasOlderMessages = false;
      escalations = [];
      escalationResolutionPending = null;
      escalationSettledAck = null;
      locallySettledEscalationCallIds = new Set();
      pendingDirectQuestion = null;
      pendingCredentialRequest = null;
      directQuestionPageIndex = 0;
      directQuestionCollapsed = false;
      directQuestionSubmitting = false;
      directQuestionSubmittingNotificationId = null;
      locallySettledDirectQuestionIds = new Set();
      authoritativelySettledDirectQuestionIds = new Set();
      directQuestionAck = null;
      sessionIds.clear();
      conversationSubloadsLoading = false;
    } finally {
      if (!isStaleConversationLoad(requestId)) {
        if (!appContactBindingId(currentConversation)) {
          await settleTimelineViewportTransition(conversationId, requestId);
        }
        initializing = false;
        initialConversationResolved = true;
        if (currentConversation || error) {
          stopInitialLoadTimeout();
        }
      }
      if (timelineInitialPositionSettled || error) {
        finishConversationSwitch(conversationId, switchRequestId);
      }
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
    return preferredNewChatAgentId(agents.filter(agent => !agent.is_contact_only), [
      selectedAgentIds.length === 1 ? selectedAgentIds[0] : null,
      currentConversation?.agent_id,
    ]);
  }

  function openNewConversationModal(): void {
    newChatHandoff = null;
    newChatCreated = null;
    newChatError = '';
    newChatAgentId = preferredNewConversationAgentId();
    newChatAgentProfileId = '';
    newChatAgentProfileMode = null;
    newChatProjectId = selectedProjectId;
    // On mobile the conversation list is a sliding overlay. Hide it before
    // showing the modal or the modal is obscured by the list drawer.
    mobileListOpen = false;
    showNewChatModal = true;
  }

  async function openNewConversationForSelectedAgent(): Promise<void> {
    newChatHandoff = null;
    newChatCreated = null;
    newChatError = '';
    newChatAgentId = preferredNewConversationAgentId();
    newChatAgentProfileId = '';
    newChatAgentProfileMode = null;
    newChatProjectId = selectedProjectId;
    mobileListOpen = false;
    await tick();
    await createNewConversation();
  }

  function closeNewConversationModal(): void {
    if (newChatCreating) return;
    showNewChatModal = false;
    newChatHandoff = null;
    newChatCreated = null;
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
      const conversation = newChatCreated ?? await api.conversations.create({
        agent_id: agentId,
        agent_profile_id: newChatAgentProfileId || null,
        agent_profile_mode: newChatAgentProfileMode,
        project_id: newChatProjectId || null,
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {}
        }
      });
      newChatCreated = conversation;
      mergeConversationList([conversation]);
      if (newChatHandoff) {
        const saved = await composerDrafts.writeComposite(DRAFT_PREFIX + conversation.conversation_id, {
          input_revision: crypto.randomUUID(), text: newChatHandoff.text,
          attachments: [], chat_mode: null,
        });
        if (!saved) throw new Error('Could not save the handoff draft. Please retry.');
        const source = newChatHandoff.source_refs
          .filter(ref => ref.store === 'intaris').sort((a, b) => b.seq - a.seq)[0];
        if (!source) throw new Error('Handoff source is unavailable.');
        await api.conversations.linkHandoff(newChatHandoffSource, {
          source_session_id: source.session_id, source_event_seq: source.seq,
          recipient_conversation_id: conversation.conversation_id,
        });
        scheduleChatV2CanonicalRecovery(newChatHandoffSource, { immediate: true });
        newChatHandoff = null;
      }
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
    await loadConversationPage(true);
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
        // The caller fails closed and asks for a notification refresh.
      }
    }
    return notificationId;
  }

  async function submitDirectQuestionReply(reply: QuestionSetReply): Promise<boolean> {
    if (!pendingDirectQuestion) return false;
    const pendingStepTool = findPendingStepRequestInputCallFromChatV2();
    const notificationId = await resolveDirectQuestionNotificationId(pendingStepTool);
    if (!notificationId) {
      addToast('Unable to find the pending input request. Refreshing it now.', 'error');
      void refreshPendingDirectQuestion();
      return false;
    }
    const inputKind = pendingInputToolKind(pendingStepTool);
    const responseText = inputKind === 'auth_challenge' ? questionSetReplyText(reply) : '';
    if (inputKind === 'auth_challenge' && !responseText) {
      addToast('Enter a response before sending.', 'error');
      return false;
    }

    beginDirectQuestionSubmission(notificationId);
    markDirectQuestionSettledLocally(notificationId);
    // Hide the form and show a compact acknowledgement immediately, before
    // the resolve REST call returns — forwarded/managed-child resolution can
    // include non-blocking post-commit work, so waiting on the response
    // would otherwise make the UI feel hung. The underlying question,
    // answers, and page/collapse state are left untouched so a failure
    // without an authoritative settle can restore them exactly by clearing
    // this acknowledgement.
    directQuestionAck = buildDirectQuestionAck(notificationId, 'sent', pendingDirectQuestion.managedOrigin);
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
      settleDirectQuestionLocally(notificationId);
      return true;
    } catch (caughtError) {
      if (shouldRestoreDirectQuestionOnSubmitFailure(notificationId, authoritativelySettledDirectQuestionIds)) {
        unmarkDirectQuestionSettledLocally(notificationId);
        directQuestionAck = null;
      }
      clearDirectQuestionSubmission(notificationId);
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
    beginDirectQuestionSubmission(notificationId);
    markDirectQuestionSettledLocally(notificationId);
    directQuestionAck = buildDirectQuestionAck(notificationId, 'cancelled', pendingDirectQuestion.managedOrigin);
    try {
      await api.notifications.resolve(notificationId, { decision: 'cancel' });
      if (pendingStepTool) {
        syncVisibleWindow();
      }
      settleDirectQuestionLocally(notificationId);
      haptic.success();
    } catch (caughtError) {
      if (shouldRestoreDirectQuestionOnSubmitFailure(notificationId, authoritativelySettledDirectQuestionIds)) {
        unmarkDirectQuestionSettledLocally(notificationId);
        directQuestionAck = null;
      }
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to cancel input request');
    } finally {
      clearDirectQuestionSubmission(notificationId);
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
    if (quickProfileBusy || composerAdmissionBusy) return;
    composerAdmissionBusy = true;
    try {
      await prepareAndSendMessage();
    } finally {
      composerAdmissionBusy = false;
    }
  }

  async function prepareAndSendMessage(): Promise<void> {
    let content = composer.trim();
    if ((!content && composerAttachments.length === 0 && !includedFeedback) || !currentConversation || isReadOnly(currentConversation)) return;
    const sendConversationId = currentConversation.conversation_id;
    if (voiceTranscribing) return;
    if (pendingDirectQuestion && directQuestionSubmitting) return;
    const composerView = () => ({
      input_revision: composerInputRevision, chat_mode: composerInputMode,
      conversationId: conversationIdFromRoute(), generation: composerAlive ? conversationLoadRequestId : -1,
      draftKey: currentDraftKey, revision: composerRevision, text: composer,
      attachments: $state.snapshot(composerAttachments) as AttachmentRef[],
      feedback: includedFeedback,
    });
    const submittedView = composerView();
    if (submittedView.conversationId !== sendConversationId
      || submittedView.draftKey !== DRAFT_PREFIX + sendConversationId) return;
    const submissionLease = documentCoordination().capture();
    const submittedChatMode = submittedView.chat_mode;
    const composerAttachmentsSnapshot = submittedView.attachments.filter((item) => !item.voice_recording);

    // STT-first: transcribe any voice recordings, replace the composer body
    // with (or append to) the transcript, and drop the audio from the
    // outgoing turn payload. Matches the channel inbound behavior.
    const voiceRecordings = submittedView.attachments.filter((a) => a.voice_recording);
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
        if (!content && composerAttachmentsSnapshot.length === 0 && !submittedView.feedback) {
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
    await submissionLease.validate();
    // Pending-question/slash resolution belongs to the view that initiated STT.
    if (composerView().generation !== submittedView.generation
      || composerView().conversationId !== submittedView.conversationId) return;

    const normalizedComposerInput = normalizeChatComposerInput({ content });
    const isSlashCommand = normalizedComposerInput.kind === 'command';
    if (isSlashCommand && submittedView.feedback) {
      addToast('Exclude included feedback before running a slash command.', 'error'); return;
    }
    const optimisticContent = normalizedComposerInput.content;
    const optimisticTurnMode = normalizedComposerInput.chatMode ?? submittedChatMode ?? undefined;

    // Detect a pending input tool call sitting in the timeline
    // waiting for a reply. This is the source-of-truth signal that the
    // agent loop is paused for user input. `pendingDirectQuestion` is a
    // mirror of the same state but can drift (stale fetch, missed WS
    // event, reload race) — the tool-call entry does not.
    const pendingStepTool = findPendingStepRequestInputCallFromChatV2();
    const inputKind = pendingInputToolKind(pendingStepTool);
    const isStepInputReply =
      !isSlashCommand && !submittedView.feedback && (pendingDirectQuestion !== null || pendingStepTool !== null);

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
    const directQuestionNotificationId = isStepInputReply
      ? await resolveDirectQuestionNotificationId(pendingStepTool)
      : '';
    if (isStepInputReply && !directQuestionNotificationId) {
      addToast('Unable to find the pending input request. Refreshing it now.', 'error');
      void refreshPendingDirectQuestion();
      return;
    }
    // Optimistic UI. When the message is a step_request_questions reply we do
    // not append a separate user bubble — the tool call block will show the
    // user's answer inline as the resolution. Adding a bubble too would
    // duplicate the content and leave the tool call block stuck as pending.
    let clientMessageId = (!isSlashCommand && !isStepInputReply)
      ? `cmsg_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`
      : null;
    const willQueueBehindActiveTurn = !isSlashCommand && !isStepInputReply && turnInProgress;
    const awaitCanonicalUserMessage =
      !isSlashCommand && !isStepInputReply && shouldAwaitCanonicalUserMessage(optimisticContent);
    let preparedEntry: OutboxEntry | null = null;
    let submission: ComposerSubmission | null = null;
    if (clientMessageId) {
      if (composerLegacyRecovery) { addToast('Confirm sending the recovered draft as a new message.', 'error'); return; }
      if (preparingComposer) return;
      preparingComposer = true;
      const createdAt = new Date().toISOString();
      preparedEntry = {
        client_txn_id: clientMessageId, client_message_id: clientMessageId,
        conversation_id: sendConversationId, content: normalizedComposerInput.content,
        attachments: composerAttachmentsSnapshot, chat_mode: optimisticTurnMode,
        feedback: submittedView.feedback,
        status: 'pending', created_at: createdAt, updated_at: createdAt,
      };
      try {
        // Preserve input before IndexedDB; recovery cannot require the failed
        // database to become readable. No admission or UI clearing on failure.
        submission = new ComposerSubmission(submittedView, preparedEntry, submissionLease);
        preparedEntry = await submission.prepare(composerDrafts, chatV2Outbox, () => {
          if (submission?.matchesInput(composerView())) flushPendingDraft();
        });
        clientMessageId = preparedEntry.client_message_id;
      } catch (persistenceError) {
        preparingComposer = false;
        if (!submission || submission.isCurrentView(composerView())) addToast(asApiError(persistenceError).message, 'error');
        return;
      }
    }
    const canProjectSubmission = () => {
      if (submission) return submission.isCurrentView(composerView());
      try { submissionLease.assertCurrent(); } catch { return false; }
      return composerAlive && composerView().generation === submittedView.generation
        && composerView().conversationId === submittedView.conversationId;
    };
    let consumedNormal = false;
    if (submission) {
      try {
        consumedNormal = await submission.consume(composerDrafts, composerView, () => {
          liveComposer?.consumed(sendConversationId, submittedView.input_revision);
          if (draftSaveTimer !== null) window.clearTimeout(draftSaveTimer);
          draftSaveTimer = null;
           composer = '';
           composerAttachments = [];
            composerInputMode = null;
            includedFeedback = null;
          composerRevision++;
          for (const recording of voiceRecordings) {
            if (recording.blob_url) URL.revokeObjectURL(recording.blob_url);
          }
          if (composerElement) composerElement.value = '';
        });
      } catch (cleanupError) {
        // Persistence already owns the submitted transaction. Failed draft
        // cleanup must not prevent its admission or restore a second copy.
        reportError(cleanupError);
      } finally { preparingComposer = false; }
    }

    if (
      canProjectSubmission() &&
      clientMessageId &&
      shouldRenderOptimisticUserMessage({
        isSlashCommand,
        isStepInputReply,
        awaitCanonicalUserMessage
      })
    ) {
      saveOptimisticUserMessageDraft({
        conversationId: sendConversationId,
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

    if (!isSlashCommand && canProjectSubmission()) {
      latestComposerSubmissionId = clientMessageId;
      lastSubmittedMessage = optimisticContent;
      lastRecoverableMessage = '';
      lastRecoverableTurnId = null;
      suppressedRecoverableTurnId = null;
      if (!willQueueBehindActiveTurn) {
        awaitingAssistantStart = true;
        turnInProgress = true;
        const visualTurnMode = normalizeChatModeTone(optimisticTurnMode ?? persistentChatMode);
        activeTurnChatMode = visualTurnMode;
        patchConversationInList(
          sendConversationId,
          optimisticConversationTurnPatch(visualTurnMode),
          { touchLastMessageAt: true }
        );
      }
    }
    if (canProjectSubmission()) error = '';
    let consumedDirect = false;
    let clearedDirectRevision: string | null = null;
    if (!submission && canProjectSubmission() && composerInputRevision === submittedView.input_revision) {
      try {
        const pinned = await composerDrafts.persistComposite(submittedView.draftKey!, submittedView);
        consumedDirect = pinned !== null && await composerDrafts.consumeComposite(pinned,
          () => canProjectSubmission() && composerInputRevision === submittedView.input_revision);
      } catch (persistenceError) {
        if (canProjectSubmission()) addToast(asApiError(persistenceError).message, 'error');
        return;
      }
      if (consumedDirect && canProjectSubmission() && composerInputRevision === submittedView.input_revision) {
        liveComposer?.consumed(sendConversationId, submittedView.input_revision);
        if (draftSaveTimer !== null) window.clearTimeout(draftSaveTimer);
        draftSaveTimer = null;
        composer = '';
        composerAttachments = [];
        composerRevision++;
        clearedDirectRevision = composerInputRevision;
      } else consumedDirect = false;
    }
    // Clear the native value synchronously and let Svelte flush the
    // reactive binding before measuring scrollHeight. Without the tick,
    // `syncComposerHeight` runs while the textarea still holds the
    // multi-line content, so it "resizes" back to the same height and
    // the composer stays expanded until the user types again.
    if (consumedDirect && composerElement) composerElement.value = '';
    const clearedElement = composerElement;
    await tick();
    if ((consumedDirect && canProjectSubmission() && composerInputRevision === clearedDirectRevision)
      || (submission && consumedNormal && submission.canResize(composerView())
      && composerElement === clearedElement)) syncComposerHeight();
    if (!submission && !canProjectSubmission()) return;
    const attachments = composerAttachmentsSnapshot;

    if (isStepInputReply) {
      beginDirectQuestionSubmission(directQuestionNotificationId);
      markDirectQuestionSettledLocally(directQuestionNotificationId);
      // Same immediate hide+acknowledge contract as the panel submit path.
      directQuestionAck = buildDirectQuestionAck(
        directQuestionNotificationId,
        'sent',
        pendingDirectQuestion?.managedOrigin,
      );
      syncVisibleWindow();
      userScrolledUp = false;
      tailPinned = true;
      scrollToBottom(true);
      try {
        if (inputKind === 'auth_challenge') {
          await api.notifications.resolve(directQuestionNotificationId, {
            decision: 'continue',
            response: content
          });
        } else {
          const questions = directQuestionItems();
          const reply = buildDirectQuestionReply(questions, content);
          if (!directQuestionReplySatisfiesRequired(questions, reply)) {
            unmarkDirectQuestionSettledLocally(directQuestionNotificationId);
            clearDirectQuestionSubmission(directQuestionNotificationId);
            directQuestionAck = null;
            addToast('Answer all required questions before sending.', 'error');
            return;
          }
          await api.notifications.resolve(directQuestionNotificationId, {
            decision: 'continue',
            response_payload: reply as unknown as Record<string, unknown>
          });
        }
        if (canProjectSubmission()) {
          settleDirectQuestionLocally(directQuestionNotificationId);
          haptic.success();
        }
        return;
      } catch (caughtError) {
        if (shouldRestoreDirectQuestionOnSubmitFailure(directQuestionNotificationId, authoritativelySettledDirectQuestionIds)) {
          unmarkDirectQuestionSettledLocally(directQuestionNotificationId);
          directQuestionAck = null;
          if (consumedDirect && canProjectSubmission() && composerInputRevision === clearedDirectRevision) {
            restoreComposerAfterFailedSend(sendConversationId, content);
          }
        }
        clearDirectQuestionSubmission(directQuestionNotificationId);
        addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to submit response');
        return;
      }
    }

    if (canProjectSubmission()) {
      syncVisibleWindow();
      userScrolledUp = false;
      tailPinned = true;
      scrollToBottom(true);
      haptic.success();
    }
    if (isSlashCommand) {
      try {
         const dispatched = await dispatchChatComposerMessage({
           conversationId: sendConversationId,
           content
        });
        if (dispatched.kind === 'command' && canProjectSubmission()) await applyCommandResponse(dispatched.response);
      } catch (caughtError) {
        // Commands are not queued in the outbox; give the text back so a
        // command lost to a flaky connection can be resent.
        if (isRetryableSendError(caughtError) && consumedDirect && canProjectSubmission()
          && composerInputRevision === clearedDirectRevision) {
          restoreComposerAfterFailedSend(sendConversationId, content);
        }
        addToast(asApiError(caughtError).message, 'error', 4000, 'Command failed');
      }
      return;
    }
    const clientTxnId = preparedEntry!.client_txn_id;
    const effectiveClientMessageId = clientMessageId ?? clientTxnId;
    const outboxStored = true;
    const effectiveContent = normalizedComposerInput.content;
    const effectiveChatMode = preparedEntry?.chat_mode ?? undefined;
    const outboxEntry = preparedEntry!;
    try {
       const dispatched = await sendOutboxEntry(chatV2Outbox, outboxEntry);
      if (dispatched.kind !== 'message') throw new Error('Expected message admission');
      const response = dispatched.response;
      if (canProjectSubmission() && chatV2OwnsActiveConversation(sendConversationId)) {
        chatV2Store.applySend(response);
        applyChatV2StateToTimeline();
      }
      // Cursor-correct incremental sync instead of a full snapshot fetch: a
      // snapshot resolved while live WS frames are streaming replaces the
      // cursor with a value BEHIND the frames already applied, making every
      // subsequent frame mismatch -> gapped -> another recovery (visible
      // re-sort/flicker at the start of each turn). /sync advances from the
      // current cursor and falls back to a snapshot only when actually needed.
      // The message is already admitted: a recovery failure here must never
      // be treated as a send failure (reconnect/foreground resync catches up).
      if (canProjectSubmission()) await recoverChatV2Canonical(sendConversationId).catch((recoveryError: unknown) => {
        reportError(recoveryError);
      });
    } catch (caughtError) {
      await projectAdmissionFailure(chatV2Outbox, outboxEntry, async (queued, assertCurrent) => {
      const retryable = isRetryableSendError(caughtError);
      const durablyQueued = queued !== null || outboxStored;
      if (queued && retryable && queued.retryable !== false) {
        scheduleChatV2OutboxRetry(sendConversationId);
      } else if (!durablyQueued) {
        // Persistence failed before admission. Only this case may restore
        // input; queued/unknown outcomes retain their original transaction.
        restoreComposerAfterFailedSend(sendConversationId, content);
        if (sendConversationId === conversationIdFromRoute() && attachments.length > 0) {
          const present = new Set(composerAttachments.map((item) => item.artifact_id));
          composerAttachments = [
            ...attachments.filter((item) => !present.has(item.artifact_id)),
            ...composerAttachments,
          ];
        }
      }
      if (!canProjectSubmission() || !shouldApplyChatSendFailureSideEffects(sendConversationId, conversationIdFromRoute())) return;
      if (awaitCanonicalUserMessage) {
        chatV2Store.addOptimisticUser({
          content: optimisticContent,
          attachments,
          clientMessageId: effectiveClientMessageId,
        });
      }
      if (latestComposerSubmissionId === clientMessageId) {
        awaitingAssistantStart = false;
        turnInProgress = chatV2Store.snapshot.runtime?.has_active_turn === true;
        currentActiveTurnId = chatV2Store.snapshot.runtime?.active_turn?.turn_id ?? null;
      }
      applyChatV2StateToTimeline();
      // Do not yield between reconciliation and failure UI projection.
      assertCurrent();
      // Keep the durable optimistic row visible, but make admission failure
      // explicit instead of leaving it indefinitely in the sending state.
      chatV2Store.markOptimisticUserFailed(effectiveClientMessageId);
      if (durablyQueued && retryable) {
        addToast('Message saved. It will be sent automatically when the connection recovers.', 'info', 4000);
      } else {
        addToast(asApiError(caughtError).message, 'error');
      }
      }, (admitted) => {
        if (admitted.kind === 'message' && canProjectSubmission() && chatV2OwnsActiveConversation(sendConversationId)) {
          chatV2Store.applySend(admitted.response);
          applyChatV2StateToTimeline();
        }
      });
    }
  }

  // Give unsent text back to its conversation's composer/draft. Text typed
  // since the send is kept; the failed message is placed before it.
  function restoreComposerAfterFailedSend(conversationId: string, content: string): void {
    const draftKey = DRAFT_PREFIX + conversationId;
    if (draftKey !== currentDraftKey) {
      writeDraft(draftKey, mergeFailedSendIntoDraft(content, readDraft(draftKey)));
      return;
    }
    const merged = mergeFailedSendIntoDraft(content, composer);
    editComposerInput();
    composer = merged;
    if (composerElement) composerElement.value = merged;
    writeDraft(draftKey, merged);
    void tick().then(syncComposerHeight);
  }

  // Show picked files immediately and upload them in the background. Sending
  // does not wait for uploads; the outbox resolves them before admission.
  async function uploadFiles(files: File[]): Promise<void> {
    if (files.length === 0) return;
    const staged = files.map((file) => attachmentUploads.stage(file));
    composerAttachments = [...composerAttachments, ...staged];
    editComposerInput();
  }

  function applyAttachmentUploadPatch(localId: string, patch: Partial<AttachmentRef>): void {
    if (!composerAttachments.some((item) => item.local_id === localId)) return;
    composerAttachments = composerAttachments.map((item) => (
      item.local_id === localId ? { ...item, ...patch } : item
    ));
  }
  onDestroy(attachmentUploads.onChange(applyAttachmentUploadPatch));

  function cancelComposerAttachment(attachment: AttachmentRef): void {
    if (attachment.local_id) attachmentUploads.cancel(attachment.local_id);
  }

  function retryComposerAttachment(attachment: AttachmentRef): void {
    if (attachment.local_id) attachmentUploads.retry(attachment.local_id);
  }

  /**
   * Finish background uploads for an outbox entry and persist the server
   * references so a later retry does not upload the files again.
   */
  async function resolveOutboxAttachments(entry: OutboxEntry): Promise<AttachmentRef[]> {
    if (!entry.attachments.some(isLocalAttachment)) return entry.attachments.map(toServerAttachment);
    const resolved = await attachmentUploads.resolveAll(entry.attachments);
    await chatV2Outbox.update(entry.client_txn_id, { attachments: resolved }).catch(() => undefined);
    return resolved;
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
    const removed = composerAttachments.find((item) => item.artifact_id === artifactId);
    composerAttachments = composerAttachments.filter((item) => item.artifact_id !== artifactId);
    editComposerInput();
    if (removed) cancelComposerAttachment(removed);
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
    await handleClipboardFilePaste(event, uploadFiles, { filterFiles: uniqueNewPastedFiles });
  }

  async function retryFailedTurn(turnId: string | null): Promise<void> {
    if (!currentConversation || !turnId || turnInProgress || awaitingAssistantStart) return;
    error = '';
    awaitingAssistantStart = true;
    try {
      await chatV2Api.retryTurn(currentConversation.conversation_id, turnId, {
        client_txn_id: crypto.randomUUID()
      });
      suppressedRecoverableTurnId = turnId;
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

  async function handleEscalationDecision(
    callId: string,
    decision: 'approve' | 'deny',
    note?: string,
  ): Promise<boolean> {
    const current = escalations.find((item) => item.call_id === callId) ?? null;
    if (current === null) return false;
    escalationResolutionPending = current;
    escalationBusyCallId = callId;
    try {
      await api.notifications.resolve(callId, { decision, note });
      // The REST response is authoritative: settle the prompt immediately
      // rather than waiting indefinitely for a WebSocket acknowledgement or
      // a canonical state refresh. Later confirmations are reconciled
      // idempotently (dedup by call_id) once they arrive.
      escalations = escalations.filter((item) => item.call_id !== callId);
      locallySettledEscalationCallIds = new Set(locallySettledEscalationCallIds).add(callId);
      escalationSettledAck = { callId, toolName: current.tool_name, decision };
      escalationResolutionPending = null;
      escalationBusyCallId = null;
      if (escalations.length === 0) stopEscalationCountdown();
      return true;
    } catch (caughtError) {
      const error = asApiError(caughtError);
      if (isResolvedApprovalRejection(error.details)) {
        escalations = escalations.filter((item) => item.call_id !== callId);
        locallySettledEscalationCallIds = new Set(locallySettledEscalationCallIds).add(callId);
        escalationSettledAck = error.details?.decision === 'deny'
          ? { callId, toolName: current.tool_name, decision: 'deny' }
          : null;
        if (escalations.length === 0) stopEscalationCountdown();
      }
      escalationResolutionPending = null;
      escalationBusyCallId = null;
      addToast(error.message, 'error', 4000, 'Unable to resolve approval');
      return false;
    }
  }

  async function sendConversationModeText(text: string): Promise<string | null> {
    if (!currentConversation || !text.trim()) throw new Error('No active voice conversation.');
    const conversationId = currentConversation.conversation_id;
    const clientTxnId = crypto.randomUUID();
    try {
      const result = await dispatchChatComposerMessage({
        conversationId,
        clientTxnId,
        clientMessageId: clientTxnId,
        content: text.trim(),
        attachments: [],
      }, { outbox: chatV2Outbox });
      if (result.kind === 'command') return null;
      return result.response.client_message_id;
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to send message');
      throw caughtError;
    }
  }

  async function reconcileConversationModeTurn(clientMessageId: string) {
    if (!currentConversation) throw new Error('Voice conversation is no longer active.');
    const conversationId = currentConversation.conversation_id;
    const admission = await chatV2Api.messageAdmission(conversationId, clientMessageId);
    if (!['completed', 'failed', 'cancelled', 'absorbed'].includes(admission.status)) return null;
    const recovered = await recoverChatV2Snapshot(conversationId);
    if (!recovered || chatV2Store.snapshot.conversationId !== conversationId) {
      throw new Error('Voice response recovery is not complete.');
    }
    return { turn_id: admission.turn_id, items: chatV2Store.visibleItems };
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
    if (userScrolledUp || visibleWindowSize() >= TIMELINE_WINDOW_MAX_ROWS * 3) return false;
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

    const conversationId = currentConversation.conversation_id;
    const loadRequestId = conversationLoadRequestId;
    const controller = new AbortController();
    olderMessagesAbortController?.abort();
    olderMessagesAbortController = controller;
    loadingOlderMessages = true;
    try {
      const beforeIds = new Set(renderableVisibleItems.map(item => item.id));
      const beforeCursor = olderMessagesCursor;
      const response = await bindTimeline(conversationId).olderVisible(olderMessagesCursor, controller.signal);
      if (
        controller.signal.aborted
        || currentConversation?.conversation_id !== conversationId
        || conversationLoadRequestId !== loadRequestId
      ) return false;
      if (!response) {
        void recoverChatV2Canonical(
          conversationId,
          loadRequestId,
        );
        return false;
      }
      applyChatV2StateToTimeline();
      olderMessagesCursor = response.before_cursor ?? null;
      hasOlderMessages = Boolean(response.has_more_before && olderMessagesCursor);

      saveCurrentConversationView();
      await tick();
      await nextAnimationFrame();
      scheduleScrollToBottom(false, 1);

      return response.items.some(item => !beforeIds.has(item.id)) || beforeCursor !== olderMessagesCursor;
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === 'AbortError') return false;
      if (
        olderMessagesAbortController !== controller
        || currentConversation?.conversation_id !== conversationId
        || conversationLoadRequestId !== loadRequestId
      ) return false;
      const message = asApiError(caughtError).message;
      if (isRecoverableBackfillCursorError(caughtError)) {
        olderMessagesCursor = null;
        hasOlderMessages = false;
      } else if (!isConnectivityError(caughtError)) {
        // Offline/timeouts: the global banner covers it; scrolling retries.
        addToast(`Unable to load older messages: ${message}`, 'error');
      }
      return false;
    } finally {
      if (olderMessagesAbortController === controller) {
        olderMessagesAbortController = null;
        loadingOlderMessages = false;
      }
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
        const next = expandWindowUpPreservingLiveTail(timelineWindow, renderableVisibleItems.length);
        if (wasTailPinned) {
          next.start = Math.max(next.start, renderableVisibleItems.length - TIMELINE_WINDOW_MAX_ROWS * 3);
          tailWindowRows = timelineWindowSize(next, renderableVisibleItems.length);
        }
        await applyAnchoredTimelineWindow(next, wasTailPinned ? null : captureTimelineAnchor());
        await nextAnimationFrame();
        if (wasTailPinned) {
          scheduleScrollToBottom(false, 1);
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
    let requestController: AbortController | null = null;
    let requestConversationId: string | null = null;
    let requestLoadId: number | null = null;

    try {
      // Element-anchored restore: the reference row stays visually fixed across
      // the prepend, immune to a simultaneous tail-window change. The anchor is
      // captured immediately before each DOM-mutating apply — never before the
      // network fetch (capturing before async work snapped actively-scrolling
      // users back to the pre-fetch position).
      if (visibleStartIndex > 0) {
        await applyAnchoredTimelineWindow(expandWindowUp(timelineWindow, renderableVisibleItems.length));
        loadingOlderMessages = false;
      } else if (olderMessagesCursor && currentConversation) {
        const conversationId = currentConversation.conversation_id;
        const loadRequestId = conversationLoadRequestId;
        const controller = new AbortController();
        requestController = controller;
        requestConversationId = conversationId;
        requestLoadId = loadRequestId;
        olderMessagesAbortController?.abort();
        olderMessagesAbortController = controller;
        const response = await bindTimeline(conversationId).olderVisible(olderMessagesCursor, controller.signal);
        if (
          controller.signal.aborted
          || currentConversation?.conversation_id !== conversationId
          || conversationLoadRequestId !== loadRequestId
        ) return;
        if (!response) {
          loadingOlderMessages = false;
          void recoverChatV2Canonical(
            conversationId,
            loadRequestId,
          );
          return;
        }
        applyChatV2StateToTimeline();
        olderMessagesCursor = response.before_cursor ?? null;
        hasOlderMessages = Boolean(response.has_more_before && olderMessagesCursor);
        saveCurrentConversationView();
        await tick();
        loadingOlderMessages = false;
      } else {
        loadingOlderMessages = false;
      }
    } catch (caughtError) {
      if (
        requestController !== null
        && (
          olderMessagesAbortController !== requestController
          || currentConversation?.conversation_id !== requestConversationId
          || conversationLoadRequestId !== requestLoadId
        )
      ) return;
      if (caughtError instanceof DOMException && caughtError.name === 'AbortError') {
        loadingOlderMessages = false;
        return;
      }
      const message = asApiError(caughtError).message;
      // A stale before-cursor after compaction/lineage change is recoverable:
      // clear it so the next scroll re-fetches from the fresh window instead of
      // failing forever. Only surface a toast for genuinely unexpected errors.
      if (isRecoverableBackfillCursorError(caughtError)) {
        olderMessagesCursor = null;
        hasOlderMessages = false;
      } else if (!isConnectivityError(caughtError)) {
        // Offline/timeouts: the global banner covers it; scrolling retries.
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
    if (!pageAlive || timelineController?.current === false) return;
    const currentId = conversationIdFromRoute();
    if (eventNeedsTreeRefresh(event as { type: string } & Record<string, unknown>)) {
      scheduleTreeRefresh();
    }
    if (event.type === 'work_invalidated') {
      if (!sidebarRevisionAdmission.observeInvalidation(event.revision)) return;
      scheduleSidebarReconciliation('work-invalidation');
    } else if (
      currentId
      && (!('conversation_id' in event) || event.conversation_id === currentId)
      && event.type === 'scope_invalidated'
      && event.reason === 'chat_scope_changed'
    ) {
      invalidateWorkScope(conversationTimelineScope(currentId).key);
    }
    const chatV2OwnsCurrent = chatV2OwnsActiveConversation(currentId);
    const legacyLifecycleEnabled = shouldApplyLegacyLifecycleFrame(chatV2OwnsCurrent);
    if (event.type === 'chat_v2_frame') {
      if (event.conversation_id === currentId) {
        void applyChatV2Frame(event);
      }
      return;
    }
    if (event.type === 'scope_invalidated') {
      if (event.reason === 'sidebar_changed' && !event.conversation_id) {
        scheduleSidebarReconciliation('owner-sidebar-invalidation');
      }
      if (event.reason === 'executor_state_changed') {
        scheduleSidebarReconciliation('cluster-invalidation');
      }
      if (event.reason === 'notification_state_changed') {
        void refreshPendingNotifications();
      }
      if (timelineController?.admitInvalidation(event)) {
        scheduleChatV2CanonicalRecovery(currentId, { immediate: true });
      }
      return;
    }
    const eventSessionId = 'session_id' in event && typeof event.session_id === 'string' ? event.session_id : null;
    if (event.type === 'recap_available' || event.type === 'handoff_available') {
      scheduleChatV2CanonicalRecovery(event.conversation_id, { immediate: true });
      return;
    }
    const eventPreviousSessionId = event.type === 'session_compacted' ? event.previous_session_id : null;
    // A new conversation was created on another device/tab. Newer servers send
    // the hydrated sidebar row so this can stay local; fall back to a full
    // projection refresh for older/no-row payloads.
    if (event.type === 'sidebar_conversation_upsert') {
      const upsert = event as typeof event & {
        conversation?: Conversation | null;
        revision?: string;
      };
      if (upsert.conversation) {
        applySidebarConversationUpsert(upsert.conversation, upsert.revision);
        const row = upsert.conversation;
        if (row.conversation_id !== currentId && (row.has_active_turn || row.has_unread)) {
          requestActivityPreload(row.conversation_id, row.has_active_turn === true);
        }
      } else {
        scheduleSidebarReconciliation('missing-sidebar-upsert');
      }
      return;
    }
    if (event.type === 'sidebar_conversation_removed') {
      const removal = event as typeof event & { revision?: string };
      const admission = sidebarRevisionAdmission.admitPush(
        removal.conversation_id,
        removal.revision,
      );
      if (admission.reconcile) scheduleSidebarReconciliation('sidebar-removal-gap');
      if (!admission.apply) return;
      sidebarNetworkProjectionApplied = true;
      removeConversationFromSidebar(removal.conversation_id, {
        markCurrentDeleted: removal.conversation_id === currentId,
        canonicalRemoval: true,
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
      // Canonical sidebar upserts own unread and lifecycle state. Scoped
      // conversation events only drive secondary UI such as notifications.
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
        if (event.has_active_turn === true || typeof event.last_message_at === 'string') {
          requestActivityPreload(event.conversation_id, event.has_active_turn === true);
        }
        const { patch } = orderedConversationRuntimePatch(event.conversation_id, event);
        if (Object.keys(patch).length > 0) {
          patchConversationInList(event.conversation_id, patch, {
            touchUpdatedAt: typeof event.title === 'string' || typeof event.updated_at === 'string',
            touchLastMessageAt: typeof event.last_message_at === 'string',
            pendingNotificationSource: Array.isArray(event.pending_notification_types) ? 'server' : undefined,
          });
        }
      }
      if (event.type === 'message_complete' || event.type === 'workflow_completed' || event.type === 'workflow_failed') {
        // Turn finished elsewhere: land its final state in the background.
        requestActivityPreload(otherConvId, false);
        const idx = conversations.findIndex((c) => c.conversation_id === otherConvId);
        const agentDirectConversation = agentDirectChats.find(
          (item) => item.conversation.conversation_id === otherConvId
        )?.conversation;
        const conversation = idx >= 0 ? conversations[idx] : agentDirectConversation ?? null;
        const agentWorkConversation = isManagedConversation(conversation);
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
            last_message_at: event.completed_at ?? undefined,
            ...(hostAllowsObservation() ? {
              has_unread: false,
              last_read_at: event.completed_at ?? new Date().toISOString(),
            } : {}),
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
        if (hostAllowsObservation()) {
          patchConversationInList(currentConversation.conversation_id, { has_unread: false });
        }
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
      if (event.command === '/profile' && event.resolved_agent_profile_id && event.conversation_id) {
        scheduleChatV2CanonicalRecovery(event.conversation_id, { immediate: true });
        return;
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
      if ('messages' in event && Array.isArray(event.messages)) {
        applyQueuedMessageSnapshot(event.messages, event.queued_count ?? event.messages.length);
      } else if ((event.queued_count ?? 0) === 0) {
        applyQueuedMessageSnapshot([], 0);
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
      if (providerNoticeOwnsSocketError(event.code, $workspaceHealth.health)) {
        error = '';
        awaitingAssistantStart = false;
        turnInProgress = false;
        clearConversationTurnState(currentConversation?.conversation_id);
        directQuestionSubmitting = false;
        void workspaceHealth.refresh();
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
      if (currentConversation && !document.hidden && hostAllowsObservation()) {
        markConversationObservedByServiceWorker(
          currentConversation.conversation_id,
          event.type === 'message_complete' ? event.completed_at : null,
        );
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
    // Escalation push events. Latency/recovery path: canonical
    // conversation-state hydration (applyConversationStateSnapshot) is the
    // primary source, so a missed frame here must not delay the prompt.
    // Dedup by call_id against both the current queue and any locally
    // settled (REST-resolved) call so a stale/reordered frame cannot
    // resurrect an already-settled escalation.
    if (event.type === 'escalation') {
      const existing = escalations.find((e) => e.call_id === event.call_id);
      if (!existing
        && escalationResolutionPending?.call_id !== event.call_id
        && !locallySettledEscalationCallIds.has(event.call_id)
      ) {
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
          managed_conversation_title: event.managed_conversation_title ?? null,
          managed_target_agent_id: event.managed_target_agent_id ?? null,
          managed_origin_conversation_id: event.managed_origin_conversation_id ?? null,
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
      if (escalationSettledAck?.callId === event.call_id) {
        escalationSettledAck = null;
      }
      if (locallySettledEscalationCallIds.has(event.call_id)) {
        const next = new Set(locallySettledEscalationCallIds);
        next.delete(event.call_id);
        locallySettledEscalationCallIds = next;
      }
      if (escalations.length === 0) stopEscalationCountdown();
      return;
    }

    // Handle conversation_updated for title and activity changes.
    if (event.type === 'conversation_updated') {
      if (currentConversation && event.conversation_id === currentConversation.conversation_id) {
        const { patch, runtimeApplied } = orderedConversationRuntimePatch(
          currentConversation.conversation_id,
          event,
        );
        if (chatV2OwnsCurrent) {
          delete patch.has_active_turn;
          delete patch.active_turn_chat_mode;
          delete patch.active_turn_chat_mode_source;
        } else if (runtimeApplied && typeof event.has_active_turn === 'boolean') {
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
      if (currentId) {
        invalidateWorkScope(conversationTimelineScope(currentId).key, { reconnect: true });
      }
      scheduleTreeRefresh();
      void resyncSidebarData('websocket-reconnect', { force: true });
      if (chatV2OwnsCurrent) {
        scheduleChatV2CanonicalRecovery(currentId, { immediate: true });
      }
      if (currentId) void drainChatV2Outbox(currentId);
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
        if (locallySettledDirectQuestionIds.has(event.notification_id)) return;
        if (currentId) beginPendingNotificationRefresh(currentId);
        const nextQuestion = pendingDirectQuestionFromQuestionSet(
          event.notification_id,
          event.step_name,
          event.questions,
          event.context,
          event,
        );
        nextQuestion.observedStateVersion = backendConversationState?.state_version ?? 0;
        if (shouldResetPendingDirectQuestionForm(
          pendingDirectQuestion?.notificationId,
          nextQuestion.notificationId,
        )) {
          directQuestionAnswers = restoreDirectQuestionDraft(nextQuestion);
          directQuestionPageIndex = 0;
          directQuestionCollapsed = false;
          clearDirectQuestionSubmission();
          directQuestionAck = null;
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
        if (locallySettledDirectQuestionIds.has(event.notification_id)) return;
        if (currentId) beginPendingNotificationRefresh(currentId);
        const nextQuestion = pendingDirectQuestionFromAuthChallengeEvent(event);
        if (!nextQuestion) return;
        nextQuestion.observedStateVersion = backendConversationState?.state_version ?? 0;
        if (shouldResetPendingDirectQuestionForm(
          pendingDirectQuestion?.notificationId,
          nextQuestion.notificationId,
        )) {
          directQuestionAnswers = restoreDirectQuestionDraft(nextQuestion);
          directQuestionPageIndex = 0;
          directQuestionCollapsed = false;
          clearDirectQuestionSubmission();
          directQuestionAck = null;
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

    if (
      (event.type === 'workflow_step_question_resolved' || event.type === 'auth_challenge_resolved')
      && event.notification_id
    ) {
      if (currentId) beginPendingNotificationRefresh(currentId);
      markDirectQuestionSettledAuthoritatively(event.notification_id);
      settleDirectQuestionLocally(event.notification_id);
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

  async function loadProjects(): Promise<void> {
    try {
      projects = await api.projects.list();
    } catch {
      projects = [];
    }
    if (selectedProjectId && !projects.some((project) => project.project_id === selectedProjectId)) {
      selectedProjectId = '';
      persistSelectedProject();
    }
  }

  function persistSelectedProject(): void {
    if (typeof window === 'undefined') return;
    if (selectedProjectId) {
      window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedProject, selectedProjectId);
    } else {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.selectedProject);
    }
  }

  async function handleProjectFilterChange(): Promise<void> {
    persistSelectedProject();
    resetConversationSearchResults();
    await loadSidebarProjection();
  }

  function toggleSidebarProject(projectId: string): void {
    selectedProjectId = selectedProjectId === projectId ? '' : projectId;
    void handleProjectFilterChange();
  }

  function patchCurrentConversation(updated: Conversation): void {
    if (currentConversation?.conversation_id === updated.conversation_id) {
      currentConversation = { ...currentConversation, ...updated };
    }
    patchConversationInList(updated.conversation_id, updated);
  }

  async function changeConversationProject(projectId: string | null): Promise<void> {
    if (!currentConversation) return;
    const target = projects.find((project) => project.project_id === projectId) ?? null;
    const targetShared = target
      && (target.is_shared_with_me || (target.grants ?? []).some((grant) => !grant.revoked_at));
    // A chat without a project becomes shared when it is added to one.
    if (
      targetShared
      && (!currentConversation.project_id || currentConversation.visibility !== 'private')
      && typeof window !== 'undefined'
      && !window.confirm(`"${target?.name}" is shared. Project members will be able to read this chat. Continue?`)
    ) {
      return;
    }
    projectChangeBusy = true;
    try {
      const updated = await api.conversations.update(currentConversation.conversation_id, { project_id: projectId });
      patchCurrentConversation(updated);
      addToast(target ? `Moved to ${target.name}.` : 'Removed from project.', 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to change project');
    } finally {
      projectChangeBusy = false;
    }
  }

  async function changeConversationVisibility(visibility: ResourceVisibility): Promise<void> {
    if (!currentConversation) return;
    projectChangeBusy = true;
    try {
      const updated = await api.conversations.update(currentConversation.conversation_id, { visibility });
      patchCurrentConversation(updated);
      addToast(visibility === 'private' ? 'Chat is now private.' : 'Chat is shared with the project.', 'success');
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to change visibility');
    } finally {
      projectChangeBusy = false;
    }
  }

  function sharedConversationAuthor(conversation: Conversation | null): string | null {
    return conversation?.is_readonly_for_caller ? conversation.user_email : null;
  }

  function canContinueSharedConversation(conversation: Conversation | null): boolean {
    return Boolean(conversation?.is_readonly_for_caller && conversation.can_continue);
  }

  async function continueSharedConversation(): Promise<void> {
    const source = currentConversation;
    if (!source || !canContinueSharedConversation(source)) return;
    newChatCreating = true;
    try {
      const conversation = await api.conversations.create({
        agent_id: source.agent_id,
        project_id: source.project_id,
        title: `Continue: ${conversationTitle(source)}`,
        context: {
          type: 'web',
          ref: null,
          platform_data: { continued_from_conversation_id: source.conversation_id },
          memory_labels: {}
        }
      });
      mergeConversationList([conversation]);
      composer = `Continuing from the shared conversation "${conversationTitle(source)}" by ${source.user_email} (/chat/${source.conversation_id}). `;
      await goto(conversationUrl(conversation.conversation_id));
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to continue');
    } finally {
      newChatCreating = false;
    }
  }

  async function handleAgentFilterChange(): Promise<void> {
    persistSelectedAgent();
    resetConversationSearchResults();
    await loadConversationPage(true);
  }

  provideChatNavigation(
    () => childView ? childViewScope(childView) : conversationTimelineScope(currentConversation?.conversation_id ?? routeConversationId),
    handleViewSession,
  );

  let childNavigationGeneration = 0;
  async function handleViewSession(sessionId: string, selectedNode?: WorkstreamRef): Promise<void> {
    if (!currentConversation) return;
    const generation = ++childNavigationGeneration;
    const sourceConversationId = currentConversation.conversation_id;
    const sourceView = childView;
    const nodes = scopedRootActivityOverview?.workstreams ?? [];
    let node = selectedNode
      ?? workstreamForSession(nodes, sessionId)
      ?? fallbackWorkstream(
        sessionId,
        treeRootConversationId ?? currentConversation.conversation_id,
        currentConversationWork,
      );
    if (!node.conversation_id) {
      try {
        const resolved = await resolveChildWorkstreamIdentity(
          node, (id) => focusedSessionIdentityLoader.resolve(id),
        );
        if (
          generation !== childNavigationGeneration
          || currentConversation?.conversation_id !== sourceConversationId
          || childView !== sourceView
        ) return;
        if (!resolved) return;
        node = resolved;
      } catch (caughtError) {
        if (generation === childNavigationGeneration && currentConversation?.conversation_id === sourceConversationId) {
          addToast(asApiError(caughtError).message, 'error', 4000, 'Unable to open session');
        }
        return;
      }
    }
    const navigation = treeSessionNavigation(inspectorPresentation);
    if (navigation.closeInspectorOverlay && headerInfoOpen) {
      closeHeaderInfo(false);
    }
    if (node.key === node.root_key) {
      closeSubSessionPanel();
      return;
    }
    // Drop only the session being left, not the whole set: other children
    // may still be genuinely active. The departed session stops receiving
    // live onRuntimeActiveChange updates once its ChildChatView unmounts, so
    // its stale entry (if any) must not linger and keep forcing it "Running"
    // forever; its coarse `activity_state`/`status` from the periodically
    // refreshed activity overview remains authoritative afterward.
    const leavingSessionId = childView?.sessionId;
    if (leavingSessionId && leavingSessionId !== sessionId) {
      const next = new Set(runtimeActiveChildSessionIds);
      next.delete(leavingSessionId);
      runtimeActiveChildSessionIds = next;
    }
    const nextChildView = childViewForWorkstream(node, treeRootConversationId ?? currentConversation.conversation_id);
    const canonicalSessionId = nextChildView.sessionId;
    const traversal = traverseInspectorSession({
      drawerOpen: conversationInfoDrawer.open,
      activeTab: conversationInfoDrawer.tab,
      presentation: inspectorPresentation,
      focusedSessionId,
      middleSessionId: childView?.sessionId ?? null,
      workSessionId: focusedSessionId,
    }, canonicalSessionId);
    if (!childView) {
      parentSurfaceScrollTop = timelineEl?.scrollTop ?? 0;
      parentSurfaceAnchor = captureTimelineAnchor();
    }
    childView = nextChildView;
    selectedChildWorkstream = node;
    childManagedConversation = null;
    childManagedError = '';
    if (nextChildView.kind === 'managed') {
      const requestedConversationId = nextChildView.conversationId;
      void api.conversations.detail(requestedConversationId, { includeState: false })
        .then((conversation) => {
          if (childView?.kind === 'managed' && childView.conversationId === requestedConversationId) {
            childManagedConversation = conversation;
          }
        })
        .catch((caughtError) => {
          if (childView?.kind === 'managed' && childView.conversationId === requestedConversationId) {
            childManagedError = asApiError(caughtError).message;
          }
        });
    }
    focusInspectorSession(traversal.focusedSessionId!, childViewScope(nextChildView));
    if (headerInfoMode === 'session') void tick().then(loadSessionInfo);
  }

  function navigateToStructuralParent(): void {
    childNavigationGeneration += 1;
    if (childView) {
      const parentView = parentChildView(scopedRootActivityOverview?.workstreams ?? [], childView);
      if (parentView) {
        const parentNode = childViewWorkstream(scopedRootActivityOverview?.workstreams ?? [], parentView);
        if (parentNode) {
          handleViewSession(parentView.sessionId, parentNode);
          return;
        }
      }
    }
    const currentSessionId = focusedSessionId ?? childView?.sessionId ?? null;
    const parentSessionId = structuralParentSessionId(
      scopedRootActivityOverview?.workstreams ?? [],
      currentSessionId,
    );
    if (parentSessionId) {
      handleViewSession(parentSessionId);
      return;
    }
    backChildViewToRoot(cancelActivityOverviewLoad, () => {
      focusedSessionId = null;
      childView = null;
      selectedChildWorkstream = null;
      activityOverview = rootActivityOverview;
      const conversationId = currentConversation?.conversation_id;
      const activeSessionId = currentConversation?.active_session_id;
      sessionInfo = conversationId && activeSessionId ? getSessionInfo(conversationId, activeSessionId) : null;
      applyCachedSessionDiagnostics(activeSessionId ?? null, sessionInfo);
    });
  }

  function closeSubSessionPanel(): void {
    childNavigationGeneration += 1;
    closeChildViewToRoot(cancelActivityOverviewLoad, () => {
      // Closing the sub-session panel returns to the root tree, where other
      // children may still be genuinely running; drop only the departed
      // child's entry (its ChildChatView unmounts and stops reporting live
      // updates), never the whole runtime-active set.
      if (childView?.sessionId) {
        const next = new Set(runtimeActiveChildSessionIds);
        next.delete(childView.sessionId);
        runtimeActiveChildSessionIds = next;
      }
      childView = null;
      selectedChildWorkstream = null;
      childManagedConversation = null;
      childManagedError = '';
      focusedSessionId = null;
      focusedSessionIdentityLoader.cancel();
      explicitFocusedIdentity = null;
      activityOverview = rootActivityOverview;
    });
  }

  let childManagedSelectionGeneration = 0;
  $effect(() => {
    childView?.sessionId;
    childView?.conversationId;
    childManagedSelectionGeneration += 1;
    childManagedBusy = null;
  });

  async function runChildManagedAction(
    action: 'stop' | 'send' | 'take-control' | 'close',
    payload?: Record<string, unknown>,
  ): Promise<void> {
    if (!childManagedConversation || childManagedBusy) return;
    childManagedBusy = action;
    childManagedError = '';
    const conversationId = childManagedConversation.conversation_id;
    const generation = childManagedSelectionGeneration;
    try {
      const result = await performManagedAction(childManagedConversation, action, payload);
      if (generation !== childManagedSelectionGeneration || childManagedConversation?.conversation_id !== conversationId) return;
      if (result) childManagedConversation = result.conversation;
      scheduleTreeRefresh();
      if (result?.followUpConversationId) await goto(conversationUrl(result.followUpConversationId));
    } catch (caughtError) {
      if (generation === childManagedSelectionGeneration && childManagedConversation?.conversation_id === conversationId) childManagedError = asApiError(caughtError).message;
    } finally {
      if (generation === childManagedSelectionGeneration && childManagedConversation?.conversation_id === conversationId) childManagedBusy = null;
    }
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
      if (timelineEl && timelineInitialPositionSettled && userScrolledUp && !timelineWindowPending
        && !childView && timelineReadingPosition) {
        // Markdown, disclosure and media layout can settle after the window's
        // DOM commit. The same canonical anchor owns that later resize too.
        if (Math.abs(timelineEl.scrollTop - timelineReadingPosition.scrollTop) < 1) {
          restoreTimelineAnchor(timelineReadingPosition);
        } else {
          // Input during the programmatic-scroll guard supersedes our saved
          // position; do not rewind that gesture when late content resizes.
          timelineReadingPosition = captureTimelineAnchor();
        }
      }
      // Re-pin iff the user is pinned to the tail. tailPinned is always kept
      // in sync with !userScrolledUp (single source of truth), so this is
      // equivalent to: if (!userScrolledUp || bottomScrollPending) re-pin.
      if (
        timelineInitialPositionSettled
        && shouldPreserveLiveTailOnResize({ tailPinned, autoScrollPending: bottomScrollPending })
      ) {
        scheduleScrollToBottom(false, 2);
      }
      if (timelineInitialPositionSettled) scheduleTimelineViewportFill();
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

    if (!currentConversation || conversationSubloadsLoading || !timelineInitialPositionSettled) return;
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
      const handle = registerOverlay({ kind: 'sheet', blocksChrome: false, root: () => conversationSidebar });
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
      if (event.key === 'Escape' && chatSearchOpen) {
        event.preventDefault();
        closeChatSearch();
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
    if (findOnlySearchConversationId === currentConversation?.conversation_id || !chatSearchOpen || seededChatSearchRan || !chatSearchQuery.trim() || !currentConversation) {
      return;
    }
    if (conversationSubloadsLoading) return;
    seededChatSearchRan = true;
    void runChatSearch();
  });

  $effect(() => {
    const conversationId = currentConversation?.conversation_id ?? '';
    if (findOnlySearchConversationId === conversationId || !chatSearchOpen || !chatSearchQuery.trim() || !conversationId) return;
    if (conversationSubloadsLoading) return;
    if (lastChatSearchConversationId && conversationId !== lastChatSearchConversationId) {
      chatSearchResults = [];
      chatSearchSelectedIndex = 0;
      void runChatSearch();
    }
  });

  let visibleConversationList = $derived.by(() => {
    if (conversationSearch.trim() && !conversationSearchSubmitted) {
      return conversationTitleSearchResults.filter(
        (conversation) => !isAgentDirectConversation(conversation),
      );
    }
    return sortConversationsByActivity(mergeWaitingConversations(conversations, waitingConversations))
      .filter((conversation) => !isAgentDirectConversation(conversation)
        && conversationMatchesSidebarProjectionFilter(conversation, currentSidebarProjectionFilter()));
  });
  let visibleConversationSections = $derived.by(() => groupConversationsByActivity(visibleConversationList, historySectionNow));
  let visibleConversationLanes = $derived.by(() =>
    partitionSidebarConversationLanes(visibleConversationList, backgroundWork.items)
  );
  let visibleHistorySections = $derived.by(() => groupConversationsByActivity(
    visibleConversationLanes.recent,
    historySectionNow,
  ));

  function openSidebarAttention(action: AttentionActionSummary, origin: HTMLElement): void {
    selectedSidebarAttentionOrigin = origin;
    selectedSidebarAttentionTitle = action.title;
    selectedSidebarAttentionId = action.action_id;
  }

  function closeSidebarAttention(): void {
    selectedSidebarAttentionId = null;
    selectedSidebarAttentionTitle = '';
    const origin = selectedSidebarAttentionOrigin;
    selectedSidebarAttentionOrigin = null;
    queueMicrotask(() => {
      if (origin?.isConnected) origin.focus({ preventScroll: true });
    });
  }

  async function sidebarAttentionSettled(action: AttentionActionDetail): Promise<void> {
    if (selectedSidebarAttentionId === action.action_id) closeSidebarAttention();
    await forceRefreshConversationHistory();
  }

  async function sidebarAttentionUnavailable(): Promise<void> {
    await forceRefreshConversationHistory();
  }

  async function toggleConversationSearch(): Promise<void> {
    conversationSearchOpen = !conversationSearchOpen;
    if (conversationSearchOpen) {
      await tick();
      document.getElementById('conversation-sidebar-search')?.focus();
    } else {
      clearConversationSearch();
    }
  }

  function loadMoreVisibleConversations(): void {
    if (!visibleConversationsHaveMore || conversationListLoading || conversationTitleSearchLoading) return;
    if (conversationSearch.trim() && !conversationSearchSubmitted) {
      void loadConversationTitleSearch(conversationSearch, false);
    } else {
      void loadConversationPage(false);
    }
  }

  function automaticConversationPageKey(): string {
    const query = conversationSearch.trim();
    return JSON.stringify({
      mode: query && !conversationSearchSubmitted ? 'title' : 'list',
      query,
      cursor: query && !conversationSearchSubmitted
        ? conversationTitleSearchCursor
        : conversationCursor,
      channels: selectedChannels,
      agents: selectedAgentIds,
      status: selectedConversationStatus,
    });
  }

  function observeConversationListEnd(node: HTMLElement): { destroy: () => void } {
    if (typeof IntersectionObserver === 'undefined') return { destroy: () => {} };
    const root = node.closest<HTMLElement>('[data-pull-to-refresh-scroller]');
    const observer = new IntersectionObserver((entries) => {
      conversationListEndVisible = entries.some((entry) => entry.isIntersecting);
      if (!conversationListEndVisible) lastAutomaticConversationPageKey = null;
    }, { root, rootMargin: '160px 0px' });
    observer.observe(node);
    return {
      destroy: () => {
        conversationListEndVisible = false;
        observer.disconnect();
      },
    };
  }

  let visibleConversationsHaveMore = $derived(
    conversationSearchSubmitted
      ? false
      : conversationSearch.trim()
        ? conversationTitleSearchHasMore
        : conversationsHasMore,
  );

  $effect(() => {
    if (
      conversationListEndVisible
      && visibleConversationsHaveMore
      && !conversationListLoading
      && !conversationTitleSearchLoading
    ) {
      const pageKey = automaticConversationPageKey();
      if (lastAutomaticConversationPageKey === pageKey) return;
      lastAutomaticConversationPageKey = pageKey;
      loadMoreVisibleConversations();
    }
  });

  let visibleAgentDirectChats = $derived.by(() => {
    if (selectedProjectId || (selectedChannels.length > 0 && !selectedChannels.includes('web'))) return [];
    return sortAgentDirectChats(agentDirectChats.filter(
      item => selectedAgentIds.length === 0 || selectedAgentIds.includes(item.agent.agent_id),
    ));
  });

  $effect(() => {
    const query = conversationSearch.trim();
    const filterKey = sidebarProjectionCacheKey();
    if (!query || conversationSearchSubmitted) {
      conversationTitleSearchRequestId += 1;
      conversationTitleSearchResults = [];
      conversationTitleSearchCursor = null;
      conversationTitleSearchHasMore = false;
      conversationTitleSearchLoading = false;
      conversationTitleSearchError = '';
      return;
    }
    conversationTitleSearchRequestId += 1;
    conversationTitleSearchResults = [];
    conversationTitleSearchCursor = null;
    conversationTitleSearchHasMore = false;
    conversationTitleSearchError = '';
    conversationTitleSearchLoading = true;
    const timer = window.setTimeout(() => {
      void loadConversationTitleSearch(query, true, filterKey);
    }, 250);
    return () => {
      window.clearTimeout(timer);
    };
  });
  // Canonical Chat v2 visible items are the single source of truth for the
  // rendered timeline. Native selectors operate on the same canonical union;
  // there is no legacy render projection or structural cast.
  //
  // Window over explicitly renderable canonical items only. The policy is
  // exhaustive, so newly added kinds cannot silently create blank rows.
  let allRenderableVisibleItems = $derived.by<ChatV2TimelineItem[]>(() =>
    overlayManagedConversationStatus(
        chatV2Store.visibleItems.filter(isRenderableTimelineItem),
        backgroundWork.items,
      )
  );
  let pinnedTransientNotice = $derived(
    selectActivePinnedTransientNotice(chatV2Store.snapshot.runtime)
  );
  let renderableVisibleItems = $derived(
    allRenderableVisibleItems.filter((item) => !isPinnedTransientNotice(item))
  );
  let displayedTimeline = $derived.by<ChatV2TimelineItem[]>(() => {
    const end = timelineWindowEndIndex(renderableVisibleItems.length);
    const start = Math.min(Math.max(0, visibleStartIndex), end);
    return renderableVisibleItems.slice(start, end);
  });
  let displayedTimelineHasStreaming = $derived(displayedTimeline.some((item) => isInlineLiveChatV2Item(item)));
  const failedTurnRecovery = $derived.by(() =>
    currentConversation
      && !turnInProgress
      && !awaitingAssistantStart
      && !pendingDirectQuestion
      && !directQuestionSubmitting
      && !isReadOnly(currentConversation)
      ? selectFailedTurnRecovery(chatV2Store.visibleItems, chatV2Store.snapshot.runtime, {
          suppressedTurnId: suppressedRecoverableTurnId,
        })
      : null
  );
  const retryableFailedTurnId = $derived(failedTurnRecovery?.turnId ?? null);
  $effect(() => {
    if (lastRecoverableTurnId && (
      turnInProgress || awaitingAssistantStart
      || suppressedRecoverableTurnId === lastRecoverableTurnId
      || (chatV2Store.snapshot.runtime?.authority
        && chatV2Store.snapshot.runtime.authority.turn_id !== lastRecoverableTurnId)
    )) {
      error = '';
      lastRecoverableTurnId = null;
      lastRecoverableMessage = '';
    }
  });
  let chatSearchMatchedMessageIds = $derived.by(() => new Set(chatSearchResults.map((result) => result.targetId)));
  let selectedChatSearchTargetId = $derived(chatSearchResults[chatSearchSelectedIndex]?.targetId ?? null);

  // Sync composer draft with the active conversation. On conversation
  // switch, save the current draft under the previous key and hydrate
  // the new one. Runs before the composer-watching effect thanks to
  // declaration order.
  $effect(() => {
    const id = currentConversation?.conversation_id;
    const nextKey = id ? DRAFT_PREFIX + id : null;
    const generation = conversationLoadRequestId;
    if (nextKey === currentDraftKey && generation === composerHydratedGeneration) return;
    if (currentDraftKey) writeDraft(currentDraftKey, composer);
    if (draftSaveTimer !== null) window.clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
    composerHydratedGeneration = generation;
    composerLegacyRecovery = true;
    composerRecoveryScope = null;
    composerLegacyCandidates = [];
    composer = '';
    composerAttachments = [];
    composerInputMode = null;
    includedFeedback = null;
    composerInputRevision = crypto.randomUUID();
    composerRevision++;
    if (composerElement) composerElement.value = '';
    let hydrated;
    try { hydrated = id ? liveComposer?.hydrate(id) ?? hydrateComposer(composerDrafts, id) : { input: emptyComposerInput(), legacy: [] }; }
    catch (caught) {
      error = caught instanceof Error ? caught.message : 'Draft recovery failed';
      currentDraftKey = nextKey;
      return;
    }
    const loaded = hydrated.input.text;
    composerAttachments = hydrated.input.attachments;
    composerInputRevision = hydrated.input.input_revision;
    composerInputMode = hydrated.input.chat_mode;
    includedFeedback = hydrated.input.feedback ?? null;
    composerLegacyCandidates = hydrated.legacy;
    composerRecoveryScope = id ? { conversationId: id, generation,
      lease: documentCoordination().capture(), candidates: composerLegacyCandidates } : null;
    composerLegacyRecovery = hydrated.legacy.length > 0;
    if (composer !== loaded) {
      composer = loaded;
      // The textarea height is set imperatively from its content. Swap the
      // native value too (as send does) and re-measure after the flush, so a
      // tall unsent draft from the previous conversation does not leave the
      // composer expanded.
      if (composerElement) composerElement.value = loaded;
      void tick().then(syncComposerHeight);
    }
    currentDraftKey = nextKey;
  });

  $effect(() => {
    const key = currentDraftKey;
    // Failed or unresolved recovery must not subscribe through an obsolete owner.
    if (!key || !liveComposer || composerLegacyRecovery) return;
    return untrack(() => liveComposer.subscribe(key.slice(DRAFT_PREFIX.length), (input) => {
      if (composerInputRevision === input.input_revision && sameSubmissionInput(compositeComposerInput(), input)) return;
      composer = input.text; composerAttachments = input.attachments; composerInputMode = input.chat_mode;
      includedFeedback = input.feedback ?? null;
      composerInputRevision = input.input_revision; composerRevision++;
      composerLegacyRecovery = false; composerLegacyCandidates = [];
      void tick().then(syncComposerHeight);
    }));
  });

  // Comment inclusion changes only the feedback field; typed text stays as it is.
  $effect(() => {
    const key = currentDraftKey;
    if (!key) return;
    const id = key.slice(DRAFT_PREFIX.length);
    const apply = (event: Event) => {
      const detail = (event as CustomEvent<ComposerFeedbackDetail>).detail;
      if (detail.conversationId !== id || composerLegacyRecovery) return;
      event.preventDefault();
      if (detail.expected !== undefined && includedFeedback?.creation_key !== detail.expected) return;
      includedFeedback = detail.feedback;
      editComposerInput();
    };
    window.addEventListener(COMPOSER_FEEDBACK_EVENT, apply);
    return () => window.removeEventListener(COMPOSER_FEEDBACK_EVENT, apply);
  });

  // Publish complete input immediately so other views cannot autosave a stale
  // snapshot during a debounce window. The conversation owner serializes writes.
  $effect(() => {
    const key = currentDraftKey;
    const captured = compositeComposerInput();
    if (!key) return;
    if (typeof window === 'undefined') return;
    if (!composerLegacyRecovery) untrack(() => liveComposer?.update(key.slice(DRAFT_PREFIX.length), captured));
  });

  onMount(() => {
    browserVisible = document.visibilityState === 'visible' && hostAllowsObservation();
    const handleTaskControlEscape = (event: KeyboardEvent): void => {
      if (!isTaskControlMode || event.key !== 'Escape') return;
      event.preventDefault();
      requestTaskAgentDockMinimize();
    };
    window.addEventListener('keydown', handleTaskControlEscape);
    window.addEventListener('keydown', handleTaskControlFocusBoundary, true);
    void loadUserPreferences(auth.getSnapshot().user?.email);
    selectedProjectId = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedProject) ?? '';
    void loadProjects();
    restoreSelectedChannel();
    restoreChatSidebarState();
    conversationFiltersOpen = initialConversationFiltersOpen(window.innerWidth);
    mobileListOpen = !conversationIdFromRoute();
    unsubscribeWs = wsClient.subscribe(handleSocketEvent);
    visibilityHandler = () => {
      browserVisible = document.visibilityState === 'visible' && hostAllowsObservation();
      syncServiceWorkerActiveConversation();
      if (!document.hidden) {
        resumeControllerRecoveryIfPending();
        void refreshConversationView('visibility');
        scheduleTreeRefresh();
        if (currentConversation) void drainChatV2Outbox(currentConversation.conversation_id);
        void refreshPendingNotifications();
        // Give the sidebar refresh a moment, then catch up running turns.
        window.setTimeout(preloadActiveConversations, 1_500);
      } else {
        flushPendingDraft();
        saveCurrentConversationView();
      }
    };
    pageHideHandler = () => flushPendingDraft();
    focusHandler = () => {
      syncServiceWorkerActiveConversation();
      void refreshConversationView('focus');
      if (currentConversation) void drainChatV2Outbox(currentConversation.conversation_id);
    };
    pageShowHandler = (event: PageTransitionEvent) => {
      if (event.persisted) void resyncSidebarData('pageshow-bfcache', { force: true });
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
    window.addEventListener('pagehide', pageHideHandler);
    // Keep files still referenced by queued messages or by compact-chat
    // composer drafts (see chat-v2/unsent-recovery.ts).
    void chatV2Outbox.list()
      .then((entries) => attachmentUploads.prune([
        ...entries.map((entry) => entry.attachments ?? []),
         ...allDraftAttachmentLists(composerDrafts),
      ]))
      .catch(() => undefined);
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
        // periodic canonical sync is the only way a really-completed turn ever
        // settles in the UI. Keep this recovery scoped to Chat v2; a full
        // conversation refresh also rebuilds an unchanged Activity Overview.
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
        void recoverChatV2Canonical(currentConversation.conversation_id);
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
        conversationFiltersOpen = false;
        mobileListOpen = true;
      } else {
        scrollToBottom(true);
      }
    });

    return () => {
      window.removeEventListener('keydown', handleTaskControlEscape);
      window.removeEventListener('keydown', handleTaskControlFocusBoundary, true);
      saveCurrentConversationView();
      mobileListOverlayCleanup?.();
      mobileListOverlayCleanup = null;
      stopInitialLoadTimeout();
      unsubscribeWs?.();
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
      if (pageHideHandler) {
        window.removeEventListener('pagehide', pageHideHandler);
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
      chatV2CanonicalRecoveryCoalescer.clear();
      chatV2Lifecycle.release();
      if (activeConversationId) {
        wsClient.unsubscribeConversation(activeConversationId);
      }
      syncServiceWorkerActiveConversation(false);
    };
  });
</script>

{#snippet conversationMetadata(location: 'header' | 'sidebar')}
  {#if currentConversation}
    {@const agent = conversationAgentForDisplay(currentConversation)}
    {@const usage = activeContextUsage()}
    {@const modelBadge = runtimeModelPresentation(usage,
      latestRuntimeSelection && latestRuntimeSelection.sessionId === (focusedSessionId ?? currentConversation.active_session_id)
        ? latestRuntimeSelection.selection : null)}
    <div class="flex min-w-0 flex-wrap items-center gap-2 text-sm text-muted" data-testid="conversation-metadata">
      <div class="relative min-w-0 max-w-full">
        <button type="button" class="flex max-w-full items-center gap-1.5 rounded-lg px-1 py-0.5 hover:bg-surface-hover" onclick={() => { showAgentProfile = showAgentProfile === location ? null : location; }}>
          <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url ?? null} class="h-5 w-5" />
          <span class="truncate">{agent.display_name ?? agent.name}</span>
        </button>
        {#if showAgentProfile === location}
          <DeferredView load={() => import('$lib/components/AgentProfilePopover.svelte')}
            label="Loading agent profile…" onCancel={() => { showAgentProfile = null; }}>
            {#snippet children(loaded)}
              <loaded.default {agent} onClose={() => { showAgentProfile = null; }} />
            {/snippet}
          </DeferredView>
        {/if}
      </div>
      {#if currentConversation.active_session_id}
        <button class="flex items-center gap-1 font-mono text-xs text-quiet hover:text-secondary" onclick={copySessionId} type="button" title="Copy full session ID">
          {currentConversation.active_session_id.slice(0, 12)}
          {#if sessionIdCopied}<Check class="h-3 w-3 text-success" />{:else}<Copy class="h-3 w-3" />{/if}
        </button>
      {/if}
      {#if activeAgentProfileLabel()}
        <span class="max-w-full truncate rounded-full border border-blue-border bg-blue-subtle px-2 py-0.5 text-[10px] text-blue-tone" title="Agent profile">profile {activeAgentProfileLabel()}</span>
      {/if}
      {#if modelBadge.model}
        <span class="max-w-full truncate rounded-full border border-control-border bg-raised/60 px-2 py-0.5 text-[10px]" title={modelBadge.selected ? 'Selected for next message' : 'Last measured model'}>{modelBadge.model}</span>
        {#if modelBadge.effort}<span class="rounded-full border border-control-border px-2 py-0.5 text-[10px]" title="Thinking effort">thinking {modelBadge.effort}</span>{/if}
      {/if}
      {#if conversationTaskId(currentConversation)}
        <a href="/tasks/{conversationTaskId(currentConversation)}" class="rounded-full border border-control-border px-2 py-0.5 text-[10px] uppercase tracking-widest text-accent hover:text-accent-hover">{contextTypeBadge(currentConversation)}</a>
      {:else}
        <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] uppercase tracking-widest">{contextTypeBadge(currentConversation)}</span>
      {/if}
      <ConversationProjectControl
        conversation={currentConversation} {projects} busy={projectChangeBusy}
        onchangeProject={(projectId) => void changeConversationProject(projectId)}
        onchangeVisibility={(visibility) => void changeConversationVisibility(visibility)}
      />
      {#if backingSessionBadge}<span class="rounded-full border border-control-border px-2 py-0.5 text-[10px]" title="Backing sessions">{backingSessionBadge}</span>{/if}
    </div>
  {/if}
{/snippet}

<svelte:head>
  <title>{currentConversation ? `${conversationTitle(currentConversation)} · Chat · Cognis` : 'Chat · Cognis'}</title>
</svelte:head>

{#if timelineIdentityRequired}
  <div class="space-y-3 p-6" role="alert" data-testid="timeline-identity-required">
    <p>{timelineIdentityError}</p>
    <Button variant="secondary" disabled={recoveringTimelineOwner} onclick={recoverTimelineOwner}>Verify session and retry</Button>
  </div>
{:else if initializing && !conversationIdFromRoute()}
  <LoadingState label="Loading conversation" description="Fetching history, restoring workflow prompts, and preparing the live stream." />
{:else}
  <div
    bind:this={chatShellElement}
    class={`relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden lg:flex-row ${isWindowMode ? '' : `gap-3 ${chatSidebarCollapsed ? '' : 'lg:gap-4'}`}`}
    data-testid="chat-shell"
    use:edgeSwipe={{ edge: 'left', onTrigger: handleChatLeftEdgeSwipe }}
    use:edgeSwipe={{
      edge: 'right',
      onTrigger: handleChatRightEdgeSwipe,
      disabled: !mobileListOpen,
    }}
  >
    {#if mobileListOpen && !isWindowMode}
      <button
        aria-label="Close conversation list"
        data-edge-swipe-surface="true"
        class="fixed inset-0 z-30 bg-scrim/80 backdrop-blur-sm lg:hidden"
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
      bind:this={conversationSidebar}
      data-find-scope
      aria-label="Conversation list"
      aria-modal={mobileListOpen ? 'true' : undefined}
      class={`chat-history-sidebar fixed left-3 right-3 top-[calc(1rem+var(--app-safe-area-top))] bottom-3 z-40 flex max-w-[22rem] min-h-0 flex-col rounded-[1.75rem] border border-separator bg-chrome/95 shadow-card backdrop-blur transition-transform duration-200 ease-out lg:static lg:right-auto lg:max-w-none lg:translate-x-0 lg:rounded-none lg:border-0 lg:border-r lg:border-separator lg:bg-transparent lg:shadow-none lg:backdrop-blur-0 ${chatSidebarCollapsed ? 'lg:hidden' : 'lg:flex'} ${mobileListOpen || !currentConversation ? 'translate-x-0' : '-translate-x-[120%] pointer-events-none lg:pointer-events-auto'}`}
      style:--chat-sidebar-width={`${chatSidebarWidth}px`}
      inert={(!mobileListOpen && Boolean(currentConversation) && isMobileViewport()) || undefined}
      role={mobileListOpen ? 'dialog' : undefined}
    >
      <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions -->
      <div
        class="chat-sidebar-resizer touch-resize-handle touch-resize-handle--right absolute -right-1 top-0 z-30 hidden h-full w-2 cursor-col-resize touch-none focus-visible:bg-accent/40 lg:block"
        role="separator"
        tabindex="0"
        aria-label="Resize conversation history"
        aria-orientation="vertical"
        aria-valuemin={CHAT_SIDEBAR_MIN_WIDTH}
        aria-valuemax={CHAT_SIDEBAR_MAX_WIDTH}
        aria-valuenow={chatSidebarWidth}
        data-testid="chat-sidebar-resizer"
        onpointerdown={startChatSidebarResize}
        onkeydown={resizeChatSidebarWithKeyboard}
      ></div>
      <!-- Static top: filters -->
      <WaitingRefreshStatus
        error={waitingSnapshotScope === sidebarProjectionCacheKey() ? waitingLoadError : null}
        scope={sidebarProjectionCacheKey()}
        onRetry={() => void waitingReconciliation.request(
          sidebarProjectionCacheKey(), { manual: true, urgent: true },
        )}
      />
      <div class={`shrink-0 space-y-2 p-4 ${visibleAgentDirectChats.length > 0 ? 'pb-0 sm:pb-0' : 'pb-2 sm:pb-2'}`}>
        <div class="flex items-center justify-between gap-3 border-b border-separator pb-2" role="group" aria-label="Conversation controls">
          <div class="flex items-center gap-1">
            <Tooltip placement="bottom" text={conversationFiltersOpen ? 'Collapse filters' : 'Expand filters'} showOnTouch={false}>
              <button
                class={`grid h-9 w-9 place-items-center rounded-lg transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${conversationFiltersOpen ? 'bg-accent-subtle text-accent' : 'text-quiet hover:bg-surface-hover hover:text-accent'}`}
                type="button"
                aria-label={conversationFiltersOpen ? 'Collapse conversation filters' : 'Expand conversation filters'}
                aria-expanded={conversationFiltersOpen}
                onclick={() => (conversationFiltersOpen = !conversationFiltersOpen)}
              ><Filter class="h-4 w-4" /></button>
            </Tooltip>
            <Tooltip placement="bottom" text={conversationSearchOpen ? 'Collapse search' : 'Search conversations'} showOnTouch={false}>
              <button
                class={`grid h-9 w-9 place-items-center rounded-lg transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${conversationSearchOpen ? 'bg-accent-subtle text-accent' : 'text-quiet hover:bg-surface-hover hover:text-accent'}`}
                type="button"
                aria-label={conversationSearchOpen ? 'Collapse conversation search' : 'Expand conversation search'}
                aria-expanded={conversationSearchOpen}
                onclick={() => void toggleConversationSearch()}
              ><Search class="h-4 w-4" /></button>
            </Tooltip>
          </div>
          <div class="flex items-center gap-1">
            <Tooltip placement="bottom" text="Refresh conversations" showOnTouch={false}>
              <button
                class="grid h-9 w-9 place-items-center rounded-lg text-quiet transition hover:bg-surface-hover hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-50"
                disabled={conversationListRefreshing || conversationListLoading}
                onclick={() => void forceRefreshConversationHistory()}
                type="button"
                aria-label="Refresh conversation history"
              ><RefreshCw class={`h-4 w-4 ${conversationListRefreshing ? 'animate-spin' : ''}`} /></button>
            </Tooltip>
            <Tooltip placement="bottom" text="New conversation" showOnTouch={false}>
              <button
                 class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-accent bg-accent-subtle text-accent transition hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas disabled:cursor-not-allowed disabled:bg-raised disabled:text-quiet"
                disabled={newChatCreating}
                onclick={openNewConversationModal}
                type="button"
                aria-label="New conversation"
               ><Plus class="h-4 w-4" strokeWidth={2.5} /></button>
            </Tooltip>
            <button
              aria-label="Close conversation list"
              class="rounded-lg p-1.5 text-muted transition hover:bg-surface-hover hover:text-strong lg:hidden"
              type="button"
              onclick={closeMobileList}
            ><X class="h-4 w-4" /></button>
          </div>
        </div>

        {#if conversationSearchOpen}
          <form class="relative" onsubmit={(event) => { event.preventDefault(); void submitConversationSearch(); }}>
            <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-quiet" />
            <Input find={{ scopeOnly: !!currentConversation && !mobileListOpen }} id="conversation-sidebar-search" bind:value={conversationSearch} class="pl-9 pr-20" placeholder={searchEnabled ? 'Search conversations' : 'Filter by title'} />
            {#if conversationSearch}
              <button aria-label="Clear search" class="absolute right-10 top-1.5 rounded-lg p-1 text-quiet transition hover:bg-surface-hover hover:text-foreground" type="button" onclick={clearConversationSearch}>
                <X class="h-4 w-4" />
              </button>
            {/if}
            <button aria-label="Search conversations" class="absolute right-2 top-1.5 rounded-lg p-1 text-accent transition hover:bg-surface-hover hover:text-accent-hover disabled:text-quiet" type="submit" disabled={!searchEnabled || !conversationSearch.trim() || conversationSearchLoading}>
              <Search class={`h-4 w-4 ${conversationSearchLoading ? 'animate-pulse' : ''}`} />
            </button>
          </form>
          {#if !searchEnabled}
            <p class="text-xs text-quiet">Content search disabled; title search remains available.</p>
          {/if}
        {/if}

        {#if !sidebarProjectionLoaded && conversationListLoading}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Loading setup</p>
            <p class="text-sm leading-6 text-muted">Loading agents and conversations…</p>
          </div>
        {:else if sidebarProjectionFailed && agents.length === 0}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Setup unavailable</p>
            <p class="text-sm leading-6 text-muted">Could not load agents and setup state.</p>
            <Button class="w-full justify-center" size="sm" variant="secondary" onclick={() => void loadSidebarProjection({ manual: true })}>Retry</Button>
          </div>
        {:else if sidebarProjectionLoaded && agents.length === 0}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Setup incomplete</p>
            <p class="text-sm leading-6 text-muted">Create an agent to start chatting.</p>
            <Button class="w-full justify-center" size="sm" onclick={() => goto('/agents/new')}>Create agent</Button>
          </div>
        {:else if isLlmUnavailableForSetup()}
          <div class="space-y-3">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Setup incomplete</p>
            <p class="text-sm leading-6 text-muted">Configure an LLM provider first.</p>
            <Button class="w-full justify-center" size="sm" onclick={() => goto('/settings?tab=providers')}>Open provider settings</Button>
          </div>
        {:else}
          <div class={`${conversationFiltersOpen ? 'grid' : 'hidden'} grid-cols-1 gap-2 sm:grid-cols-2`}>
            <div class="relative">
              <label class="mb-1 block text-xs font-medium uppercase tracking-widest text-quiet" for="conversation-agent-filter">Agent</label>
              <button
                id="conversation-agent-filter"
                type="button"
                class="flex w-full items-center justify-between gap-2 rounded-lg border border-control-border bg-surface px-3 py-2 text-left text-xs font-medium text-foreground transition hover:border-border-strong"
                onclick={() => {
                  agentFilterDropdownOpen = !agentFilterDropdownOpen;
                  channelFilterDropdownOpen = false;
                }}
                aria-haspopup="listbox"
                aria-expanded={agentFilterDropdownOpen}
              >
                <span class="truncate">{selectedAgentFilterLabel()}</span>
                <ChevronDown class="h-3.5 w-3.5 shrink-0 text-quiet" />
              </button>
              {#if agentFilterDropdownOpen}
                <div class="absolute left-0 right-0 z-40 mt-1 max-h-64 overflow-y-auto rounded-xl border border-control-border bg-elevated p-1 shadow-xl shadow-black/40" role="listbox" aria-multiselectable="true">
                  <button
                    type="button"
                    class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selectedAgentIds.length === 0 ? 'bg-accent-subtle text-foreground' : 'text-secondary hover:bg-surface-hover hover:text-strong'}`}
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
                      class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selected ? 'bg-accent-subtle text-foreground' : 'text-secondary hover:bg-surface-hover hover:text-strong'}`}
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
              <label class="mb-1 block text-xs font-medium uppercase tracking-widest text-quiet" for="conversation-channel-filter">Channel</label>
              <button
                id="conversation-channel-filter"
                type="button"
                class="flex w-full items-center justify-between gap-2 rounded-lg border border-control-border bg-surface px-3 py-2 text-left text-xs font-medium text-foreground transition hover:border-border-strong"
                onclick={() => {
                  channelFilterDropdownOpen = !channelFilterDropdownOpen;
                  agentFilterDropdownOpen = false;
                }}
                aria-haspopup="listbox"
                aria-expanded={channelFilterDropdownOpen}
              >
                <span class="truncate">{selectedChannelFilterLabel()}</span>
                <ChevronDown class="h-3.5 w-3.5 shrink-0 text-quiet" />
              </button>
              {#if channelFilterDropdownOpen}
                <div class="absolute left-0 right-0 z-40 mt-1 max-h-64 overflow-y-auto rounded-xl border border-control-border bg-elevated p-1 shadow-xl shadow-black/40" role="listbox" aria-multiselectable="true">
                  <button
                    type="button"
                    class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selectedChannels.length === 0 ? 'bg-accent-subtle text-foreground' : 'text-secondary hover:bg-surface-hover hover:text-strong'}`}
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
                      class={`flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition ${selected ? 'bg-accent-subtle text-foreground' : 'text-secondary hover:bg-surface-hover hover:text-strong'}`}
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
          {#if conversationFiltersOpen && projects.length > 0}
            <div data-testid="conversation-project-filter">
              <label class="mb-1 block text-xs font-medium uppercase tracking-widest text-quiet" for="conversation-project-filter">Project</label>
              <select
                id="conversation-project-filter"
                class="w-full rounded-lg border border-control-border bg-surface px-3 py-2 text-xs font-medium text-foreground focus:border-accent"
                bind:value={selectedProjectId}
                onchange={() => void handleProjectFilterChange()}
              >
                <option value="">All projects</option>
                {#each projects.filter((project) => project.status === 'active') as project (project.project_id)}
                  <option value={project.project_id}>{project.name}</option>
                {/each}
              </select>
            </div>
          {/if}
          {#if conversationFiltersOpen}
            <label class="flex items-center gap-2 text-xs text-secondary">
              <input type="checkbox" bind:checked={includeSharedChats} onchange={() => void handleProjectFilterChange()} />
              Shared with me
            </label>
            <div class="grid grid-cols-4 gap-1.5 sm:gap-2">
              <Button size="sm" variant={selectedConversationStatus === 'active' ? 'primary' : 'secondary'} onclick={() => void setConversationStatusFilter('active')}>Active</Button>
              <Button size="sm" variant={selectedConversationStatus === 'starred' ? 'primary' : 'secondary'} onclick={() => void setConversationStatusFilter('starred')}>Starred</Button>
              <Button size="sm" variant={selectedConversationStatus === 'archived' ? 'primary' : 'secondary'} onclick={() => void setConversationStatusFilter('archived')}>Archived</Button>
              <Button size="sm" variant={selectedConversationStatus === 'task' ? 'primary' : 'secondary'} onclick={() => void setConversationStatusFilter('task')}>Task</Button>
            </div>
          {/if}
        {/if}

        {#if visibleAgentDirectChats.length > 0}
          <section class="border-b border-separator pb-0 pt-1" aria-labelledby="sidebar-agents-heading">
            <div class="mb-2 flex items-center justify-between px-1">
              <p id="sidebar-agents-heading" class="text-[10px] font-semibold uppercase tracking-[0.24em] text-muted">Agents</p>
            </div>
            <div class="flex gap-2 overflow-x-auto pb-1">
              {#each visibleAgentDirectChats as item}
                {@const conversation = item.conversation}
                {@const isActive = conversation.conversation_id === currentConversation?.conversation_id}
                   {@const inProgress = conversation.has_active_turn || (isActive && turnInProgress)}
                   {@const avatarState = conversationActivityState(conversation, {
                     open: isActive,
                     runtimeActive: inProgress,
                     backgroundWork: backgroundWork.items,
                   })}
                {@const showAttentionDot = conversationShowsAttentionDot(conversation, isActive, inProgress)}
                {@const attentionDescription = conversationAttentionDescription(conversation)}
                 <a
                  class={`group flex min-w-[4.5rem] flex-col items-center gap-1 rounded-2xl px-2 py-1 transition ${isActive ? 'bg-accent-subtle text-strong' : 'text-secondary hover:bg-surface-hover'}`}
                  href={conversationUrl(conversation.conversation_id)}
                  onclick={closeMobileList}
                  aria-current={isActive ? 'page' : undefined}
                  aria-label={`Open direct chat with ${agentLabel(item.agent)}${isActive ? ', current conversation' : ''}${showAttentionDot ? `, ${attentionDescription}` : ''}`}
                  title={`Open direct chat with ${agentLabel(item.agent)}`}
                >
                  <ActivityAvatar
                    name={agentLabel(item.agent)}
                    avatarUrl={item.agent.avatar_url ?? null}
                    state={avatarState}
                    class="h-10 w-10 rounded-2xl"
                  />
                  <span class="max-w-[4rem] truncate text-center text-[11px] font-medium">{agentLabel(item.agent)}</span>
                </a>
              {/each}
            </div>
          </section>
        {/if}

      </div>

      <!-- Scrollable middle: conversation list -->
      <PullToRefresh
        class="min-h-0 flex-1 px-4 pb-2 pt-2"
        disabled={!isMobileViewport()}
        onRefresh={forceRefreshConversationHistory}
      >
        <div class="space-y-1 pb-3">
          {#if (conversationListLoading || conversationListRefreshing) && visibleConversationList.length === 0 && !(conversationSearchSubmitted && searchEnabled)}
            <div class="rounded-2xl border border-separator bg-surface/40 px-4 py-6 text-center text-sm text-muted">
              <RefreshCw class="mx-auto mb-2 h-4 w-4 animate-spin text-quiet" />
              Loading conversation history...
            </div>
          {/if}
          {#if conversationSearchSubmitted && conversationSearchLoading && searchEnabled}
            <p class="mb-3 rounded-2xl border border-separator px-4 py-4 text-center text-sm text-muted">
              Searching conversation history...
            </p>
          {:else if conversationSearchSubmitted && conversationSearchError && searchEnabled}
            <p class="mb-3 rounded-2xl border border-danger-border bg-danger-subtle px-4 py-4 text-center text-sm text-danger">
              {conversationSearchError}
            </p>
          {:else if conversationSearchSubmitted && conversationSearchResults.length > 0}
            <div class="mb-3 space-y-1">
              <p class="px-2 text-[10px] font-semibold uppercase tracking-[0.24em] text-quiet">Search results</p>
              {#each conversationSearchResults as result}
                {@const agent = searchResultAgent(result)}
                {@const canExpand = result.match_count > 1 && result.extra_matches.length > 0}
                {@const expanded = searchResultExpanded(result.intaris_session_id)}
                {@const resultTime = searchMatchTime(result.top_match)}
                <div class="rounded-xl transition hover:bg-surface-hover">
                  <div class="group flex items-start gap-3 rounded-xl px-3 py-2.5 text-left text-foreground transition hover:bg-surface-hover">
                    <button
                      class="flex min-w-0 flex-1 items-start gap-3 text-left"
                      type="button"
                      onclick={() => void openConversationSearchResult(result)}
                    >
                      <AgentAvatar name={agent?.display_name ?? agent?.name ?? result.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-8 w-8 shrink-0" />
                      <span class="min-w-0 flex-1">
                        <span class="block break-words text-sm font-medium text-strong">{searchResultConversationTitle(result)}</span>
                        <span class="mt-0.5 flex items-center gap-2">
                          <span class="truncate text-xs text-muted">{agent?.display_name ?? agent?.name ?? result.agent_id}</span>
                          {#if result.match_count > 1}
                            <span class="shrink-0 text-[10px] font-medium text-quiet">{result.match_count} matches</span>
                          {/if}
                          {#if resultTime}
                            <span class="shrink-0 text-[10px] text-quiet">{resultTime}</span>
                          {/if}
                          <span class="shrink-0 text-[10px] text-quiet">{searchMatchScore(result.top_match)}</span>
                        </span>
                        <span class="mt-1 line-clamp-2 text-xs leading-5 text-muted">{searchResultSnippet(result)}</span>
                      </span>
                    </button>
                    {#if canExpand}
                      <button
                        aria-label={expanded ? 'Collapse matches' : 'Expand matches'}
                        class="mt-1 shrink-0 rounded-lg p-1 text-quiet transition hover:bg-surface-hover hover:text-foreground"
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
                    <div class="ml-11 mr-3 mb-2 space-y-1 border-l border-separator pl-2">
                      {#each result.extra_matches as match}
                        {@const matchTime = searchMatchTime(match)}
                        <button
                          class="block w-full rounded-lg px-2 py-1.5 text-left text-xs leading-5 text-muted transition hover:bg-surface-hover hover:text-foreground"
                          type="button"
                          onclick={() => void openConversationSearchResult(result, match)}
                        >
                          <span class="mb-0.5 block text-[10px] text-quiet">
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
            <p class="mb-3 rounded-2xl border border-dashed border-control-border px-4 py-4 text-center text-sm text-muted">
              No content matches found.
            </p>
          {/if}

          {#if conversationSearchSubmitted && searchEnabled}
            <!-- Content search replaces the normal list; empty state is handled above. -->
          {:else}
            {#if conversationTitleSearchError}
            <p class="rounded-2xl border border-danger-border bg-danger-subtle px-4 py-4 text-center text-sm text-danger">
              {conversationTitleSearchError}
            </p>
            {/if}
            {#if visibleConversationList.length === 0 && !(conversationListLoading || conversationListRefreshing || conversationTitleSearchLoading) && !conversationTitleSearchError}
            <p class="rounded-2xl border border-dashed border-control-border px-4 py-6 text-center text-sm text-muted">
              No conversations found.
            </p>
            {:else if visibleConversationList.length > 0}
            {@const sidebarSectionsEnabled = $userPreferences.display.conversation_sidebar_sections}
            {#if sidebarSectionsEnabled && visibleConversationLanes.active.length > 0}
              <section class="mb-3 flex min-h-0 flex-col" aria-labelledby="sidebar-active-heading" data-testid="sidebar-conversations-active">
                <div id="sidebar-active-heading" class="mb-1 flex shrink-0 items-center justify-between gap-2 px-2 text-[10px] font-semibold uppercase tracking-[0.24em] text-info">
                  <span>Active</span>
                  <span class="rounded-full bg-info-subtle px-1.5 py-0.5 text-[9px] tabular-nums tracking-normal" aria-label={`${visibleConversationLanes.active.length} active conversations`}>
                    {visibleConversationLanes.active.length}
                  </span>
                </div>
                <div class="space-y-1">
                  {#each visibleConversationLanes.active as conversation (conversation.conversation_id)}
                    {@const agent = conversationAgentForDisplay(conversation)}
                    {@const isActive = conversation.conversation_id === currentConversation?.conversation_id}
                    {@const inProgress = conversation.has_active_turn || (isActive && turnInProgress)}
                    <SidebarConversationRow
                      {selectedProjectId} onToggleProject={toggleSidebarProject}
                      {conversation}
                      href={conversationUrl(conversation.conversation_id)}
                      agentName={agent?.display_name ?? agent?.name ?? conversation.agent_id}
                      agentAvatarUrl={agent?.avatar_url ?? null}
                      avatarState={conversationActivityState(conversation, { open: isActive, runtimeActive: inProgress, backgroundWork: backgroundWork.items })}
                      active={isActive}
                      unread={conversation.has_unread && !isActive}
                      dense={$userPreferences.display.conversation_sidebar_dense}
                      canStar={canStarConversation(conversation)}
                      starBusy={starringConversationId === conversation.conversation_id}
                      todoProgress={conversationTodoProgressTodos(conversation)}
                      showTodoProgress={shouldShowConversationTodoProgress(conversation)}
                      contextBadge={(conversation.context?.type ?? 'web').toLowerCase() === 'web' ? null : contextTypeBadge(conversation)}
                      onNavigate={closeMobileList}
                      onToggleStar={(event) => { event.preventDefault(); event.stopPropagation(); void toggleConversationStar(conversation); }}
                      onOpenAttention={openSidebarAttention}
                    />
                  {/each}
                </div>
              </section>
            {/if}
            {#if sidebarSectionsEnabled && visibleConversationLanes.waiting.length > 0}
              <section class="mb-3 flex min-h-0 flex-col" aria-labelledby="sidebar-waiting-heading" data-testid="sidebar-conversations-waiting">
                <div id="sidebar-waiting-heading" class="mb-1 flex shrink-0 items-center justify-between gap-2 px-2 text-[10px] font-semibold uppercase tracking-[0.24em] text-warning">
                  <span>Waiting</span>
                  <span class="rounded-full bg-warning-subtle px-1.5 py-0.5 text-[9px] tabular-nums tracking-normal" aria-label={`${visibleConversationLanes.waiting.length} waiting conversations`}>
                    {visibleConversationLanes.waiting.length}
                  </span>
                </div>
                <div class="space-y-1">
                  {#each visibleConversationLanes.waiting as conversation (conversation.conversation_id)}
                    {@const agent = conversationAgentForDisplay(conversation)}
                    {@const isActive = conversation.conversation_id === currentConversation?.conversation_id}
                    {@const inProgress = conversation.has_active_turn || (isActive && turnInProgress)}
                    <SidebarConversationRow
                      {selectedProjectId} onToggleProject={toggleSidebarProject}
                      {conversation}
                      href={conversationUrl(conversation.conversation_id)}
                      agentName={agent?.display_name ?? agent?.name ?? conversation.agent_id}
                      agentAvatarUrl={agent?.avatar_url ?? null}
                      avatarState={conversationActivityState(conversation, { open: isActive, runtimeActive: inProgress, backgroundWork: backgroundWork.items })}
                      active={isActive}
                      unread={conversation.has_unread && !isActive}
                      dense={$userPreferences.display.conversation_sidebar_dense}
                      waitingReason={dashboardConversationWaitingReason(conversation)}
                      canStar={canStarConversation(conversation)}
                      starBusy={starringConversationId === conversation.conversation_id}
                      todoProgress={conversationTodoProgressTodos(conversation)}
                      showTodoProgress={shouldShowConversationTodoProgress(conversation)}
                      contextBadge={(conversation.context?.type ?? 'web').toLowerCase() === 'web' ? null : contextTypeBadge(conversation)}
                      onNavigate={closeMobileList}
                      onToggleStar={(event) => { event.preventDefault(); event.stopPropagation(); void toggleConversationStar(conversation); }}
                      onOpenAttention={openSidebarAttention}
                    />
                  {/each}
                </div>
              </section>
            {/if}
            {#if !sidebarSectionsEnabled || visibleHistorySections.length > 0}
            <section aria-labelledby={sidebarSectionsEnabled ? 'sidebar-history-heading' : undefined} data-testid="sidebar-conversations-history">
              {#if sidebarSectionsEnabled}
              <p id="sidebar-history-heading" class="mb-1 shrink-0 px-2 text-[10px] font-semibold uppercase tracking-[0.24em] text-muted">History</p>
              {/if}
              <div class="space-y-1">
            {#each (sidebarSectionsEnabled ? visibleHistorySections : visibleConversationSections) as section (section.key)}
              <section class="space-y-1" aria-labelledby={`history-section-${section.key}`}>
                <p id={`history-section-${section.key}`} class="sticky top-0 z-10 py-0.5 pr-2 text-right text-[10px] font-medium italic tracking-wide text-muted first:mt-0">{section.label}</p>
                {#each section.conversations as conversation (conversation.conversation_id)}
                  {@const agent = conversationAgentForDisplay(conversation)}
                  {@const isActive = conversation.conversation_id === currentConversation?.conversation_id}
                  {@const inProgress = conversation.has_active_turn || (isActive && turnInProgress)}
                  <SidebarConversationRow
                    {selectedProjectId} onToggleProject={toggleSidebarProject}
                    {conversation}
                    href={conversationUrl(conversation.conversation_id)}
                    agentName={agent?.display_name ?? agent?.name ?? conversation.agent_id}
                    agentAvatarUrl={agent?.avatar_url ?? null}
                    avatarState={conversationActivityState(conversation, { open: isActive, runtimeActive: inProgress, backgroundWork: backgroundWork.items })}
                    active={isActive}
                    unread={conversation.has_unread && !isActive}
                    dense={$userPreferences.display.conversation_sidebar_dense}
                    canStar={canStarConversation(conversation)}
                    starBusy={starringConversationId === conversation.conversation_id}
                    todoProgress={conversationTodoProgressTodos(conversation)}
                    showTodoProgress={shouldShowConversationTodoProgress(conversation)}
                    contextBadge={(conversation.context?.type ?? 'web').toLowerCase() === 'web' ? null : contextTypeBadge(conversation)}
                    onNavigate={closeMobileList}
                    onToggleStar={(event) => { event.preventDefault(); event.stopPropagation(); void toggleConversationStar(conversation); }}
                    onOpenAttention={openSidebarAttention}
                  />
                {/each}
              </section>
            {/each}
                {#if visibleConversationsHaveMore}
                  <div class="flex h-8 items-center justify-center" use:observeConversationListEnd role="status" aria-live="polite">
                    {#if conversationListLoading || conversationTitleSearchLoading}
                      <RefreshCw class="h-4 w-4 animate-spin text-quiet" />
                      <span class="sr-only">Loading more conversations</span>
                    {/if}
                  </div>
                {/if}
              </div>
            </section>
            {/if}
            {/if}
          {/if}

          {#if visibleConversationsHaveMore && $userPreferences.display.conversation_sidebar_sections && visibleHistorySections.length === 0}
            <div class="flex h-8 items-center justify-center" use:observeConversationListEnd role="status" aria-live="polite">
              {#if conversationListLoading || conversationTitleSearchLoading}
                <RefreshCw class="h-4 w-4 animate-spin text-quiet" />
                <span class="sr-only">Loading more conversations</span>
              {/if}
            </div>
          {/if}
        </div>
      </PullToRefresh>

    </aside>
    {#if selectedSidebarAttentionId}
      <AttentionActionDialog
        actionId={selectedSidebarAttentionId}
        title={selectedSidebarAttentionTitle}
        onClose={closeSidebarAttention}
        onSettled={sidebarAttentionSettled}
        onUnavailable={sidebarAttentionUnavailable}
      />
    {/if}
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
      class={`app-keyboard-avoiding-chat relative min-h-0 min-w-0 flex-1 bg-transparent ${inspectorPinned && !appContactBindingId(currentConversation) ? 'grid grid-cols-[minmax(32rem,1fr)_auto] grid-rows-[auto_auto_minmax(0,1fr)]' : 'flex flex-col'}`}
      data-testid="chat-main" data-find-container
      use:keyboardAvoidance
      ondragenter={handleDragEnter}
      ondragleave={handleDragLeave}
      ondragover={handleDragOver}
      ondrop={(event) => void handleDrop(event)}
    >
      {#if appContactBindingId(currentConversation)}
        {#key currentConversation?.conversation_id}
          <DeferredView load={() => import('$lib/components/AppContactChat.svelte')} label="Loading contact chat…">
          {#snippet children(loaded)}
          <loaded.default
            contactId={appContactBindingId(currentConversation) ?? ''}
            agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
            onOpenSidebar={() => { mobileListOpen = true; }}
          />
          {/snippet}
          </DeferredView>
        {/key}
      {:else}
      {#if switchingConversation}
        <div class="pointer-events-none absolute inset-0 z-20 bg-canvas/35 backdrop-blur-[1px]"></div>
      {/if}
      {#if showDropZone}
        <div class="pointer-events-none absolute inset-0 z-50 flex items-center justify-center rounded-3xl border-2 border-dashed border-accent bg-accent-subtle backdrop-blur-sm">
          <div class="rounded-2xl bg-elevated/90 px-6 py-4 text-center">
            <p class="text-lg font-medium text-accent">Drop files here to attach</p>
            <p class="mt-1 text-sm text-muted">Images, PDFs, audio, and other files</p>
          </div>
        </div>
      {/if}
      {#if !childView}
      <!-- Shared CSS assigns status clearance to this header in mobile standalone
           mode and to the route frame otherwise. -->
      <div
        class="app-keyboard-stable-header chat-header-shell relative z-20 border-b border-separator px-2.5 py-1 sm:px-3 sm:py-1.5 lg:px-4 lg:py-1.5"
        style="padding-left: max(0.625rem, env(safe-area-inset-left)); padding-right: max(0.625rem, env(safe-area-inset-right));"
        data-testid="chat-header" data-find-toolbar
      >
        <div class="flex min-w-0 items-center gap-1.5 sm:items-start sm:justify-between sm:gap-3">
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
                class="hidden rounded-lg p-1.5 text-muted transition hover:bg-surface-hover hover:text-strong lg:inline-flex"
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
                  <button type="button" class="flex h-11 w-10 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-surface-hover hover:text-foreground focus-visible:ring-2 focus-visible:ring-accent"
                    aria-label="Navigation" title="Navigation" onclick={requestOpenMobileNav}>
                    <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
                  </button>
                  <button type="button" aria-label="Open conversations" class="flex h-11 w-10 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-surface-hover hover:text-foreground" onclick={openMobileList}>
                    <ArrowLeft class="h-4 w-4" />
                  </button>
                {/if}
              </div>
              <!-- Editable title -->
              {#if editingTitle}
                <!-- svelte-ignore a11y_autofocus -->
                <div class="flex min-w-0 flex-1 items-center gap-1.5">
                  <input
                    class="min-w-0 flex-1 rounded-lg border border-accent bg-surface px-2 py-1 text-lg font-semibold text-strong focus:outline-none focus:ring-1 focus:ring-accent sm:text-xl"
                    bind:value={editTitleValue}
                    onblur={handleTitleBlur}
                    onkeydown={handleTitleKeydown}
                    autofocus
                  />
                  <button
                    class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-control-border text-muted transition hover:bg-surface-hover hover:text-accent disabled:cursor-not-allowed disabled:opacity-50"
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
                <div class="flex min-w-0 flex-1 flex-col justify-center">
                  <!-- The conversation title is the page heading. -->
                  <h1 class="flex min-w-0">
                  <button
                    class="min-w-0 truncate text-left text-[15px] font-semibold leading-5 text-strong transition hover:text-accent sm:text-lg sm:leading-6"
                    onclick={startEditTitle}
                    type="button"
                    title={currentConversation ? `${conversationTitle(currentConversation)} - click to edit title` : 'Click to edit title'}
                  >
                    {currentConversation ? conversationTitle(currentConversation) : 'Conversation'}
                  </button>
                  </h1>
                  {#if currentConversation}<ManagedChannelBadge conversation={currentConversation} />{/if}
                  {#if currentConversation}
                    <span
                      class="truncate text-[10px] font-medium uppercase leading-4 tracking-[0.12em] text-quiet sm:hidden"
                      role="status"
                      aria-live="polite"
                    >
                      {mobileConversationStatusLabel(
                        currentConversation.status,
                        turnInProgress,
                        pendingDirectQuestionVisible
                          || Boolean(pendingCredentialRequest)
                          || managedEscalationPending(escalations, selectedManagedChildConversationId),
                      )}
                    </span>
                  {/if}
                </div>
              {/if}
              {#if conversationYoloEnabled(currentConversation, currentConversation ? conversationAgent(currentConversation) ?? null : null)}
                <span
                  class="shrink-0 rounded-full border border-warning-border bg-warning-subtle px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide text-warning"
                  role="status"
                  title="Dangerous: Intaris still evaluates and audits calls, but policy may allow evaluated denials."
                  data-testid="chat-header-yolo-badge"
                >Yolo mode</span>
              {/if}
            </div>

            <!-- Sub-header info row -->
            <div class="chat-header-meta mt-1.5 hidden items-center justify-between gap-3 text-sm text-muted sm:flex">
              {@render conversationMetadata('header')}
              {#if activeContextUsage() && !isStandalonePwa}
                {@const usage = activeContextUsage()}
                <button
                  class={`relative ml-auto hidden h-8 w-8 shrink-0 items-center justify-center rounded-full border bg-surface/70 transition hover:border-accent hover:bg-surface-hover sm:inline-flex ${headerInfoOpen && headerInfoMode === 'context' ? 'border-accent' : 'border-control-border'}`}
                  title={contextUsageTooltip(usage)}
                  aria-label={headerInfoOpen && headerInfoMode === 'context' ? 'Close context usage details' : 'Open context usage details'}
                  aria-expanded={headerInfoOpen && headerInfoMode === 'context'}
                  aria-controls="conversation-info-drawer"
                  onclick={toggleContextInfo}
                  type="button"
                >
                  <svg class="absolute inset-1 -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
                    <circle cx="18" cy="18" r={CONTEXT_DONUT_RADIUS} fill="none" stroke="rgb(var(--color-muted) / 0.35)" stroke-width="3" />
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
            Keep mobile actions in the title row. The 44px targets stay
            touch-safe without creating a second info-only header band.
          -->
          <div data-find-actions class="flex shrink-0 items-center gap-0 sm:items-start sm:gap-2" data-testid="chat-header-controls"
            use:findTarget={{
              primary: true, enabled: !!currentConversation, identity: () => currentConversation?.conversation_id,
              open: async () => {
                if (!chatSearchOpen) {
                  chatSearchOpener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
                  findOnlySearchConversationId = currentConversation?.conversation_id ?? null;
                }
                chatSearchOpen = true; await tick(); return chatSearchInput;
              },
            }}>
            {#if !isTaskControlMode}<div class="hidden sm:block"><PinConversationButton conversation={currentConversation} /></div>{/if}
            {#if isTaskControlMode && !isStandalonePwa}
              <button
                class="touch-target-compact inline-flex items-center justify-center rounded-lg border border-control-border text-muted transition hover:bg-surface-hover hover:text-foreground"
                onclick={requestTaskAgentDockMinimize}
                type="button"
                title="Return to task"
                aria-label="Return to task"
              >
                <X class="h-4 w-4" />
              </button>
            {/if}
            {#if !isWindowMode && canOpenAuxiliaryWindow}
              <button
                class="touch-target-compact hidden items-center justify-center rounded-lg border border-control-border text-muted transition hover:bg-surface-hover hover:text-foreground sm:inline-flex"
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
              class="touch-target-compact hidden items-center justify-center rounded-lg border border-control-border transition hover:bg-surface-hover sm:inline-flex"
              class:text-accent={chatSearchOpen} class:text-muted={!chatSearchOpen} class:hover:text-foreground={!chatSearchOpen}
              onclick={() => { if (chatSearchOpen) closeChatSearch(); else openChatSearch(); }}
              type="button"
              title="Search conversation"
              aria-label="Search conversation"
              aria-pressed={chatSearchOpen}
              disabled={!currentConversation}
            >
              <Search class="h-4 w-4" />
            </button>
            <button
              class="touch-target-compact hidden items-center justify-center rounded-lg border border-control-border transition hover:bg-surface-hover sm:inline-flex"
              class:text-accent={$commentState.list} class:text-muted={!$commentState.list} class:hover:text-foreground={!$commentState.list}
              onclick={() => $commentState.list ? commentWorkspace.closeComments() : commentWorkspace.showComments()}
              type="button"
              title="Show comments"
              aria-label="Show comments"
              aria-pressed={$commentState.list}
              disabled={!currentConversation}
            >
              <MessagesSquare class="h-4 w-4" />
            </button>
            <ChatHeaderMenu conversation={isTaskControlMode ? undefined : currentConversation} items={[
              { label: 'Show comments', icon: 'comments', run: () => commentWorkspace.showComments(), disabled: !currentConversation },
              { label: 'Search conversation', icon: 'search', run: () => openChatSearch(), disabled: !currentConversation },
            ]} />
            <SessionDetailsButton compact open={headerInfoOpen} loading={sessionInfoLoading} ariaControls="conversation-info-drawer" testId="chat-header-info" onclick={toggleHeaderInfo} />
          </div>
        </div>
      </div>

      {#if chatSearchOpen}
        <div class="relative z-20 col-start-1 row-start-2 h-0 min-w-0 shrink-0">
          <ChatSearchBar
            bind:inputElement={chatSearchInput}
            bind:query={chatSearchQuery}
            results={chatSearchResults}
            selectedIndex={chatSearchSelectedIndex}
            loading={chatSearchLoading}
            disabled={!currentConversation}
            onSubmit={runChatSearch}
            onClose={closeChatSearch}
            onNext={() => selectChatSearchResult(chatSearchSelectedIndex + 1)}
            onPrevious={() => selectChatSearchResult(chatSearchSelectedIndex - 1)}
          />
        </div>
      {/if}
      {/if}

      {#if currentConversation && ($commentState.active || $commentState.list)}
        <DeferredView load={() => import('$lib/components/feedback/FeedbackConversationWorkspace.svelte')} label="Loading comments…">
          {#snippet children(loaded)}<loaded.default workspace={commentWorkspace} conversationId={currentConversation!.conversation_id} viewport={timelineEl} />{/snippet}
        </DeferredView>
      {/if}

      {#if currentConversation}
        {@const panelAgent = conversationAgentForDisplay(currentConversation)}
        {@const panelPerformance = activeLastGeneration()}
        {@const panelResponsiveness = responsivenessBadgeOrNull(panelPerformance)}
        <ConversationInfoDrawer
          open={headerInfoOpen}
          presentation={inspectorPresentation}
          width={effectiveInspectorWidth}
          onWidthChange={(width) => conversationInfoDrawer.setWidth(width, false)}
          onWidthCommit={(width) => conversationInfoDrawer.setWidth(width)}
           onFocusChange={setInspectorFocus}
           onClose={() => closeHeaderInfo(true)}
         >
           {#snippet header()}
               <div class="min-w-0 flex-1">
                 <h2 id="conversation-info-heading" class="sr-only">Conversation inspector</h2>
                  <SharedInspectorTabs
                    scope={inspectorScope ?? conversationTimelineScope(currentConversation!.conversation_id)}
                    sessionId={focusedSessionId ?? currentConversation!.active_session_id ?? ''}
                    activeTab={headerInfoMode === 'context' ? 'overview' : headerInfoMode}
                    headerOnly
                    idPrefix="conversation-info"
                    testIdPrefix="conversation-info"
                    onChange={(id) => {
                      if (id !== 'overview') cancelActivityOverviewDemand();
                      conversationInfoDrawer.mode = id;
                      if (id === 'session' && !sessionInfo) void loadSessionInfo();
                      if (id === 'overview') loadVisibleActivityOverview();
                    }}
                  />
               </div>
           {/snippet}
           <div
            id={`conversation-info-panel-${headerInfoMode}`}
            role={headerInfoMode === 'context' ? undefined : 'tabpanel'}
            aria-labelledby={headerInfoMode === 'context' ? undefined : `conversation-info-tab-${headerInfoMode}`}
          >
             {#if childView && childWorkstream}
             <section class="mb-4 min-w-0 border-b border-separator pb-4" aria-label="Selected session details" data-testid="child-sidebar-metadata">
               <h3 class="mb-2 text-xs font-semibold uppercase tracking-widest text-quiet">Selected session</h3>
               <p class="truncate text-sm text-foreground">{childWorkstream.title}</p>
               <p class="text-xs text-secondary">{childWorkstream.agent_display_name ?? subSessionAgent()?.display_name ?? subSessionAgent()?.name ?? childWorkstream.agent_id}</p>
             </section>
             {:else}
             <section class="mb-4 min-w-0 border-b border-separator pb-4" aria-label="Conversation details" data-testid="conversation-sidebar-metadata">
             <h3 class="mb-2 text-xs font-semibold uppercase tracking-widest text-quiet">Conversation</h3>
             {@render conversationMetadata('sidebar')}
             </section>
             {/if}
           {#if focusedSessionId}
              <button type="button" class="mb-3 inline-flex items-center gap-1 text-xs text-accent hover:text-accent-hover" onclick={navigateToStructuralParent}>
               <ArrowLeft class="h-3.5 w-3.5" /> Back
             </button>
            {/if}
            {#if headerInfoMode === 'overview' || headerInfoMode === 'context'}
               {#if overviewReadPresentation.loading}
                <p class="text-xs text-quiet">
                  {activityOverviewReadState.root.pending
                    ? 'Loading execution tree…'
                    : 'Loading focused activity…'}
                </p>
               {:else if renderedActivityOverview && overviewReadPresentation.ready}
                  <InspectorOverview
                  overview={renderedActivityOverview}
                  {agents}
                  focusedSession={focusedWorkstream}
                  focusedSessionId={treeFocusedSessionId}
                  focusedSessionRuntimeActive={Boolean(
                    canonicalFocusedSessionId
                    && runtimeActiveChildSessionIds.has(canonicalFocusedSessionId)
                  )}
                   {runtimeActiveSessionIds}
                   narrow={inspectorPresentation === 'overlay'}
                   contextUsage={focusedDiagnostics.contextUsage}
                   diagnosticsFreshness={focusedDiagnostics.freshness}
                  onOpenWork={(category, sessionId, focus) => openInspectorWork(category, sessionId, focus)}
                  onRefresh={() => loadVisibleActivityOverview(true)}
                 onViewSession={handleViewSession}
                 />
              {:else if overviewReadPresentation.error}
                <div class="space-y-2">
                  <p class="text-xs text-danger">{overviewReadPresentation.error}</p>
                  <button
                    type="button"
                    class="text-xs text-accent hover:text-accent-hover"
                    onclick={() => void loadVisibleActivityOverview(true)}
                  >Retry</button>
                </div>
              {:else}
                <p class="text-xs text-quiet">Loading activity overview…</p>
              {/if}
            {:else if headerInfoMode === 'usage'}
              <ConversationUsagePanel
                scope={inspectorScope ?? conversationTimelineScope(currentConversation.conversation_id)}
                visible={headerInfoOpen}
              />
            {:else if headerInfoMode === 'work'}
                  <LazyWorkView
                  scope={inspectorScope ?? conversationTimelineScope(currentConversation.conversation_id)}
                   initialTab={focusedWorkCategory === 'deliverables' ? 'results' : focusedWorkCategory}
                    forceInitialTab
                    initialFocus={focusedWorkInitialFocus}
                    onSourceFocus={requestWorkSourceFocus}
                   onViewSession={handleViewSession}
                />
           {:else}
          {#if !sessionInfo && latestRuntimeSelection && latestRuntimeSelection.sessionId === currentConversation.active_session_id}
            <section class="text-xs text-secondary" data-testid="pending-session-selected-runtime">
              <p>Selected for next message</p>
              <p>Profile: {latestRuntimeSelection.selection.profile_id}</p>
               <p>Model: {latestRuntimeSelection.selection.provider_id ? `${latestRuntimeSelection.selection.provider_id}/` : ''}{latestRuntimeSelection.selection.model ?? 'Provider default'}</p>
              <p>Thinking: {latestRuntimeSelection.selection.reasoning_effort ?? 'Default'}</p>
            </section>
          {/if}
          {#if sessionInfoLoading && !sessionInfo}
            <p class="text-xs text-quiet">Loading session details…</p>
          {:else if sessionInfo}
            {@const loadedSessionInfo = sessionInfo!}
            {@const panelContextUsage = mergeContextUsage(activeContextUsage(), loadedSessionInfo.context_usage ?? null)!}
            {@const narrativeText = sessionNarrativeText(sessionInfo)}
            {#if true}
              {#snippet timelineDiagnosticsFooter()}
                <TimelineDiagnosticExport clientState={chatV2Store.snapshot} visibleItems={chatV2Store.visibleItems} />
              {/snippet}
              <SessionDetailsContent
                detail={sessionInfo}
                sessionId={focusedSessionId ?? currentConversation.active_session_id ?? sessionInfo.intaris_session_id}
                yoloMode={sessionYoloEnabled(
                  currentConversation,
                  conversationAgent(currentConversation) ?? null,
                  focusedSessionId,
                  sessionInfo.maximum_outcome
                )}
                contextUsage={panelContextUsage}
                performance={activeLastGeneration()}
                onOpenIntaris={openIntarisSession}
                canStar={!isTaskControlMode && canStarConversation(currentConversation)}
                starred={Boolean(currentConversation.starred_at)}
                starBusy={starringConversationId === currentConversation.conversation_id}
                onToggleStar={() => { if (currentConversation) void toggleConversationStar(currentConversation); }}
                canArchive={canManageInspectorConversationLifecycle}
                canDelete={canManageInspectorConversationLifecycle}
                archived={currentConversation.status === 'archived'}
                archiveBusy={archivingConversation}
                deleteBusy={deletingConversation}
                onArchive={canManageInspectorConversationLifecycle ? archiveConversation : undefined}
                onRestore={canManageInspectorConversationLifecycle ? restoreConversation : undefined}
                onDelete={canManageInspectorConversationLifecycle ? deleteConversation : undefined}
                runtimeDetailsFooter={!childView ? timelineDiagnosticsFooter : undefined}
             />
            {:else}
            {#if narrativeText}
              <div class="mb-3">
                <p class="text-[10px] font-semibold uppercase tracking-widest text-quiet">{sessionNarrativeLabel(sessionInfo)}</p>
                <p class={`mt-1 whitespace-pre-wrap text-sm leading-6 text-foreground ${sessionNarrativeExpanded ? '' : 'line-clamp-3 sm:line-clamp-none'}`}>{narrativeText}</p>
                <button
                  class="mt-1 inline text-xs font-medium text-accent underline-offset-4 hover:text-accent-hover hover:underline sm:hidden"
                  type="button"
                  onclick={() => { sessionNarrativeExpanded = !sessionNarrativeExpanded; }}
                >
                  {sessionNarrativeExpanded ? 'Show less' : 'Show more'}
                </button>
              </div>
            {/if}
            {#if headerInfoMode !== 'context'}
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
                <span>Status: <span class="text-foreground">{loadedSessionInfo.status}</span></span>
                <span>Calls: <span class="text-foreground">{loadedSessionInfo.total_calls}</span></span>
                <span class="text-success">{loadedSessionInfo.approved_count} approved</span>
                <span class="text-danger">{loadedSessionInfo.denied_count} denied</span>
                <span class="text-info">{loadedSessionInfo.escalated_count} escalated</span>
              </div>
              <Button size="sm" variant="secondary" onclick={() => void openIntarisSession(sessionInfo?.intaris_session_id ?? '')}>Open in Intaris</Button>
            </div>
            {/if}
            {#if headerInfoMode !== 'context'}
              <div class="mt-4 grid gap-3 lg:grid-cols-2">
                <section class="rounded-2xl border border-separator bg-inset/50 p-3">
                  <p class="text-[10px] font-semibold uppercase tracking-widest text-quiet">Runtime</p>
                  <dl class="mt-2 grid gap-2 text-xs sm:grid-cols-2">
                    <div>
                      <dt class="text-quiet">Model</dt>
                      <dd class="mt-0.5 break-all text-foreground">{panelPerformance?.model ?? panelContextUsage?.model ?? 'Unknown'}</dd>
                    </div>
                    <div>
                      <dt class="text-quiet">Provider</dt>
                      <dd class="mt-0.5 text-foreground">{panelPerformance?.provider_name ?? panelPerformance?.provider_id ?? panelContextUsage?.provider_id ?? 'Cloud or default'}</dd>
                    </div>
                    {#if panelPerformance?.runtime}
                      <div>
                        <dt class="text-quiet">Runtime</dt>
                        <dd class="mt-0.5 text-foreground">{panelPerformance?.runtime}</dd>
                      </div>
                    {/if}
                    {#if panelPerformance?.executor_name || panelPerformance?.executor_id}
                      <div>
                        <dt class="text-quiet">Executor</dt>
                        <dd class="mt-0.5 text-foreground">{panelPerformance?.executor_name ?? panelPerformance?.executor_id}</dd>
                      </div>
                    {/if}
                    {#if panelPerformance?.configured_context_tokens}
                      <div>
                        <dt class="text-quiet">Configured context</dt>
                        <dd class="mt-0.5 text-foreground">{formatTokenCount(panelPerformance?.configured_context_tokens)} tokens</dd>
                      </div>
                    {/if}
                    {#if panelPerformance?.quantization}
                      <div>
                        <dt class="text-quiet">Quantization</dt>
                        <dd class="mt-0.5 text-foreground">{panelPerformance?.quantization}</dd>
                      </div>
                    {/if}
                  </dl>
                </section>
                <section class="rounded-2xl border border-separator bg-inset/50 p-3">
                  <div class="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p class="text-[10px] font-semibold uppercase tracking-widest text-quiet">Last generation</p>
                      <p class="mt-1 text-xs text-quiet">
                        {performanceMeasurementLabel(panelPerformance)}
                      </p>
                    </div>
                    {#if panelResponsiveness}
                      <span
                        class={`rounded-full border px-2 py-1 text-[10px] font-semibold ${panelResponsiveness?.tone === 'good' ? 'border-success-border bg-success-subtle text-success' : panelResponsiveness?.tone === 'slow' ? 'border-warning-border bg-warning-subtle text-warning' : 'border-info-border bg-info-subtle text-info'}`}
                        title={panelResponsiveness?.detail}
                      >
                        {panelResponsiveness?.label}
                      </span>
                    {/if}
                  </div>
                  {#if panelPerformance}
                    <div class="mt-3 grid gap-2 sm:grid-cols-2">
                      {#each optionalPerformanceMetrics(panelPerformance) as metric}
                        <div class="rounded-xl border border-separator bg-inset/40 px-2.5 py-2">
                          <p class="text-[10px] uppercase tracking-wide text-quiet">{metric.label} · {metric.raw}</p>
                          <p class="mt-1 text-xs font-medium text-foreground">{metric.value}</p>
                        </div>
                      {/each}
                    </div>
                    {#if panelResponsiveness}
                      <p class="mt-2 text-[11px] text-quiet">{panelResponsiveness?.label} for {panelResponsiveness?.detail}.</p>
                    {/if}
                    {#if panelPerformance?.processor || panelPerformance?.gpu_residency || panelPerformance?.digest}
                      <p class="mt-2 break-all text-[11px] text-quiet">
                        {performanceHardwareLabel(panelPerformance)}
                      </p>
                    {/if}
                  {/if}
                </section>
              </div>
            {/if}
            {#if panelContextUsage}
              {@const policy = panelContextUsage.projection_policy}
              <div class="mt-4 rounded-2xl border border-separator bg-inset/50 p-3">
                <div class="mb-3 flex items-center gap-3">
                  <svg class="h-12 w-12 shrink-0 -rotate-90" viewBox="0 0 36 36" aria-hidden="true">
                    <circle cx="18" cy="18" r={CONTEXT_DONUT_RADIUS} fill="none" stroke="rgb(var(--color-muted) / 0.35)" stroke-width="3" />
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
                    <p class="text-[10px] font-semibold uppercase tracking-widest text-quiet">Context window</p>
                    <p class="mt-1 text-sm font-medium text-strong">
                      {formatTokenCount(panelContextUsage.prompt_tokens)} / {formatTokenCount(panelContextUsage.max_context_tokens)}
                      <span class={contextUsageColor(panelContextUsage)}>({formatPercent(contextWindowUsagePercentage(panelContextUsage))})</span>
                    </p>
                    <p class="mt-0.5 text-xs text-quiet">
                      {panelContextUsage.model}
                      {panelContextUsage.reasoning_effort ? ` · thinking ${panelContextUsage.reasoning_effort}` : ''}
                      {contextUsageProfileLabel(panelContextUsage) ? ` · profile ${contextUsageProfileLabel(panelContextUsage)}` : ''}
                    </p>
                  </div>
                </div>
                <div class="grid gap-2 text-xs text-muted xl:grid-cols-[minmax(0,1fr)_minmax(16rem,0.45fr)]">
                  <div class="space-y-2">
                    {#each contextBudgetBars(panelContextUsage) as metric}
                      <div class="rounded-xl border border-separator bg-inset/40 px-2.5 py-2">
                        <div class="mb-1.5 flex items-center justify-between gap-3">
                          <span class="inline-flex items-center gap-1.5">
                            {metric.label}
                            <Popover text={metric.description} placement="top">
                              <button
                                class="inline-flex h-4 w-4 items-center justify-center rounded-full border border-control-border text-[10px] font-semibold text-quiet transition hover:border-accent hover:text-accent"
                                type="button"
                                aria-label={`${metric.label} help`}
                              >
                                ?
                              </button>
                            </Popover>
                          </span>
                          <span class="text-foreground">{metric.value}</span>
                        </div>
                        <div class="h-1.5 overflow-hidden rounded-full bg-raised">
                          <div class={`h-full rounded-full ${metric.color}`} style={`width: ${metric.percent}%`}></div>
                        </div>
                      </div>
                    {/each}
                  </div>
                  <div class="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
                    {#each contextBudgetChips(panelContextUsage) as metric}
                      <div class="rounded-xl border border-separator bg-inset/40 px-2.5 py-2">
                        <div class="flex items-baseline justify-between gap-2">
                          <span class="inline-flex items-center gap-1.5">
                            {metric.label}
                            <Popover text={metric.description} placement="top">
                              <button
                                class="inline-flex h-4 w-4 items-center justify-center rounded-full border border-control-border text-[10px] font-semibold text-quiet transition hover:border-accent hover:text-accent"
                                type="button"
                                aria-label={`${metric.label} help`}
                              >
                                ?
                              </button>
                            </Popover>
                          </span>
                          <span class="text-foreground">{metric.value}</span>
                        </div>
                      </div>
                    {/each}
                  </div>
                </div>
              </div>
            {/if}
            {/if}
          {:else}
            <p class="text-xs text-quiet">Unable to load session details.</p>
          {/if}
          {/if}
          </div>
        </ConversationInfoDrawer>
      {/if}

      <div
        class:contents={!childView}
        class:hidden={Boolean(childView)}
        inert={childView ? true : undefined}
        aria-hidden={childView ? 'true' : undefined}
        data-testid="parent-chat-surface"
      >
      <!-- Message area + composer -->
      <div class="col-start-1 row-start-3 flex min-h-0 min-w-0 flex-1 flex-col gap-2 overflow-x-hidden px-0 pt-2 pb-0 sm:gap-4 sm:px-4 sm:py-4">
        <ProviderUnavailableNotices />

        {#if showPushPrompt}
          <div class="rounded-2xl border border-info-border bg-info-subtle px-4 py-3 text-sm text-foreground">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="min-w-0 flex-1">
                <p class="font-medium">Native notifications</p>
                <p class="mt-1 text-secondary">{pushPromptText()}</p>
                {#if pushPromptError}
                  <p class="mt-2 text-xs text-danger">{pushPromptError}</p>
                {/if}
                {#if pushDeliveryError}
                  <p class="mt-2 break-words font-mono text-xs text-warning">Last delivery error: {pushDeliveryError}</p>
                {/if}
                <button class="mt-2 text-xs font-medium text-accent hover:text-accent-hover underline-offset-4 hover:underline" type="button" onclick={() => goto('/settings?tab=notifications')}>
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
                  class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-foreground transition hover:bg-surface-hover"
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

        {#if retryWaitingTurn}
          <div class="rounded-2xl border border-warning-border bg-warning-subtle px-3 py-3 text-sm text-warning">
            <p>
              Waiting to retry at {new Date(retryWaitingTurn.retry_at ?? '').toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              because {retryWaitingTurn.retry_reason === 'rate_limit' ? 'the provider rate-limited this turn' : 'the provider requested a retry'}.
              {#if queuedCount > 0}
                {queuedCount} later message{queuedCount === 1 ? ' is' : 's are'} queued behind it.
              {/if}
            </p>
            {#if retryWaitingTurn.retry_reason === 'rate_limit' && currentConversation && !isReadOnly(currentConversation)}
              {@const retryConversationId = currentConversation.conversation_id}
              <RateLimitProviderSwitch
                conversationId={retryConversationId}
                turnId={retryWaitingTurn.turn_id}
                providerId={retryWaitingTurn.provider_id ?? null}
                model={retryWaitingTurn.model ?? null}
                retryAt={retryWaitingTurn.retry_at ?? null}
                onResponse={async (response) => {
                  if (currentConversation?.conversation_id === retryConversationId) await applyCommandResponse(response);
                }}
              />
            {/if}
          </div>
        {/if}

        {#if queuedCount > 0}
          <div class="rounded-2xl border border-info-border bg-info-subtle px-3 py-2 text-sm text-foreground">
            {#if queuedMessages.length > 0}
              <div class="space-y-1.5">
                {#each queuedMessages as queued (queued.queue_id)}
                  {@const automaticContinuation = isAutomaticContinuation(queued)}
                  {@const committing = queued.status === 'committing'}
                  {@const canMutateQueue = !automaticContinuation && !committing && canMutateQueuedMessages()}
                  {@const isExpanded = queueIsExpanded(queued.queue_id)}
                  {@const accessibleLabel = queuedMessageAccessibleLabel(queued)}
                  <div class="chat-queue-item-enter rounded-xl border border-info-border bg-surface px-2.5 py-2">
                    <div class="flex min-w-0 flex-wrap items-center gap-2 sm:flex-nowrap">
                      {#if automaticContinuation || committing}
                        <span class="shrink-0 text-[11px] uppercase tracking-wide text-info">{automaticContinuation ? 'automatic' : 'committing'}</span>
                      {/if}
                      <p class="min-w-0 flex-1 truncate text-foreground">{queuedMessageLabel(queued)}</p>
                      {#if queued.attachments?.length}
                        <span class="shrink-0 rounded-full border border-control-border px-2 py-0.5 text-[11px] text-secondary">{queued.attachments.length} attachment{queued.attachments.length === 1 ? '' : 's'}</span>
                      {/if}
                      <div class="ml-auto flex shrink-0 items-center gap-1.5">
                        <Button size="sm" variant="ghost" disabled={queueBusyId === queued.queue_id} aria-expanded={isExpanded} aria-label={`${isExpanded ? 'Collapse' : 'Expand'} queued message: ${accessibleLabel}`} onclick={() => toggleQueuedMessage(queued.queue_id)}>{isExpanded ? 'Collapse' : 'Details'}</Button>
                        {#if canMutateQueue}
                          {#if queued.can_edit}
                          <Button size="sm" variant="secondary" disabled={queueBusyId === queued.queue_id} aria-label={`Edit queued message: ${accessibleLabel}`} onclick={() => startQueuedMessageEdit(queued)}>Edit</Button>
                          {/if}
                          <Button size="sm" variant="danger" disabled={queueBusyId === queued.queue_id} aria-label={`Delete queued message: ${accessibleLabel}`} onclick={() => void deleteQueuedMessage(queued.queue_id)}>Delete</Button>
                        {/if}
                      </div>
                    </div>
                      {#if queueEditingId === queued.queue_id && !committing && queued.can_edit}
                      <div class="mt-2 rounded-lg border border-separator bg-inset p-2.5">
                        <textarea
                          class="min-h-24 w-full resize-y rounded-lg border border-control-border bg-surface px-3 py-2 text-sm text-foreground outline-none transition placeholder:text-quiet focus:border-accent"
                          bind:value={queueEditContent}
                          disabled={queueBusyId === queued.queue_id}
                          aria-label={`Edit queued message: ${accessibleLabel}`}
                        ></textarea>
                        {#if queueEditError}
                          <p class="mt-2 text-xs text-danger">{queueEditError}</p>
                        {/if}
                        {#if queued.attachments?.length}
                          <p class="mt-2 text-xs text-secondary">{queued.attachments.length} attachment{queued.attachments.length === 1 ? '' : 's'} attached. To change attachments, delete this queued message and recreate it.</p>
                        {/if}
                        <div class="mt-2 flex justify-end gap-2">
                          <Button size="sm" variant="ghost" disabled={queueBusyId === queued.queue_id} onclick={cancelQueuedMessageEdit}>Cancel</Button>
                          <Button size="sm" variant="primary" disabled={queueBusyId === queued.queue_id || !queueEditContent.trim()} onclick={() => void saveQueuedMessageEdit(queued.queue_id)}>
                            {queueBusyId === queued.queue_id ? 'Saving…' : 'Save'}
                          </Button>
                        </div>
                      </div>
                    {:else if isExpanded}
                      <div class="mt-2 rounded-lg border border-separator bg-inset p-2.5">
                        <p class="max-h-[32vh] overflow-auto whitespace-pre-wrap break-words text-foreground">{queued.content || queuedMessageLabel(queued)}</p>
                        {#if queued.attachments?.length}
                          <p class="mt-2 text-xs text-secondary">{queued.attachments.length} attachment{queued.attachments.length === 1 ? '' : 's'} attached. To change attachments, delete this queued message and recreate it.</p>
                        {/if}
                      </div>
                    {/if}
                  </div>
                {/each}
              </div>
            {:else}
              <p class="mt-1 text-secondary">{queuedCount} queued message{queuedCount === 1 ? '' : 's'} waiting. Details are syncing…</p>
            {/if}
          </div>
        {/if}

        {#if error && !failedTurnRecovery && !lastRecoverableTurnId}
          <div class="rounded-2xl border border-danger-border bg-danger-subtle px-3 py-3 text-sm text-danger">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p class="min-w-0 flex-1 break-words">{error}</p>
              <div class="flex shrink-0 items-center gap-2">
                {#if lastRecoverableTurnId}
                  <Button size="sm" variant="secondary" onclick={retryLastTurn}>Retry</Button>
                {/if}
                <button
                  aria-label="Dismiss error"
                  class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-danger transition hover:bg-surface-hover"
                  onclick={() => { error = ''; lastRecoverableMessage = ''; lastRecoverableTurnId = null; }}
                  type="button"
                >
                  <X class="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
        {/if}

        {#if failedTurnRecovery}
          <RecoveryBanner message={failedTurnRecovery.message} actionLabel="Retry failed turn" onRetry={continueFailedTurn}>
            {#if failedTurnRecovery.providerId && failedTurnRecovery.model && currentConversation}
              {@const retryConversationId = currentConversation.conversation_id}
              <RateLimitProviderSwitch
                conversationId={retryConversationId}
                turnId={failedTurnRecovery.turnId}
                providerId={failedTurnRecovery.providerId ?? null}
                model={failedTurnRecovery.model ?? null}
                onResponse={async (response) => {
                  if (currentConversation?.conversation_id === retryConversationId) await applyCommandResponse(response);
                }}
              />
            {/if}
          </RecoveryBanner>
        {:else if error && lastRecoverableTurnId && !turnInProgress && !awaitingAssistantStart}
          <RecoveryBanner message={error} onRetry={retryLastTurn} />
        {/if}

        {#if initialLoadTimedOut && !currentConversation && !error && !connectivityOwnsNotices}
          <div class="rounded-2xl border border-info-border bg-info-subtle px-4 py-4 text-sm text-foreground">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p class="font-medium">Still loading conversation</p>
                <p class="mt-1 text-secondary">History or live session setup is taking longer than expected.</p>
              </div>
              <div class="flex gap-2">
                <Button size="sm" variant="secondary" onclick={() => void openConversation(conversationIdFromRoute())}>Retry</Button>
                <Button size="sm" variant="secondary" onclick={() => goto('/settings/system')}>Diagnostics</Button>
              </div>
            </div>
          </div>
        {/if}

        {#if controllerRecoveryPending && (criticalEventStoreError || !connectivityOwnsNotices)}
          <div class={criticalEventStoreError
            ? 'rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger'
            : 'rounded-2xl border border-info-border bg-info-subtle px-4 py-3 text-sm text-foreground'}>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>
                {criticalEventStoreError
                  ? `Critical service failure: ${criticalEventStoreError}. Cognis cannot load authoritative conversation history.`
                  : shouldContinueControllerRecovery(controllerRecoveryAttempts)
                  ? 'Controller is reconnecting. Your saved conversation remains visible and updates will retry automatically.'
                  : 'Controller is still unavailable. Your saved conversation remains visible; retry when it is back.'}
              </p>
              <Button size="sm" variant="secondary" onclick={() => void retryControllerRecoveryNow()}>Retry now</Button>
            </div>
          </div>
        {/if}

        {#if sessionsError && !isPreSessionConversation && !controllerRecoveryPending && !connectivityOwnsNotices}
          <div class="rounded-2xl border border-info-border bg-info-subtle px-3 py-3 text-sm text-foreground">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Session details are temporarily unavailable: {sessionsError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry</Button>
            </div>
          </div>
        {/if}

        {#if historyError && !isPreSessionConversation && !controllerRecoveryPending && !connectivityOwnsNotices}
          <div class="rounded-2xl border border-info-border bg-info-subtle px-4 py-3 text-sm text-foreground">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>{historyError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry history</Button>
            </div>
          </div>
        {/if}

        {#if escalationError && !controllerRecoveryPending && !connectivityOwnsNotices}
          <div class="rounded-2xl border border-info-border bg-info-subtle px-4 py-3 text-sm text-foreground">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Escalation updates are temporarily unavailable: {escalationError}</p>
              <Button size="sm" variant="secondary" onclick={() => refreshEscalations()}>Refresh escalations</Button>
            </div>
          </div>
        {/if}

        {#if cachedRefreshBannerVisible && cachedConversationRefreshing && currentConversation && !historyError && !sessionsError && !connectivityOwnsNotices}
          <div class="rounded-2xl border border-info-border bg-info-subtle px-3 py-2 text-sm text-foreground">
            <div class="flex items-center gap-2">
              <RefreshCw class="h-3.5 w-3.5 animate-spin" />
              <span>Showing cached conversation, refreshing latest messages…</span>
            </div>
          </div>
        {/if}

        <!-- Timeline -->
        {#if loadingOlderMessages}
          <!-- Keep announcements outside space-y content: even an absolutely
               positioned sr-only sibling changes its spacing selectors. -->
          <p class="sr-only" role="status" aria-live="polite">Loading earlier conversation history.</p>
        {/if}
        <TimelineViewport
          items={[]}
          hasStreamingItems={displayedTimelineHasStreaming}
          agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
          bind:viewportElement={timelineEl}
          bind:contentElement={timelineContentEl}
          bind:userScrolledUp
          class="relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain [overflow-anchor:auto] px-2.5 pb-1.5 pt-0 sm:px-4 sm:pb-4"
          contentClass="space-y-3 [overflow-anchor:auto]"
           onScroll={handleTimelineScroll}
           onPointerDown={handleTimelinePointerDown}
          onWheel={handleTimelineWheel}
          onTouchStart={handleTimelineTouchStart}
          onTouchMove={handleTimelineTouchMove}
          onTouchEnd={handleTimelineTouchEnd}
          onKeydown={handleTimelineKeydown}
           onViewSession={handleViewSession}
           onJumpToBottom={jumpToBottom}
          onJumpToActiveStart={jumpToActiveTimelineStart}
          autoScrollOnResize={false}
          interactionEnabled={timelineInitialPositionSettled}
          followPausedLabel="Scroll to bottom"
          testId="timeline-viewport"
        >
            {#if selectedRemoteSearchMatch}
              <section aria-label="Search match" data-chat-search-context class="border-l-2 border-accent bg-accent/10 px-4 py-3">
                <p class="text-xs font-medium capitalize text-secondary">{selectedRemoteSearchMatch.kind} · Search match</p>
                <p class="mt-1 whitespace-pre-wrap break-words text-sm text-foreground">{cleanSearchSnippet(selectedRemoteSearchMatch.snippet)}</p>
              </section>
            {/if}
            {#if !currentConversation && !error}
              <div class="rounded-2xl border border-dashed border-control-border px-4 py-10 text-center text-sm text-muted">
                {#if connectivityOwnsNotices}
                  Waiting for the connection to load this conversation.
                {:else if initialLoadTimedOut}
                  Conversation data is still loading. Use Retry above if it stays stuck.
                {:else}
                  Loading conversation history and preparing the live stream.
                {/if}
              </div>
            {:else if renderableVisibleItems.length === 0}
              {#if connectivityOwnsNotices && (historyError || sessionsError)}
                <p class="px-4 py-16 text-center text-sm text-quiet" role="status">Waiting for the connection to load this conversation.</p>
              {:else if (conversationSubloadsLoading || cachedConversationRefreshing || !canonicalTimelineIsReady(currentConversation?.conversation_id ?? '')) && !historyError && !sessionsError}
                <div class="rounded-2xl border border-dashed border-control-border px-4 py-10 text-center text-sm text-muted">
                  {#if initialLoadTimedOut}
                    Conversation data is still loading. Use Retry above if it stays stuck.
                  {:else}
                    Loading conversation history and preparing the live stream.
                  {/if}
                </div>
              {:else if !historyError && !sessionsError && !hasOlderMessages && !olderMessagesCursor && !hasAnyActiveTurnTimelineItem() && !turnInProgress && !pendingDirectQuestion && !pendingCredentialRequest && queuedMessages.length === 0}
                <p class="px-4 py-16 text-center text-sm text-quiet">
                  Send the first message to start this conversation.
                </p>
              {:else if !historyError && !sessionsError}
                <p class="px-4 py-16 text-center text-sm text-quiet" role="status">Loading conversation history.</p>
              {/if}
            {:else}
              <ChatV2TimelineList
                onHandoffConversation={(item) => {
                  openNewConversationModal();
                  if (!currentConversation) return;
                  newChatHandoff = item;
                  newChatHandoffSource = currentConversation.conversation_id;
                  newChatAgentId = currentConversation.agent_id;
                  newChatAgentProfileId = currentConversation.agent_profile_id || activeAgentProfileLabel() || '';
                  newChatProjectId = currentConversation.project_id || '';
                }}
                items={displayedTimeline}
                scope={currentConversation ? { key: `conversation:${currentConversation.conversation_id}`, kind: 'conversation', conversation_id: currentConversation.conversation_id } : undefined}
                cycleStates={chatV2Store.cycleStates}
                 runtime={chatV2Store.snapshot.runtime}
                 onProviderSwitchResponse={currentConversation && !isReadOnly(currentConversation)
                   ? async (response) => {
                     if (response.conversation_id === currentConversation?.conversation_id) await applyCommandResponse(response);
                   } : undefined}
                agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
                searchQuery={chatSearchQuery}
                searchMatchedIds={chatSearchOpen ? chatSearchMatchedMessageIds : emptySearchMatchedIds}
                 searchSelectedId={selectedChatSearchTargetId}
                 preferences={$userPreferences}
                 onViewSession={handleViewSession}
                  onOpenWork={(category, sessionId, path, sourceItemId) => openInspectorWork(category, sessionId, path && sourceItemId ? { workItemId: sourceItemId, files: [{ path }] } : undefined)}
              />
            {/if}

        </TimelineViewport>

        <div bind:this={footerChromeEl} class="shrink-0 space-y-3">
          <!--
            Escalation approval prompt: pinned footer chrome directly above
            the composer, not inside the scrolling timeline, so it stays
            visible without forced scrolling. Sequential: shows one
            escalation at a time and surfaces the queued count. A settled
            acknowledgement (decision-correct, rendered immediately after a
            successful REST resolve) only occupies this slot once the queue
            is empty, so a queued escalation always takes priority.
          -->
          {#if visibleEscalations.length > 0}
            {@const current = visibleEscalations[0]}
            <div class="px-3" data-testid="escalation-prompt-pinned">
              <EscalationPrompt
                item={current}
                secondsRemaining={escalationSecondsRemaining(current)}
                pending={escalationBusyCallId === current.call_id}
                queuedCount={visibleEscalations.length - 1}
                onApprove={(note) => handleEscalationDecision(current.call_id, 'approve', note)}
                onDeny={(note) => handleEscalationDecision(current.call_id, 'deny', note)}
              />
            </div>
          {:else if escalationSettledAck}
            <div class="px-3" data-testid="escalation-settled-ack">
              <div class={`w-full rounded-[1.4rem] border px-4 py-3 shadow-card sm:rounded-3xl ${escalationSettledAck.decision === 'approve' ? 'border-success-border bg-success-subtle text-success' : 'border-warning-border bg-warning-subtle text-warning'}`}>
                <div class="flex items-center gap-2.5">
                  <span class="h-2 w-2 shrink-0 rounded-full bg-current opacity-80" aria-hidden="true"></span>
                  <div class="min-w-0 flex-1">
                    <p class="text-sm font-semibold">
                      {escalationSettledAck.decision === 'approve' ? 'Approved' : 'Denied'} · {escalationSettledAck.toolName ?? 'Escalated action'}
                    </p>
                  </div>
                </div>
              </div>
            </div>
          {/if}
          {#if !childView}
          {#if pinnedTransientNotice}
            <div class="px-3" data-testid="pinned-transient-notice">
              <ChatV2TimelineItemRenderer
                item={pinnedTransientNotice}
                pinnedTransient={true}
                scope={currentConversation ? { key: `conversation:${currentConversation.conversation_id}`, kind: 'conversation', conversation_id: currentConversation.conversation_id } : undefined}
                agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
              />
            </div>
          {/if}
          {#if shouldShowOngoingWorkDrawer}
            <TimelineOngoingWorkDrawer
              todos={chatTodos}
              work={currentConversationWork}
              truncated={backgroundWork.truncated}
              bind:open={ongoingWorkDrawerOpen}
              onViewSession={handleViewSession}
            />
          {/if}
          {/if}

          <!-- Composer or read-only banner -->
          {#if currentConversation && isManagedConversation(currentConversation)}
            <ManagedConversationControls
              conversation={currentConversation}
              busy={managedActionBusy}
              error={managedActionError}
              onStop={() => void stopManagedConversation()}
              onTerminate={() => void terminateManagedConversation()}
              onSend={(message) => {
                managedInstruction = message;
                void sendManagedInstruction();
              }}
              onTakeControl={() => void takeControlOfManagedConversation()}
            />
            <!-- Legacy inline controls retained only in git history.
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
            -->
          {:else if currentConversation && currentConversation.is_readonly_for_caller}
            <div class="mx-3 mb-2 flex h-9 items-center justify-between gap-2 rounded-xl border border-control-border bg-surface/60 px-3 text-xs text-muted sm:mx-5" data-testid="shared-conversation-banner">
              <span class="truncate" title={`Shared by ${sharedConversationAuthor(currentConversation)}${canContinueSharedConversation(currentConversation) ? '' : '. Agent access is required to continue in your own chat.'}`}>
                Read-only
                <span class="text-quiet"> · {sharedConversationAuthor(currentConversation)}</span>
              </span>
              {#if canContinueSharedConversation(currentConversation)}
                <button type="button" class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-secondary hover:bg-surface-hover hover:text-strong disabled:opacity-50" aria-label="Continue in my chat" title="Continue in my chat" disabled={newChatCreating} onclick={() => void continueSharedConversation()}><GitFork class="h-4 w-4" /></button>
              {/if}
            </div>
          {:else if currentConversation && !isWebConversation(currentConversation)}
            <div class="rounded-2xl border border-control-border bg-surface/60 px-4 py-3 text-center text-sm text-muted">
              {#if currentConversation.context?.type === 'task' && currentConversation.context.ref}
                This is a task step and is read-only here. <a class="font-medium text-accent hover:underline" href={`/tasks/${currentConversation.context.ref}`}>Open the task</a> to follow up or change it.
              {:else}
                This conversation is from <span class="font-medium text-secondary">{contextTypeBadge(currentConversation)}</span>. Read-only in web UI.
              {/if}
            </div>
          {:else if currentConversation && currentConversation.status === 'archived'}
            <div class="rounded-2xl border border-control-border bg-surface/60 px-4 py-3 text-center text-sm text-muted">
              This conversation is archived.
            </div>
          {:else if currentConversation && currentConversation.status === 'deleted'}
            <div class="rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-center text-sm text-danger">
              This conversation has been deleted.
            </div>
          {:else if isSessionBlocked()}
            <div class="space-y-3 rounded-2xl border border-info-border bg-info-subtle px-4 py-3 text-center text-sm text-info">
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
            {#if pendingDirectQuestionVisible && pendingDirectQuestion && !directQuestionAck}
              <PendingRequestPanel id="pending-direct-question-panel" title="Assistant requested more input" bind:collapsed={directQuestionCollapsed}>
                  {#snippet status()}
                      {#if directQuestionSubmitting}
                        <LiveDots inline={true} size="sm" label="Answering" />
                      {:else if directQuestionVisibleItems.length > 1}
                        <span class="rounded-full border border-info-border bg-inset/40 px-2 py-0.5 font-mono text-[11px] text-info">
                          {directQuestionCurrentIndex + 1}/{directQuestionVisibleItems.length}
                        </span>
                      {/if}
                  {/snippet}
                  {#if pendingDirectQuestion.context}
                    <p class="mt-2 max-h-16 overflow-y-auto overscroll-contain text-xs leading-5 text-secondary">{pendingDirectQuestion.context}</p>
                  {/if}
                  {#if currentDirectQuestion}
                    {#key `${currentDirectQuestion.id}:${directQuestionCurrentIndex}`}
                    <div class="rounded-2xl border border-info-border bg-surface p-3">
                      {#if currentDirectQuestion.header}
                        <p class="text-xs uppercase tracking-[0.2em] text-secondary">{currentDirectQuestion.header}</p>
                      {/if}
                      <p class="text-sm font-medium text-foreground">{currentDirectQuestion.question}</p>
                      {#if currentDirectQuestion.required}
                        <p class="mt-1 text-[11px] uppercase tracking-[0.18em] text-secondary">Required</p>
                      {:else}
                        <p class="mt-1 text-[11px] uppercase tracking-[0.18em] text-muted">Optional</p>
                      {/if}
                      {#if currentDirectQuestion.options.length > 0}
                        <div class="mt-3 space-y-2">
                          {#each currentDirectQuestion.options as option (option.id)}
                            <button
                              class={`flex w-full items-start gap-3 rounded-2xl border px-3 py-2 text-left text-xs transition ${directQuestionOptionSelected(currentDirectQuestion.id, option.id) ? 'border-accent bg-accent-subtle text-strong' : 'border-control-border bg-surface text-foreground hover:bg-surface-hover'}`}
                              type="button"
                              disabled={directQuestionSubmitting}
                              onclick={() => { submitDirectQuestionOption(currentDirectQuestion, option.id); }}
                            >
                              <span class={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center border ${currentDirectQuestion.multiple ? 'rounded' : 'rounded-full'} ${directQuestionOptionSelected(currentDirectQuestion.id, option.id) ? 'border-accent bg-accent text-on-accent' : 'border-control-border bg-surface'}`}>
                                {#if directQuestionOptionSelected(currentDirectQuestion.id, option.id)}
                                  {#if currentDirectQuestion.multiple}
                                    ✓
                                  {:else}
                                    <span class="h-1.5 w-1.5 rounded-full bg-on-accent"></span>
                                  {/if}
                                {/if}
                              </span>
                              <span class="min-w-0">
                                <span class="block font-medium">{option.label}</span>
                                {#if option.description}
                                  <span class="mt-0.5 block text-secondary">{option.description}</span>
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
                          class="mt-3 min-h-[72px] w-full rounded-2xl border border-control-border bg-inset px-3 py-2 text-sm text-foreground placeholder:text-muted focus:border-accent"
                          placeholder={currentDirectQuestion.options.length > 0 ? 'Optional custom answer' : 'Type your answer'}
                          disabled={directQuestionSubmitting}
                        ></textarea>
                      {/if}
                    </div>
                    {/key}
                  {:else}
                    <p class="rounded-2xl border border-info-border bg-surface p-3 text-secondary">Waiting for question details…</p>
                  {/if}
                  {#if currentDirectQuestion?.options.length}
                    <p class="mt-2 text-xs text-secondary">Selecting an option only updates this answer. Use Next or Submit to continue.</p>
                  {/if}
                  {#if directQuestionVisibleItems.length > 1}
                    <div class="mt-3 grid gap-1" style={`grid-template-columns: repeat(${Math.min(directQuestionVisibleItems.length, 7)}, minmax(0, 1fr));`} aria-hidden="true">
                      {#each directQuestionVisibleItems as item, index (item.id)}
                        <span class={`h-1.5 rounded-full ${index === directQuestionCurrentIndex ? 'bg-accent' : directQuestionAnswerSatisfies(item) && (directQuestionState(item.id).selected.length > 0 || directQuestionState(item.id).custom.trim()) ? 'bg-accent/60' : 'bg-control-border'}`}></span>
                      {/each}
                    </div>
                  {/if}
                {#snippet footer()}
                {#if currentDirectQuestion}
                  <div class="flex flex-wrap items-center gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      class="border-danger-border bg-danger-subtle text-danger hover:bg-surface-hover"
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
                {/snippet}
              </PendingRequestPanel>
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
            {#if !childView}
            {@const quickProfileAgent = currentConversation ? conversationAgent(currentConversation) : null}
            {#snippet composerSettings()}
             {#if quickProfileAgent && !turnInProgress && !awaitingAssistantStart}
              <AgentQuickProfileSwitcher
                 agent={quickProfileAgent}
                  runtimeSelection={currentConversation?.active_session_id
                    ? quickProfileRuntimeBySession[currentConversation.active_session_id] ?? null
                    : null}
                profileId={latestRuntimeSelection && latestRuntimeSelection.sessionId === currentConversation?.active_session_id
                  ? latestRuntimeSelection.selection.profile_id
                  : activeAgentProfileLabel()}
                disabled={turnInProgress || awaitingAssistantStart || composerAdmissionBusy || voiceTranscribing}
                busy={quickProfileBusy}
                hasDraft={Boolean(composer.trim() || composerAttachments.length)}
                conversationId={currentConversation?.conversation_id}
                onSelect={(profileId) => { void selectQuickProfile(profileId); }}
              />
            {/if}
            {/snippet}
            {#if voiceTranscribing}
              <div class="flex items-center gap-2 rounded-2xl border border-info-border bg-info-subtle px-3 py-2 text-xs text-info" aria-live="polite">
                <span class="inline-block h-2 w-2 animate-pulse rounded-full bg-info"></span>
                Transcribing voice message…
              </div>
            {/if}
            {#if persistentChatMode === 'plan'}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-blue-border bg-blue-subtle px-3 py-2 text-xs text-blue-tone" aria-live="polite">
                <span><strong class="font-semibold">Plan mode active.</strong> This chat is read-only until you send <span class="font-mono">/build</span> or <span class="font-mono">/default</span>.</span>
                <span class="rounded-full border border-blue-border bg-blue-subtle px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.2em] text-blue-tone">Plan</span>
              </div>
            {:else if persistentChatMode === 'build'}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-orange-border bg-orange-subtle px-3 py-2 text-xs text-orange-tone" aria-live="polite">
                <span><strong class="font-semibold">Build mode active.</strong> This chat is using implementation mode until you send <span class="font-mono">/default</span>.</span>
                <span class="rounded-full border border-orange-border bg-orange-subtle px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.2em] text-orange-tone">Build</span>
              </div>
            {/if}
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
               * Software-keyboard Enter inserts a newline. Physical-keyboard
               * Enter follows the persisted Enter-to-send preference.
            -->
              {@const capturedComposerRecovery = composerRecoveryScope}
              <LegacyComposerRecovery candidates={composerLegacyCandidates} onSelect={(candidate, mode) => {
                if (!canSelectRecovery(capturedComposerRecovery, composerRecoveryScope, conversationIdFromRoute(),
                  conversationLoadRequestId, composerAlive, candidate)) return;
                const chosen = selectLegacyInput(candidate, mode);
                composer = chosen.text; composerAttachments = chosen.attachments;
                composerInputRevision = chosen.input_revision; composerInputMode = chosen.chat_mode; composerRevision++;
                composerLegacyCandidates = []; composerLegacyRecovery = false;
                if (currentDraftKey) writeDraft(currentDraftKey, composer);
              }} />
             <ChatV2Composer
                feedback={includedFeedback}
                bind:value={() => composer, (value) => { editComposerInput(); composer = value; }}
                bind:attachments={() => composerAttachments, (value) => { editComposerInput(); composerAttachments = value; }}
              bind:textarea={composerElement}
              conversationId={currentConversation?.conversation_id}
              placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : pendingDirectQuestion ? 'Answer the pending clarification request...' : `Message ${currentAgentDisplayName}`}
              ariaLabel="Chat message"
              settings={composerSettings}
              disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup() || directQuestionSubmitting || voiceTranscribing}
               busy={directQuestionSubmitting || voiceTranscribing || quickProfileBusy}
              active={turnInProgress}
              stopping={cancelTurnPending}
              allowQueue
              onSend={handleSend}
              onStop={() => { haptic.warning(); cancelActiveTurnWithChatV2(); }}
              onFiles={uploadFiles}
              onRemoveAttachment={cancelComposerAttachment}
              onRetryAttachment={retryComposerAttachment}
              onInput={syncComposerHeight}
              onPaste={handlePaste}
              onOpenMode={openConversationMode}
              onSendRecorded={async () => {
                await tick();
                await handleSend();
              }}
            />
            {/if}
            {/if}
          </form>
          {/if}
        </div>
      </div>

      </div>

      {#if childView && childWorkstream}
        {@const deferredChildView = childView}
        {@const deferredChildNode = childWorkstream}
        <aside class={`${inspectorPinned ? 'relative col-start-1 row-start-1 row-end-4' : 'absolute inset-0'} min-h-0 min-w-0 overflow-hidden`} data-testid="child-middle-column">
          <DeferredView load={() => import('$lib/components/chat-v2/ChildChatView.svelte')}
            label="Loading child conversation…" onCancel={closeSubSessionPanel}>
          {#snippet children(loaded)}
          <loaded.default
            view={deferredChildView}
            node={deferredChildNode}
            agent={subSessionAgent() ?? null}
            preferences={$userPreferences}
            work={childBackgroundWork}
            workTruncated={backgroundWork.truncated}
            inspectorOpen={headerInfoOpen}
            bind:userScrolledUp={subSessionUserScrolledUp}
            onBack={navigateToStructuralParent}
            onClose={closeSubSessionPanel}
            onToggleInspector={(trigger) => {
              headerInfoTrigger = trigger;
              conversationInfoDrawer.setOpen(!headerInfoOpen);
              if (conversationInfoDrawer.open) {
                if (headerInfoMode === 'session') void loadSessionInfo();
                else if (headerInfoMode === 'overview') loadVisibleActivityOverview();
              } else {
                cancelActivityOverviewDemand();
              }
            }}
            onViewSession={handleViewSession}
            onRuntimeActiveChange={((boundSessionId: string) => (active: boolean) => {
              // Bind the session ID at the moment this ChildChatView instance
              // is rendered, not when the (possibly delayed/async) event
              // fires: by then `childView` may already refer to a different,
              // newly-selected child, which would otherwise misattribute the
              // update to the wrong session.
              const currentNode = (scopedRootActivityOverview?.workstreams ?? [])
                .find((item) => item.session_id === boundSessionId)
                ?? (childView?.sessionId === boundSessionId ? childWorkstream : undefined);
              const terminal = currentNode?.activity_state === 'closed'
                || isTerminalSessionStatus(currentNode?.status);
              runtimeActiveChildSessionIds = updateRuntimeActiveSessions(
                runtimeActiveChildSessionIds,
                boundSessionId,
                active && !terminal,
                currentNode?.status,
                currentNode?.activity_state,
              );
            })(deferredChildView.sessionId)}
            managedConversation={childManagedConversation}
            managedBusy={childManagedBusy}
            managedError={childManagedError}
            onManagedStop={() => void runChildManagedAction('stop', { reason: 'Stopped by user from managed conversation UI' })}
            onManagedSend={(message) => void runChildManagedAction('send', { message, wait: false })}
             onManagedTakeControl={() => void runChildManagedAction('take-control')}
             onManagedTerminate={() => void runChildManagedAction('close')}
          />
          {/snippet}
          </DeferredView>
        </aside>
      {/if}
      {/if}
    </section>
  </div>
{/if}

{#if showNewChatModal}
  {#await import('$lib/components/NewChatModal.svelte')}
    <div class="fixed inset-0 z-50 grid place-content-center gap-3 bg-base/90" role="status">
      <span>Loading new chat…</span>
      <Button variant="secondary" onclick={closeNewConversationModal}>Cancel</Button>
    </div>
  {:then module}
    <module.default
      profileOnly={newChatHandoff !== null}
      title={newChatHandoff ? 'Continue from handoff' : 'New chat'}
      description={newChatHandoff ? 'Choose a profile. The handoff opens as an unsent draft.' : 'Choose the primary agent for the new conversation.'}
      agents={agents.filter(agent => !agent.is_contact_only)}
      bind:selectedAgentId={newChatAgentId}
      bind:selectedAgentProfileId={newChatAgentProfileId}
      bind:selectedAgentProfileMode={newChatAgentProfileMode}
      {projects}
      bind:selectedProjectId={newChatProjectId}
      busy={newChatCreating}
      error={newChatError}
      oncancel={closeNewConversationModal}
      onconfirm={() => void createNewConversation()}
    />
  {:catch}
    <div class="fixed inset-0 z-50 grid place-content-center gap-3 bg-base/90" role="alert">
      <span>Could not load new chat. Close and try again.</span>
      <Button variant="secondary" onclick={closeNewConversationModal}>Close</Button>
    </div>
  {/await}
{/if}

{#if conversationModeOpen}
<DeferredView load={() => import('$lib/components/ConversationMode.svelte')}
  label="Loading voice conversation…" overlay onCancel={closeConversationMode}>
{#snippet children(loaded)}
<loaded.default
  open={conversationModeOpen}
  cues={conversationCues}
  cycleStates={chatV2Store.cycleStates}
  conversationId={currentConversation?.conversation_id ?? ''}
  agent={currentConversation ? conversationAgentForDisplay(currentConversation) : null}
  onclose={closeConversationMode}
  submitText={sendConversationModeText}
  reconcileTurn={reconcileConversationModeTurn}
  stopAssistant={cancelActiveTurnWithChatV2}
  runtime={chatV2Store.snapshot.conversationId === currentConversation?.conversation_id
    ? chatV2Store.snapshot.runtime : null}
  items={conversationModeOpen ? chatV2Store.visibleItems : []}
/>
{/snippet}
</DeferredView>
{/if}

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

  @media (min-width: 1024px) {
    .chat-history-sidebar {
      width: var(--chat-sidebar-width);
      flex: 0 0 var(--chat-sidebar-width);
    }
  }

</style>
