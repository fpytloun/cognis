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
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import ChatSearchBar from '$lib/components/ChatSearchBar.svelte';
  import CredentialRequestForm from '$lib/components/CredentialRequestForm.svelte';
  import ComposerAttachments from '$lib/components/ComposerAttachments.svelte';
  import ConversationMode from '$lib/components/ConversationMode.svelte';
  import MicRecorderButton from '$lib/components/MicRecorderButton.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import NewChatModal from '$lib/components/NewChatModal.svelte';
  import TimelineList from '$lib/components/timeline/TimelineList.svelte';
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
    cloneSidebarProjection,
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
    hasRetryableFailedTurnTail,
    groupConversationsByActivity,
    managedConversationTurnState,
    mergeConversationPreservingActivity,
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
    rememberSidebarProjectionSnapshot,
    isPreSessionChatConversation,
    pendingNotificationTypesFromNotifications,
    setConversationStatusSearchParam,
    shouldReconcileAfterReconnect,
    shouldAdoptConversationSessionId,
    shouldSuppressPreSessionSocketError,
    nextConversationLoadId,
    nextPollDelayMs,
    type ChatModeTone,
    type ConversationStatusFilter,
    type PendingDirectQuestion,
    CHAT_STORAGE_KEYS,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { loadSessionLog, refreshSessionLog, type SessionLogState } from '$lib/session-log';
  import { edgeSwipe } from '$lib/actions/edgeSwipe';
  import { scrollPersist } from '$lib/actions/scrollPersist';
  import { confirmAction } from '$lib/stores/confirm';
  import { mobileNavOpen as mobileNavOpenStore, requestOpenMobileNav } from '$lib/stores/mobileNav';
  import { registerOverlay } from '$lib/stores/overlays';
  import { canAttemptPwaAuxiliaryWindow } from '$lib/stores/pwa';
  import { onTabReset } from '$lib/stores/tabReset';
  import { addToast } from '$lib/stores/toasts';
  import { haptic } from '$lib/haptics';
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
    mergeSearchResultsByTarget,
    type ChatSearchResult,
    type LocalChatMatch
  } from '$lib/chat-search';
  import {
    annotateStepRequestInputWithNotification,
    applyRuntimeSnapshotOverlay,
    applyRuntimeTimelineEvent,
    applyTimelinePatch,
    applyWebSocketEvent,
    appendOptimisticUserMessage,
    clearRuntimeOverlayForCanonicalEvent,
    createRuntimeTimelineOverlay,
    findPendingStepRequestInputCall,
    hydrateMessageCompleteFromRuntimeOverlay,
    isActiveToolStatus,
    isRuntimeTimelineEvent,
    latestTodoSnapshot,
    parseTodoSnapshot,
    projectDisplayTimeline,
    promoteRuntimeOverlayForCanonicalEvent,
    optimisticallyResolveStepRequestInput,
    normalizeHistory,
    removeQueuedOptimisticUserMessage,
    type MessageTimelineItem,
    timelineFromProjection,
    timelinePatchContainsActiveWork,
    timelineItemKey,
    type TimelineItem,
    type TodoSnapshotItem
  } from '$lib/chat';
  import { incompleteTodos, visibleTodos as activeVisibleTodos } from '$lib/todos';
  import type { ActiveThinkingSnapshot, Agent, AgentDirectChat, AttachmentRef, CognisWebSocketEvent, ContextUsage, Conversation, ConversationSearchMatch, ConversationStateEnvelope, Escalation, MessageEvent, Notification, QueuedMessage, QuestionSetAnswer, QuestionSetQuestion, QuestionSetReply, Session, SidebarProjection } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';

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
  let currentConversation = $state<Conversation | null>(null);
  let sessions = $state<Session[]>([]);
  let conversationTodoSnapshots = $state<Record<string, TodoSnapshotItem[]>>({});
  let conversationSubloadsLoading = $state(false);
  let cachedConversationRefreshing = $state(false);
  let composer = $state('');
  let composerElement = $state<HTMLTextAreaElement | null>(null);
  let composerAttachments = $state<AttachmentRef[]>([]);
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
  let selectedAgentId = $state('');
  let selectedConversationStatus = $state<ConversationStatusFilter>(parseConversationStatusFilter(page.url.searchParams.get('status')));
  let starringConversationId = $state<string | null>(null);
  let archivingConversation = $state(false);
  let deletingConversation = $state(false);
  let mobileListOpen = $state(false);
  let mobileListOverlayCleanup: (() => void) | null = null;
  let conversationFiltersOpen = $state(false);
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
  let queueEditingId = $state<string | null>(null);
  let queueEditContent = $state('');
  let queueExpandedIds = $state<string[]>([]);
  let timeline = $state<TimelineItem[]>([]);
  let runtimeOverlay = $state(createRuntimeTimelineOverlay());

  let visibleStartIndex = $state(0);
  let activeConversationId = '';
  let routeConversationId = $derived(page.params.conversationId ?? '');
  let activeSessionLastSeq = 0;
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
  let escalationCountdownTimer: number | null = null;
  let notificationRefreshTimer: number | null = null;
  let pushPromptDismissed = $state(hasDismissedWebPushPrompt());
  let pushSubscriptionKnownEnabled = $state(hasEnabledWebPush());
  let pushPromptBusy = $state(false);
  let pushPromptError = $state('');
  let pushDeliveryError = $state('');
  let awaitingAssistantStart = $state(false);
  let turnInProgress = $state(false);
  let activeTurnChatMode = $state<ChatModeTone>('default');
  let lastSubmittedMessage = '';
  let lastRecoverableMessage = $state('');
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
  let subSessionTimeline = $state<TimelineItem[]>([]);
  let subSessionLog = $state<SessionLogState | null>(null);
  let subSessionLoading = $state(false);
  let subSessionError = $state('');
  let subSessionPollDelayMs = $state(SESSION_LOG_POLL_INTERVAL_MS);
  let subSessionTimelineEl = $state<HTMLDivElement | null>(null);
  let subSessionTimelineContentEl = $state<HTMLDivElement | null>(null);
  let subSessionUserScrolledUp = $state(false);
  let timelineEl = $state<HTMLDivElement | null>(null);
  let timelineContentEl = $state<HTMLDivElement | null>(null);
  let userScrolledUp = $state(false);
  let loadingOlderMessages = $state(false);
  let programmaticScroll = false;
  let userScrollIntentUp = false;
  let lastTimelineTouchY: number | null = null;
  let lastTimelineScrollTop = $state(0);
  let footerChromeEl = $state<HTMLDivElement | null>(null);
  let selectedChannel = $state('all');
  let chatSidebarCollapsed = $state(false);
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
  }
  let sessionInfo = $state<SessionInfoData | null>(null);
  let sessionInfoLoading = $state(false);
  let sessionInfoRequestId = 0;
  let sessionNarrativeExpanded = $state(false);

  interface ConversationViewCacheEntry {
    conversation: Conversation;
    sessions: Session[];
    timeline: TimelineItem[];
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
    return sessionInfo?.context_usage ?? contextUsage;
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
    for (const message of messages) {
      timeline = removeQueuedOptimisticUserMessage(
        timeline,
        message.queue_id,
        message.client_message_id,
        message.content,
        message.attachments,
      );
    }
    const liveQueueIds = new Set(messages.map((message) => message.queue_id));
    queueExpandedIds = queueExpandedIds.filter((queueId) => liveQueueIds.has(queueId));
    if (queueEditingId && !liveQueueIds.has(queueEditingId)) {
      queueEditingId = null;
      queueEditContent = '';
    }
  }

  function applyConversationRuntimeSnapshot(
    event: Extract<CognisWebSocketEvent, { type: 'conversation_runtime_snapshot' }>
  ): void {
    applyQueuedMessageSnapshot(event.queued_messages, event.queued_count);
    const hasRuntimeActivity = (
      event.active_streams.length > 0
      || event.active_tool_outputs.length > 0
      || event.active_thinking.length > 0
    );
    runtimeOverlay = applyRuntimeSnapshotOverlay(runtimeOverlay, event);
    const hasActiveTurn = event.has_active_turn ?? currentConversation?.has_active_turn ?? turnInProgress;
    turnInProgress = hasRuntimeActivity || hasActiveTurn;
    if (typeof event.has_active_turn === 'boolean' && currentConversation) {
      patchConversationInList(currentConversation.conversation_id, {
        has_active_turn: event.has_active_turn,
        active_turn_chat_mode: event.active_turn_chat_mode ?? null,
        active_turn_chat_mode_source: event.active_turn_chat_mode_source ?? null,
      });
      activeTurnChatMode = event.has_active_turn
        ? normalizeChatModeTone(event.active_turn_chat_mode)
        : 'default';
    }
    setConversationTurnIndicator(currentConversation?.conversation_id, turnInProgress);
    if (turnInProgress) {
      awaitingAssistantStart = false;
    }
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
    queuedMessages = queuedMessages.filter((item) => item.queue_id !== queueId);
    queuedCount = queuedMessages.length;
    try {
      await api.conversations.deleteQueuedMessage(conversationId, queueId);
    } catch (caughtError) {
      queuedMessages = previous;
      queuedCount = previous.length;
      addToast(asApiError(caughtError).message, 'error');
      await refreshQueuedMessages();
    } finally {
      queueBusyId = null;
    }
  }

  async function saveQueuedMessage(queueId: string): Promise<void> {
    const conversation = mutableQueuedMessagesConversation();
    if (!conversation) return;
    const conversationId = conversation.conversation_id;
    const content = queueEditContent.trim();
    if (!content) return;
    queueBusyId = queueId;
    try {
      const updated = await api.conversations.updateQueuedMessage(conversationId, queueId, content);
      queuedMessages = queuedMessages.map((item) => item.queue_id === queueId ? updated : item);
      queueEditingId = null;
      queueEditContent = '';
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error');
      await refreshQueuedMessages();
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

  function startQueuedMessageEdit(queued: QueuedMessage): void {
    if (!canMutateQueuedMessages()) return;
    queueEditingId = queued.queue_id;
    queueEditContent = queued.content;
    if (!queueIsExpanded(queued.queue_id)) {
      queueExpandedIds = [...queueExpandedIds, queued.queue_id];
    }
  }

  function cancelQueuedMessageEdit(): void {
    queueEditingId = null;
    queueEditContent = '';
  }

  function canMutateQueuedMessages(): boolean {
    return mutableQueuedMessagesConversation() !== null;
  }

  function mutableQueuedMessagesConversation(): Conversation | null {
    if (!currentConversation || isReadOnly(currentConversation)) return null;
    return currentConversation;
  }
  let contextUsage = $state<ContextUsage | null>(null);
  let subSessionInfoOpen = $state(false);
  let subSessionInfo = $state<SessionInfoData | null>(null);
  let subSessionInfoLoading = $state(false);
  type ChatTodo = TodoSnapshotItem;

  let pendingDirectQuestion = $state<PendingDirectQuestion | null>(null);
  let directQuestionAnswers = $state<Record<string, { selected: string[]; custom: string }>>({});
  let directQuestionPageIndex = $state(0);
  let directQuestionCollapsed = $state(false);
  let pendingCredentialRequest = $state<Notification | null>(null);
  let directQuestionSubmitting = $state(false);
  let chatTodoDrawerOpen = $state(true);
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

  function isSessionBlocked(): boolean {
    const status = activeSessionStatus();
    return status !== null && BLOCKED_SESSION_STATES.has(status);
  }

  function startNewConversationFromBlockedSession(): void {
    if (!currentConversation || isReadOnly(currentConversation)) return;
    error = '';
    awaitingAssistantStart = false;
    turnInProgress = false;
    activeTurnChatMode = 'default';
    clearConversationTurnState(currentConversation.conversation_id);
    wsClient.sendMessage(currentConversation.conversation_id, '/new', [], null);
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

  function setDirectQuestionCustom(questionId: string, value: string): void {
    const current = directQuestionState(questionId);
    directQuestionAnswers = {
      ...directQuestionAnswers,
      [questionId]: { ...current, custom: value }
    };
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
      window.sessionStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, conversation.conversation_id);
      window.sessionStorage.setItem(lastOpenedConversationStorageKey(conversation.agent_id), conversation.conversation_id);
      window.localStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, conversation.conversation_id);
      window.localStorage.setItem(lastOpenedConversationStorageKey(conversation.agent_id), conversation.conversation_id);
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
        if (storage.getItem(key) === conversationId) {
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
    const latestTodos = latestTodoSnapshot(timeline, currentConversation?.context?.type === 'web');
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

  function visibleTodoSnapshot(todos: TodoSnapshotItem[] | undefined): TodoSnapshotItem[] {
    return activeVisibleTodos(todos);
  }

  function hasIncompleteTodo(todos: TodoSnapshotItem[]): boolean {
    return todos.some((todo) => todo.status !== 'completed' && todo.status !== 'cancelled');
  }

  function setConversationTodoSnapshot(conversationId: string | null | undefined, todos: TodoSnapshotItem[]): void {
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

  function conversationTodoProgressTodos(conversation: Conversation): TodoSnapshotItem[] {
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

  function todoSnapshotFromSocketEvent(event: import('$lib/types/api').CognisWebSocketEvent): TodoSnapshotItem[] | null {
    if (event.type === 'tool_call' && event.tool_name === 'step_todo_write') {
      return parseTodoSnapshot(event.arguments?.todos);
    }
    if (event.type === 'tool_result' && (event.tool_name === 'step_todo_write' || event.tool_name === 'step_todo_list')) {
      try {
        const parsed = JSON.parse(event.result.replace(/^<tool_result[^>]*>\n?/, '').replace(/\n?<\/tool_result>\s*$/, ''));
        return parseTodoSnapshot((parsed as Record<string, unknown>)?.todos);
      } catch {
        return null;
      }
    }
    return null;
  }

  function backendTodoSnapshot(state: ConversationStateEnvelope | null): TodoSnapshotItem[] | null {
    if (!state?.task) return null;
    const step = state.task.relevant_step ?? state.task.current_step ?? null;
    if (!step) return null;
    return step.todos.map((todo) => ({
      content: todo.content,
      status: todo.status,
      priority: todo.priority ?? 'normal',
    }));
  }

  function conversationStateConversationPatch(state: ConversationStateEnvelope): Partial<Conversation> {
    const patch: Partial<Conversation> = {
      pending_notification_types: state.pending?.notification_types ?? [],
      has_active_turn: state.active_turn?.has_active_turn ?? false,
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
    if (conversationId === currentConversation?.conversation_id) {
      backendConversationState = state;
      turnInProgress = state.active_turn?.has_active_turn ?? turnInProgress;
    }
    if (patchConversationRows) {
      patchConversationInList(conversationId, conversationStateConversationPatch(state));
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
        preserveTimelineOnHistoryFailure: true,
        mergeTimeline: true,
      });
    }
  }
  let showTurnProgress = $derived.by(() =>
    turnInProgress
      && liveTailTimelineItemKey === null
  );

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
    const next = reset ? [] : conversations.filter((conversation) => !isAgentDirectConversation(conversation));
    const indexById = new Map(next.map((conversation, index) => [conversation.conversation_id, index]));
    for (const conversation of items) {
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
        continue;
      }
      const index = indexById.get(conversation.conversation_id);
      if (index === undefined) {
        indexById.set(conversation.conversation_id, next.length);
        next.push(conversation);
      } else {
        next[index] = mergeConversationPreservingActivity(next[index], conversation);
      }
    }
    conversations = sortConversationsByActivity(next);
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

  function patchConversationInList(
    conversationId: string,
    patch: Partial<Conversation>,
    options: { touchUpdatedAt?: boolean; touchLastMessageAt?: boolean } = {}
  ): void {
    const index = conversations.findIndex((conversation) => conversation.conversation_id === conversationId);
    const now = new Date().toISOString();
    if (index < 0) {
      const direct = agentDirectChats.find((item) => item.conversation.conversation_id === conversationId);
      if (direct) {
        const updatedDirectConversation: Conversation = {
          ...direct.conversation,
          ...patch,
          updated_at: options.touchUpdatedAt ? patch.updated_at ?? now : patch.updated_at ?? direct.conversation.updated_at,
          last_message_at: options.touchLastMessageAt ? patch.last_message_at ?? now : patch.last_message_at ?? direct.conversation.last_message_at,
        };
        patchAgentDirectChat(updatedDirectConversation);
        if (currentConversation?.conversation_id === conversationId) {
          currentConversation = { ...currentConversation, ...updatedDirectConversation };
        }
        rememberSidebarProjection();
      }
      return;
    }
    const existing = conversations[index];
    const updated: Conversation = {
      ...existing,
      ...patch,
      updated_at: options.touchUpdatedAt ? patch.updated_at ?? now : patch.updated_at ?? existing.updated_at,
      last_message_at: options.touchLastMessageAt ? patch.last_message_at ?? now : patch.last_message_at ?? existing.last_message_at,
    };
    const next = [...conversations];
    next[index] = updated;
    conversations = sortConversationsByActivity(next);
    patchAgentDirectChat(updated);
    if (currentConversation?.conversation_id === conversationId) {
      currentConversation = { ...currentConversation, ...updated };
    }
    rememberSidebarProjection();
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

  function recordTodoSnapshotFromSocketEvent(event: import('$lib/types/api').CognisWebSocketEvent): void {
    const conversationId = 'conversation_id' in event && typeof event.conversation_id === 'string'
      ? event.conversation_id
      : currentConversation?.conversation_id;
    const todos = todoSnapshotFromSocketEvent(event);
    if (todos !== null) {
      setConversationTodoSnapshot(conversationId, todos);
    }
  }

  function copyTimelineItems(items: TimelineItem[]): TimelineItem[] {
    return items.map((item) => ({ ...item }));
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
    if (!currentConversation) return;
    touchConversationViewCache(currentConversation.conversation_id, {
      conversation: { ...currentConversation },
      sessions: sessions.map((session) => ({ ...session })),
      timeline: copyTimelineItems(timeline),
      queuedCount,
      queuedMessages: queuedMessages.map((message) => ({ ...message })),
      contextUsage,
      sessionInfo: sessionInfo ? { ...sessionInfo } : null,
      visibleStartIndex,
      scrollTop: timelineEl?.scrollTop ?? lastTimelineScrollTop,
      userScrolledUp,
      turnInProgress,
      awaitingAssistantStart,
      activeSessionLastSeq,
      olderMessagesCursor,
      hasOlderMessages,
      activeTurnChatMode,
      fetchedAt: Date.now(),
    });
  }

  function restoreConversationView(conversationId: string): ConversationViewCacheEntry | null {
    const entry = conversationViewCache.get(conversationId);
    if (!entry) return null;
    touchConversationViewCache(conversationId, entry);
    activeConversationId = conversationId;
    currentConversation = { ...entry.conversation };
    sessions = entry.sessions.map((session) => ({ ...session }));
    timeline = copyTimelineItems(entry.timeline);
    runtimeOverlay = createRuntimeTimelineOverlay();
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
    visibleStartIndex = Math.min(entry.visibleStartIndex, Math.max(0, timeline.length - 1));
    userScrolledUp = entry.userScrolledUp;
    initialLoadTimedOut = false;
    mergeConversationList([entry.conversation]);
    requestAnimationFrame(() => {
      if (timelineEl) {
        programmaticScroll = true;
        timelineEl.scrollTop = entry.scrollTop;
        lastTimelineScrollTop = timelineEl.scrollTop;
        programmaticScroll = false;
      }
    });
    return entry;
  }

  function clearConversationViewState(): void {
    sessions = [];
    sessionIds.clear();
    timeline = [];
    runtimeOverlay = createRuntimeTimelineOverlay();
    visibleStartIndex = 0;
    activeSessionLastSeq = 0;
    olderMessagesCursor = null;
    hasOlderMessages = false;
    queuedCount = 0;
    queuedMessages = [];
    contextUsage = null;
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
    editingTitle = false;
    subSessionPanelOpen = false;
    userScrolledUp = false;
    lastTimelineScrollTop = 0;
  }

  function isLiveOnlyTimelineItem(item: TimelineItem): boolean {
    if (item.kind === 'message') {
      return item.optimistic === true || item.streaming === true;
    }
    if (item.kind === 'tool_call') {
      return !['completed', 'failed', 'cancelled'].includes(item.status);
    }
    if (item.kind === 'delegation') {
      return item.status === 'started' || item.status === 'running' || item.status === 'paused';
    }
    if (item.kind === 'thinking') {
      return item.streaming;
    }
    return false;
  }

  function mergeTimelineRefresh(refreshed: TimelineItem[], existing: TimelineItem[]): TimelineItem[] {
    if (existing.length === 0) return refreshed;
    const refreshedKeys = new Set(refreshed.map(timelineItemKey));
    const firstRefreshedExistingIndex = existing.findIndex((item) => refreshedKeys.has(timelineItemKey(item)));
    const preservedOlderItems = firstRefreshedExistingIndex > 0
      ? existing.slice(0, firstRefreshedExistingIndex).filter((item) => !refreshedKeys.has(timelineItemKey(item)))
      : [];
    const preservedLiveItems = existing.filter((item) => {
      if (!isLiveOnlyTimelineItem(item)) return false;
      return !refreshedKeys.has(timelineItemKey(item));
    });
    if (preservedOlderItems.length === 0 && preservedLiveItems.length === 0) return refreshed;
    const result = [...preservedOlderItems, ...refreshed];
    for (const item of preservedLiveItems) {
      const previousIndex = existing.findIndex((candidate) => candidate.id === item.id);
      const insertAfterKey = previousIndex > 0 ? timelineItemKey(existing[previousIndex - 1]) : null;
      const insertAfterIndex = insertAfterKey
        ? result.findIndex((candidate) => timelineItemKey(candidate) === insertAfterKey)
        : -1;
      if (insertAfterIndex >= 0) {
        result.splice(insertAfterIndex + 1, 0, item);
      } else {
        result.push(item);
      }
    }
    return result;
  }

  function prependOlderTimelinePage(existing: TimelineItem[], older: TimelineItem[]): TimelineItem[] {
    if (older.length === 0) return existing;
    const existingKeys = new Set(existing.map(timelineItemKey));
    const uniqueOlder = older.filter((item) => !existingKeys.has(timelineItemKey(item)));
    return uniqueOlder.length > 0 ? [...uniqueOlder, ...existing] : existing;
  }

  function messageHistoryOlderCursor(
    response: import('$lib/types/api').MessageHistoryResponse,
  ): string | null {
    const cursor = (response as unknown as { older_cursor?: unknown }).older_cursor;
    return typeof cursor === 'string' && cursor.length > 0 ? cursor : null;
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

  async function loadConversationPage(reset = false): Promise<void> {
    conversationListLoadCount += 1;
    conversationListLoading = true;
    const channelFilter = selectedChannel !== 'all' ? selectedChannel : null;
    const agentFilter = selectedAgentId !== 'all' ? selectedAgentId : null;
    try {
      const response = await api.conversations.list(reset ? null : conversationCursor, {
        contextType: channelFilter,
        agentId: agentFilter,
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
      channel: selectedChannel,
      agent: selectedAgentId,
      status: selectedConversationStatus,
    });
  }

  function currentSidebarProjectionFilter() {
    return {
      selectedChannel,
      selectedAgentId,
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

  function applySidebarProjection(projection: SidebarProjection): void {
    agents = projection.agents;
    agentDirectChats = projection.agent_direct_chats;
    applyAvailableChannelTypes(projection.context_types);
    mergeConversationList(projection.conversations.items, { reset: true });
    conversationCursor = projection.conversations.cursor;
    conversationsHasMore = projection.conversations.has_more;
  }

  function applyCachedSidebarProjection(key = sidebarProjectionCacheKey()): boolean {
    const cached = sidebarProjectionCache.get(key);
    if (!cached) return false;
    applySidebarProjection(cloneSidebarProjection(cached));
    return true;
  }

  function applyAvailableChannelTypes(contextTypes: string[]): void {
    const types = new Set(contextTypes.map((contextType) => contextType.toLowerCase()));
    if (selectedChannel && selectedChannel !== 'all') {
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
      contextType: selectedChannel !== 'all' ? selectedChannel : null,
      agentId: selectedAgentId !== 'all' ? selectedAgentId : null,
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
    } finally {
      conversationListLoadCount = Math.max(0, conversationListLoadCount - 1);
      conversationListLoading = conversationListLoadCount > 0;
    }
  }

  async function loadAgentDirectChats(): Promise<void> {
    if (selectedChannel !== 'all' && selectedChannel !== 'web') {
      agentDirectChats = [];
      return;
    }
    const agentFilter = selectedAgentId !== 'all' ? selectedAgentId : null;
    agentDirectChats = await api.conversations.agentDirect({
      agentId: agentFilter,
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
    const previous = selectedAgentId;
    if (typeof window === 'undefined') return false;
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
    if (stored && agents.some((a) => a.agent_id === stored && a.status === 'active')) {
      selectedAgentId = stored;
    } else {
      selectedAgentId = 'all';
    }
    return selectedAgentId !== previous;
  }

  function persistSelectedAgent(): void {
    if (typeof window === 'undefined' || !selectedAgentId) return;
    if (selectedAgentId === 'all') {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.selectedAgent);
      return;
    }
    window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedAgent, selectedAgentId);
  }

  function clearSelectedAgentFilter(): boolean {
    const hadFilter = selectedAgentId !== 'all'
      || (typeof window !== 'undefined' && window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent) !== null);
    selectedAgentId = 'all';
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
    const conversationId = currentConversation?.conversation_id ?? conversationIdFromRoute();
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
      return agentLabel(conversationAgent(conversation));
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
    backendConversationState = null;
    return conversationSwitchRequestId;
  }

  function resetConversationSwitch(): void {
    conversationSwitchRequestId = nextConversationLoadId(conversationSwitchRequestId);
    switchingConversationId = null;
  }

  function finishConversationSwitch(conversationId: string, switchRequestId: number): void {
    if (
      isCurrentConversationLoad(switchRequestId, conversationSwitchRequestId)
      && switchingConversationId === conversationId
    ) {
      switchingConversationId = null;
    }
  }

  function conversationAgent(conversation: Conversation): Agent | undefined {
    return agents.find((agent) => agent.agent_id === conversation.agent_id);
  }

  function messageAgent(item: MessageTimelineItem): Agent | null {
    const runtimeAgentId = item.runtime?.agent_id;
    if (runtimeAgentId) {
      return agents.find((agent) => agent.agent_id === runtimeAgentId) ?? null;
    }
    return currentConversation ? conversationAgent(currentConversation) ?? null : null;
  }

  async function refreshCurrentConversationMetadata(): Promise<void> {
    if (!currentConversation) return;
    const updated = await api.conversations.detail(currentConversation.conversation_id);
    currentConversation = updated;
    patchConversationInList(updated.conversation_id, updated);
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
          agent_id: selectedAgentId !== 'all' ? selectedAgentId : null,
          status: selectedConversationStatus,
          context_type: selectedChannel !== 'all' ? selectedChannel : null,
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
    const fullIndex = timeline.findIndex((item) => item.id === id);
    if (fullIndex >= 0 && fullIndex < visibleStartIndex) {
      visibleStartIndex = Math.max(0, fullIndex - 5);
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
    for (const item of timeline) {
      if (item.kind !== 'message' || !item.timestamp) continue;
      const value = Date.parse(item.timestamp);
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
    const local = findLocalChatMatches(timeline, q).map((match) => ({
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
        targetId: nearestMessageIdForTimestamp(match.match.ts) ?? `${match.intaris_session_id}:${match.match.ref_id ?? match.match.ts ?? match.match.snippet}`
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
    return agent?.display_name ?? agent?.name ?? 'Cognis';
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
    if (!conversation) return;
    const currentTypes = conversation.pending_notification_types ?? [];
    const nextTypes = present
      ? Array.from(new Set([...currentTypes, notificationType]))
      : currentTypes.filter((type) => type !== notificationType);
    patchConversationInList(conversationId, { pending_notification_types: nextTypes });
  }

  function refreshConversationPendingNotificationTypes(conversationId: string | null | undefined): void {
    if (!conversationId) return;
    void api.notifications
      .list(conversationId)
      .then((notifications) => {
        patchConversationInList(conversationId, {
          pending_notification_types: pendingNotificationTypesFromNotifications(notifications),
        });
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

  async function loadHistory(
    conversationId: string,
    limit = 200,
  ): Promise<import('$lib/types/api').TimelineProjectionResponse> {
    return loadConversationHistoryPage(conversationId, null, limit);
  }

  function projectedTimelineItems(
    response: import('$lib/types/api').TimelineProjectionResponse,
  ): TimelineItem[] {
    return timelineFromProjection(response.timeline_items);
  }

  async function refreshSidebarData(): Promise<void> {
    await loadSidebarProjection();
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
  async function refreshEscalations(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    try {
      const taskId = conversationTaskId(currentConversation);
      const notifications = taskId
        ? await api.notifications.list(null, { taskId })
        : await api.notifications.list(currentConversation.conversation_id);
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
    } catch (caughtError) {
      escalationError = asApiError(caughtError).message;
    }
  }

  async function refreshPendingDirectQuestion(): Promise<void> {
    if (!currentConversation || document.hidden) return;
    try {
      const notifications = await api.notifications.list(currentConversation.conversation_id);
      const pendingStepNotifications = notifications.filter(
        (item) => ['step_question', 'auth_challenge'].includes(item.notification_type) && item.status === 'pending',
      );
      pendingCredentialRequest = notifications.find(
        (item) => item.notification_type === 'credential_request' && item.status === 'pending',
      ) ?? null;
      // Annotate any pending input/challenge tool call with a
      // notification id so the send routing can resolve it without
      // needing another round-trip. Task-scoped notifications count
      // too — the UI uses the annotation purely for resolution, and
      // the backend rejects unauthorized resolves.
      for (const notif of pendingStepNotifications) {
        timeline = annotateStepRequestInputWithNotification(timeline, notif.notification_id);
      }
      const directQuestion = pendingStepNotifications
        .filter((item) => item.task_id === null)
        .map((item) => pendingDirectQuestionFromNotification(item))
        .find((item): item is PendingDirectQuestion => item !== null) ?? null;
      if (directQuestion?.notificationId !== pendingDirectQuestion?.notificationId) {
        directQuestionAnswers = {};
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
    } catch {
      // Ignore notification refresh failures here — they should not block chat.
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
      void refreshEscalations();
      void refreshPendingDirectQuestion();
    }, 5000);
  }

  function syncVisibleWindow(): void {
    visibleStartIndex = Math.max(0, timeline.length - 100);
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
  }

  function clearUserScrollIntentSoon(): void {
    window.setTimeout(() => {
      userScrollIntentUp = false;
    }, 120);
  }

  function scheduleScrollToBottom(force = false, frames = 2): void {
    if (frames <= 0) {
      scrollToBottom(force);
      return;
    }
    requestAnimationFrame(() => scheduleScrollToBottom(force, frames - 1));
  }

  function scrollToBottom(force = false): void {
    if (!timelineEl || (!force && userScrolledUp)) return;
    if (force) userScrolledUp = false;
    programmaticScroll = true;
    requestAnimationFrame(() => {
      if (timelineEl) {
        timelineEl.scrollTop = timelineEl.scrollHeight;
        lastTimelineScrollTop = timelineEl.scrollTop;
        if (force) userScrolledUp = false;
      }
      requestAnimationFrame(() => {
        programmaticScroll = false;
        if (force && timelineEl) {
          const distanceFromBottom = timelineDistanceFromBottom();
          userScrolledUp = !isNearScrollBottom(distanceFromBottom, CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX);
          lastTimelineScrollTop = timelineEl.scrollTop;
        }
      });
    });
  }

  function handleTimelineScroll(): void {
    if (!timelineEl || programmaticScroll) return;
    const currentScrollTop = timelineEl.scrollTop;
    const distanceFromBottom = timelineDistanceFromBottom();

    const nextState = nextChatScrollState({
      currentScrollTop,
      lastScrollTop: lastTimelineScrollTop,
      distanceFromBottom,
      userScrolledUp,
      userScrollIntentUp,
      bottomThresholdPx: CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX,
      scrollDeltaThresholdPx: CHAT_USER_SCROLL_DELTA_THRESHOLD_PX,
    });
    userScrolledUp = nextState.userScrolledUp;
    if (isNearScrollBottom(nextState.distanceFromBottom, CHAT_LIVE_TAIL_BOTTOM_THRESHOLD_PX)) {
      userScrollIntentUp = false;
      lastTimelineTouchY = null;
    }

    lastTimelineScrollTop = currentScrollTop;

    if (currentScrollTop <= 24 && (visibleStartIndex > 0 || hasOlderMessages)) {
      void loadOlder();
    }
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    userScrollIntentUp = false;
    lastTimelineTouchY = null;
    scrollToBottom(true);
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

  async function persistSelectedChannel(): Promise<void> {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedChannel, selectedChannel);
    resetConversationSearchResults();
    await Promise.all([loadAgentDirectChats(), loadConversationPage(true)]);
  }

  function restoreSelectedChannel(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedChannel);
    if (stored) selectedChannel = stored;
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
        context_usage: detail.context_usage ?? null
      };
      if (detail.context_usage) {
        contextUsage = detail.context_usage;
      }
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
      if (detail.context_usage) {
        contextUsage = detail.context_usage;
      }
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
        context_usage: detail.context_usage ?? null
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
      preserveTimelineOnHistoryFailure?: boolean;
      preserveScroll?: boolean;
      mergeTimeline?: boolean;
    } = {},
  ): Promise<void> {
    const reloadSessions = options.reloadSessions ?? true;
    const reloadHistory = options.reloadHistory ?? true;
    const shouldResubscribe = options.resubscribe ?? false;
    const preserveTimelineOnHistoryFailure = options.preserveTimelineOnHistoryFailure ?? false;
    const shouldMergeTimeline = options.mergeTimeline ?? false;
    const shouldPreserveScroll = options.preserveScroll === true && userScrolledUp && timelineEl !== null;
    const preservedScrollTop = shouldPreserveScroll ? timelineEl?.scrollTop ?? 0 : 0;
    const preservedVisibleStartIndex = visibleStartIndex;
    const initialLoadPolicy = conversationInitialLoadPolicy(currentConversation);

    const [sessionResult, historyResult] = await Promise.allSettled([
      reloadSessions ? api.conversations.sessions(conversationId, initialLoadPolicy.sessionOptions) : Promise.resolve(sessions),
      reloadHistory
        ? loadHistory(conversationId, initialLoadPolicy.historyLimit)
        : Promise.resolve({
            items: [],
            last_seq: 0,
            has_more: false,
            has_active_turn: false,
            active_streams: [],
            active_tool_outputs: [],
            active_session_id: null,
            active_session_last_seq: 0,
            history_truncated: false,
            truncation_reason: null,
            state_snapshot: null,
            timeline_items: []
          }),
    ]);

    if (isStaleConversationLoad(requestId)) {
      return;
    }

    sessionsError = '';
    historyError = '';

    const previousConversation = currentConversation;
    let nextSessions = reloadSessions ? [] : sessions;
    let nextActiveSessionId = previousConversation?.active_session_id ?? null;

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
      }
    }

    if (reloadHistory && historyResult.status === 'fulfilled') {
      nextActiveSessionId = historyResult.value.active_session_id ?? nextActiveSessionId;
      activeSessionLastSeq = historyResult.value.active_session_last_seq ?? activeSessionLastSeq;
      applyConversationStateSnapshot(historyResult.value.state_snapshot);
      olderMessagesCursor = messageHistoryOlderCursor(historyResult.value);
      hasOlderMessages = Boolean(historyResult.value.has_more && olderMessagesCursor);
      timeline = shouldMergeTimeline
        ? mergeTimelineRefresh(projectedTimelineItems(historyResult.value), timeline)
        : projectedTimelineItems(historyResult.value);
      runtimeOverlay = applyRuntimeSnapshotOverlay(createRuntimeTimelineOverlay(), {
        active_streams: historyResult.value.active_streams ?? [],
        active_tool_outputs: historyResult.value.active_tool_outputs ?? [],
        active_thinking: [],
        has_active_turn: historyResult.value.has_active_turn,
      });
      turnInProgress = historyResult.value.has_active_turn ?? hasActiveTurnTimelineItem();
      setConversationTurnIndicator(currentConversation?.conversation_id, turnInProgress);
      if (turnInProgress) {
        awaitingAssistantStart = false;
      }
      syncConversationActiveSession(nextActiveSessionId);
      if (shouldPreserveScroll) {
        visibleStartIndex = Math.min(preservedVisibleStartIndex, Math.max(0, timeline.length - 100));
      } else {
        syncVisibleWindow();
        userScrolledUp = false;
      }
      if (!headerInfoOpen) {
        void refreshSessionContextUsage();
      }
      if (currentConversation) {
        saveCurrentConversationView();
      }
    } else if (reloadHistory && historyResult.status === 'rejected') {
      if (!preserveTimelineOnHistoryFailure) {
        timeline = [];
        syncVisibleWindow();
      }
      const nextError = asApiError(historyResult.reason).message;
      if (!(isPreSessionChatConversation(previousConversation, nextSessions.length) && isMissingSessionError(nextError))) {
        historyError = nextError;
      }
    }
    if (reloadHistory) {
      cachedConversationRefreshing = false;
    }

    if (shouldResubscribe) {
      const subscribeSeq = reloadHistory && historyResult.status === 'fulfilled'
        ? (historyResult.value.active_session_last_seq ?? 0)
        : activeSessionLastSeq;
      activeSessionLastSeq = subscribeSeq;
      wsClient.subscribeConversation(
        conversationId,
        subscribeSeq,
        reloadHistory && historyResult.status === 'fulfilled'
          ? (historyResult.value.active_session_id ?? null)
          : nextActiveSessionId,
        { replaceCursor: reloadHistory && historyResult.status === 'fulfilled' },
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
      await Promise.all([refreshEscalations(), refreshPendingDirectQuestion()]);
      if (isStaleConversationLoad(requestId)) {
        return;
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
        if (shouldPreserveScroll && timelineEl) {
          timelineEl.scrollTop = preservedScrollTop;
          userScrolledUp = true;
          return;
        }
        scrollToBottom(true);
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
    const lastItem = timeline[timeline.length - 1];
    if (lastItem?.kind === 'system_message' && lastItem.text === text) {
      return;
    }
    timeline = applyWebSocketEvent(timeline, {
      type: 'system_message',
      text,
    });
    syncVisibleWindow();
  }

  function hasActiveTurnTimelineItem(): boolean {
    const lastItem = projectedTimeline[projectedTimeline.length - 1];
    return Boolean(lastItem && isInlineLiveTimelineItem(lastItem));
  }

  function hasAnyActiveTurnTimelineItem(): boolean {
    return projectedTimeline.some((item) => {
      if (item.kind === 'message') {
        return item.role === 'assistant' && item.streaming === true;
      }
      if (item.kind === 'thinking') {
        return item.streaming === true;
      }
      if (item.kind === 'tool_call') {
        return isActiveToolStatus(item.status);
      }
      if (item.kind === 'delegation') {
        return ['started', 'running', 'pending'].includes(item.status ?? '');
      }
      return false;
    });
  }

  function timelinePatchHasActiveWork(event: CognisWebSocketEvent): boolean {
    if (event.type !== 'timeline_patch') return false;
    return timelinePatchContainsActiveWork(
      event.items.filter((item) => timelinePatchItemBelongsToActiveSessionFromProjection(item))
    );
  }

  function isInlineLiveTimelineItem(item: TimelineItem): boolean {
    if (item.kind === 'message') {
      return item.role === 'assistant' && item.streaming === true;
    }
    if (item.kind === 'thinking') {
      return item.streaming === true;
    }
    if (item.kind === 'tool_call') {
      return isActiveToolStatus(item.status);
    }
    return false;
  }

  function timelinePatchItemBelongsToActiveSession(item: TimelineItem): boolean {
    if (item.kind !== 'message' && item.kind !== 'tool_call' && item.kind !== 'thinking') return true;
    const sessionId = item.sessionId ?? null;
    const activeSessionId = currentConversation?.active_session_id ?? null;
    return !sessionId || !activeSessionId || sessionId === activeSessionId;
  }

  function timelinePatchItemBelongsToActiveSessionFromProjection(item: import('$lib/types/api').TimelineProjectionItem): boolean {
    const kind = typeof item.kind === 'string' ? item.kind : '';
    if (kind !== 'message' && kind !== 'tool_call' && kind !== 'thinking') return true;
    const sessionId = typeof item.sessionId === 'string' ? item.sessionId : null;
    const activeSessionId = currentConversation?.active_session_id ?? null;
    return !sessionId || !activeSessionId || sessionId === activeSessionId;
  }

  async function reconcileActiveConversation(): Promise<void> {
    if (!currentConversation || document.hidden || initializing || switchingConversation) {
      return;
    }

    const requestId = beginConversationLoad();
    conversationSubloadsLoading = true;
    try {
      wsClient.connect();
      await reloadConversationSubloads(currentConversation.conversation_id, requestId, {
        reloadSessions: true,
        reloadHistory: true,
        resubscribe: true,
        preserveTimelineOnHistoryFailure: true,
        preserveScroll: true,
      });

      if (!isStaleConversationLoad(requestId) && (historyError || sessionsError)) {
        appendLocalSystemMessage('Chat refresh failed. History may be stale.');
      }
    } finally {
      if (!isStaleConversationLoad(requestId)) {
        conversationSubloadsLoading = false;
      }
    }
  }

  function scheduleForegroundReconcile(delayMs = 150): void {
    if (typeof window === 'undefined') return;
    if (foregroundSyncTimer !== null) {
      window.clearTimeout(foregroundSyncTimer);
    }
    foregroundSyncTimer = window.setTimeout(() => {
      foregroundSyncTimer = null;
      void reconcileActiveConversation();
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
      api.conversations.markRead(conversationId).catch(() => {});
      conversationSubloadsLoading = true;
      try {
        const conversation = await api.conversations.detail(conversationId);
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
          preserveTimelineOnHistoryFailure: true,
          preserveScroll: true,
          mergeTimeline: true,
        });
        if (isStaleConversationLoad(requestId)) {
          return;
        }
        patchConversationInList(conversationId, {
          has_unread: false,
          active_session_id: currentConversation?.active_session_id,
        });
      } catch (caughtError) {
        if (!isStaleConversationLoad(requestId)) {
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
      const conversation = await api.conversations.detail(conversationId);
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

      api.conversations.markRead(conversationId).catch(() => {});
      currentConversation = {
        ...(currentConversation ?? conversation),
        has_unread: false,
      };
      patchConversationInList(conversationId, {
        has_unread: false,
        active_session_id: currentConversation.active_session_id,
      });
    } catch (caughtError) {
      if (isStaleConversationLoad(requestId)) {
        return;
      }
      error = asApiError(caughtError).message;
      clearLastOpenedConversation(conversationId);
      currentConversation = null;
      sessions = [];
      timeline = [];
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
    if (selectedAgentId !== 'all' && primaryAgents.some((agent) => agent.agent_id === selectedAgentId)) {
      return selectedAgentId;
    }
    const currentAgentId = currentConversation?.agent_id ?? '';
    if (currentAgentId && primaryAgents.some((agent) => agent.agent_id === currentAgentId)) {
      return currentAgentId;
    }
    if (typeof window !== 'undefined') {
      const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
      if (stored && primaryAgents.some((agent) => agent.agent_id === stored)) {
        return stored;
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

  /** Slash commands that are handled as system actions, not chat messages. */
  const SYSTEM_SLASH_COMMANDS = ['/approve', '/deny', '/compact', '/summarize', '/fork', '/undo', '/redo', '/new', '/reset', '/clear', '/stop', '/cancel', '/context', '/info', '/lsp', '/executor', '/model', '/thinking', '/profile', '/help', '/retry', '/continue', '/plan', '/build', '/default'];

  function normalizeSlashCommandInput(value: string): string {
    const trimmed = value.trim();
    if (!trimmed.startsWith('/')) return trimmed;
    return `/${trimmed.slice(1).trimStart()}`;
  }

  function isSystemSlashCommand(value: string): boolean {
    const normalized = normalizeSlashCommandInput(value);
    return SYSTEM_SLASH_COMMANDS.some((command) => {
      if (['/plan', '/build', '/default'].includes(command)) {
        return normalized === command;
      }
      return normalized === command || normalized.startsWith(`${command} `);
    });
  }

  /** Slash command suggestions shown when user types /. */
  const SLASH_SUGGESTIONS = [
    { command: '/help', description: 'Show available commands' },
    { command: '/model', description: 'List or switch LLM model' },
    { command: '/thinking', description: 'Set reasoning effort' },
    { command: '/profile', description: 'List or switch agent runtime profile' },
    { command: '/context', description: 'Show context usage' },
    { command: '/info', description: 'Show session details' },
    { command: '/lsp', description: 'Show LSP diagnostics status' },
    { command: '/executor', description: 'Show or switch active executor' },
    { command: '/plan', description: 'Plan/read-only mode; add text for one-shot planning' },
    { command: '/build', description: 'Build/implementation mode; add text for one-shot build' },
    { command: '/default', description: 'Return to agent default mode; add text for one-shot default' },
    { command: '/compact', description: 'Compact conversation' },
    { command: '/summarize', description: 'Alias for /compact' },
    { command: '/fork', description: 'Fork conversation; add text to start the fork' },
    { command: '/undo', description: 'Undo the last user turn in this chat' },
    { command: '/redo', description: 'Redo the last undone turn in this chat' },
    { command: '/new', description: 'Start new conversation' },
    { command: '/reset', description: 'Alias for /new' },
    { command: '/clear', description: 'Alias for /new' },
    { command: '/stop', description: 'Stop current work' },
    { command: '/cancel', description: 'Alias for /stop' },
    { command: '/approve', description: 'Approve tool escalation' },
    { command: '/deny', description: 'Deny tool escalation' },
    { command: '/retry', description: 'Retry paused workflow gate' },
    { command: '/continue', description: 'Continue paused workflow gate' },
  ];

  let slashSuggestionsVisible = $state(false);
  let slashFilteredSuggestions = $state<typeof SLASH_SUGGESTIONS>([]);
  let slashSelectedIndex = $state(0);

  function updateSlashSuggestions(): void {
    const val = normalizeSlashCommandInput(composer);
    if (val.startsWith('/') && !val.includes(' ') && val.length < 20) {
      const filter = val.toLowerCase();
      slashFilteredSuggestions = SLASH_SUGGESTIONS.filter((s) => s.command.startsWith(filter));
      slashSuggestionsVisible = slashFilteredSuggestions.length > 0;
      slashSelectedIndex = 0;
    } else {
      slashSuggestionsVisible = false;
    }
  }

  function acceptSlashSuggestion(index: number): void {
    const suggestion = slashFilteredSuggestions[index];
    if (!suggestion) return;
    // Commands that take arguments get a trailing space
    const needsArg = ['/model', '/thinking', '/executor', '/approve', '/deny', '/retry', '/continue'].includes(suggestion.command);
    composer = needsArg ? suggestion.command + ' ' : suggestion.command;
    slashSuggestionsVisible = false;
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

  async function resolveDirectQuestionNotificationId(pendingStepTool: ReturnType<typeof findPendingStepRequestInputCall>): Promise<string> {
    let notificationId = pendingStepTool?.notificationId ?? pendingDirectQuestion?.notificationId ?? '';
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
    const pendingStepTool = findPendingStepRequestInputCall(timeline);
    const notificationId = await resolveDirectQuestionNotificationId(pendingStepTool);
    if (!notificationId) return false;

    directQuestionSubmitting = true;
    if (pendingStepTool) {
      timeline = optimisticallyResolveStepRequestInput(timeline, pendingStepTool.id, reply);
    }
    syncVisibleWindow();
    userScrolledUp = false;
    scrollToBottom();
    const stepName =
      typeof pendingStepTool?.arguments?.step_name === 'string'
        ? (pendingStepTool.arguments.step_name as string)
        : pendingDirectQuestion.stepName;
    haptic.success();
    wsClient.respondStepQuestion(notificationId, reply, stepName);
    return true;
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

    // Detect a `step_request_questions` tool call sitting in the timeline
    // waiting for a reply. This is the source-of-truth signal that the
    // agent loop is paused for user input. `pendingDirectQuestion` is a
    // mirror of the same state but can drift (stale fetch, missed WS
    // event, reload race) — the tool-call entry does not.
    const pendingStepTool = findPendingStepRequestInputCall(timeline);
    const isStepInputReply =
      !isSlashCommand && (pendingDirectQuestion !== null || pendingStepTool !== null);

    if (isStepInputReply && composerAttachments.length > 0) {
      addToast('Attachments are not supported for clarification responses.', 'error');
      return;
    }
    if (isStepInputReply && pendingDirectQuestion?.kind !== 'auth_challenge') {
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

    if (!isSlashCommand && !isStepInputReply && !willQueueBehindActiveTurn) {
      timeline = appendOptimisticUserMessage(timeline, content, composerAttachments, clientMessageId);
    }
    if (!isSlashCommand) {
      lastSubmittedMessage = content;
      lastRecoverableMessage = '';
      awaitingAssistantStart = true;
      turnInProgress = true;
      activeTurnChatMode = persistentChatMode;
      patchConversationInList(
        currentConversation.conversation_id,
        optimisticConversationTurnPatch(persistentChatMode),
        { touchLastMessageAt: true }
      );
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
    const attachments = [...composerAttachments];
    composerAttachments = [];

    if (isStepInputReply) {
      // Resolve the notification ID. Order of preference:
      //   1. The tool call's own annotation (authoritative if present).
      //   2. `pendingDirectQuestion.notificationId` from the WS event.
      //   3. A fresh fetch of pending input/challenge notifications.
      const notificationId = await resolveDirectQuestionNotificationId(pendingStepTool);

      if (notificationId) {
        directQuestionSubmitting = true;
        if (pendingStepTool) {
          // Show the user's answer inside the tool call block immediately
          // so the Resolution section stops saying "Waiting for user input"
          // even before the backend tool_result arrives.
          if (pendingDirectQuestion?.kind === 'auth_challenge') {
            timeline = optimisticallyResolveStepRequestInput(timeline, pendingStepTool.id, content);
          } else {
            timeline = optimisticallyResolveStepRequestInput(timeline, pendingStepTool.id, buildDirectQuestionReply(directQuestionItems(), content));
          }
        }
        syncVisibleWindow();
        userScrolledUp = false;
        scrollToBottom();
        const stepName =
          typeof pendingStepTool?.arguments?.step_name === 'string'
            ? (pendingStepTool.arguments.step_name as string)
            : pendingDirectQuestion?.stepName;
        haptic.success();
        if (pendingDirectQuestion?.kind === 'auth_challenge') {
          wsClient.respondAuthChallenge(notificationId, content, stepName);
        } else {
          const questions = directQuestionItems();
          const reply = buildDirectQuestionReply(questions, content);
          if (!directQuestionReplySatisfiesRequired(questions, reply)) {
            directQuestionSubmitting = false;
            addToast('Answer all required questions before sending.', 'error');
            return;
          }
          wsClient.respondStepQuestion(notificationId, reply, stepName);
        }
        return;
      }

      // Fall back to a regular message: we saw a pending step_request_questions
      // but could not resolve a notification_id. Replace the missing
      // optimistic bubble so the user still sees their message.
      timeline = appendOptimisticUserMessage(timeline, content, attachments);
    }

    syncVisibleWindow();
    userScrolledUp = false;
    scrollToBottom();
    haptic.success();
    wsClient.sendMessage(currentConversation.conversation_id, outboundContent, attachments, clientMessageId);
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

  async function retryLastTurn(): Promise<void> {
    if (!currentConversation || !lastSubmittedMessage) return;
    composer = lastSubmittedMessage;
    syncComposerHeight();
    await handleSend();
  }

  async function continueFailedTurn(): Promise<void> {
    if (!currentConversation || !canRetryFailedTurn) return;
    composer = 'Continue';
    syncComposerHeight();
    await handleSend();
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
    wsClient.resolveEscalation(callId, decision);
  }

  function escalationSecondsRemaining(esc: Escalation): number {
    const timeout = esc.timeout_seconds ?? escalationTimeoutSeconds;
    const elapsed = (Date.now() - (esc.received_at ?? Date.now())) / 1000;
    return Math.max(0, Math.ceil(timeout - elapsed));
  }

  async function loadOlder(): Promise<void> {
    if (!timelineEl || loadingOlderMessages) return;

    if (visibleStartIndex === 0 && (!hasOlderMessages || !olderMessagesCursor || !currentConversation)) {
      return;
    }

    const previousScrollHeight = timelineEl.scrollHeight;
    const previousScrollTop = timelineEl.scrollTop;
    loadingOlderMessages = true;

    try {
      if (visibleStartIndex > 0) {
        visibleStartIndex = Math.max(0, visibleStartIndex - 50);
        await tick();
      } else if (olderMessagesCursor && currentConversation) {
        const response = await loadConversationHistoryPage(
          currentConversation.conversation_id,
          olderMessagesCursor,
        );
        timeline = prependOlderTimelinePage(timeline, projectedTimelineItems(response));
        olderMessagesCursor = messageHistoryOlderCursor(response);
        hasOlderMessages = Boolean(response.has_more && olderMessagesCursor);
        visibleStartIndex = 0;
        saveCurrentConversationView();
        await tick();
      }
      requestAnimationFrame(() => {
        if (timelineEl) {
          timelineEl.scrollTop = previousScrollTop + (timelineEl.scrollHeight - previousScrollHeight);
          lastTimelineScrollTop = timelineEl.scrollTop;
        }
        loadingOlderMessages = false;
        programmaticScroll = false;
      });
    } catch (caughtError) {
      const message = asApiError(caughtError).message;
      addToast(`Unable to load older messages: ${message}`, 'error');
      loadingOlderMessages = false;
      programmaticScroll = false;
      if (timelineEl) {
        timelineEl.scrollTop = previousScrollTop;
        lastTimelineScrollTop = timelineEl.scrollTop;
      }
    }
  }

  async function loadConversationHistoryPage(
    conversationId: string,
    before: string | null = null,
    limit = 200,
  ): Promise<import('$lib/types/api').TimelineProjectionResponse> {
    const conversationsApi = api.conversations as typeof api.conversations & {
      timelinePage(
        conversationId: string,
        limit?: number,
        before?: string | null,
      ): Promise<import('$lib/types/api').TimelineProjectionResponse>;
    };
    return conversationsApi.timelinePage(conversationId, limit, before);
  }

  async function openCreatedConversation(conversationId: string): Promise<void> {
    let createdConversation: Conversation | null = null;

    try {
      createdConversation = await api.conversations.detail(conversationId);
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
      const createdConversation = await api.conversations.detail(conversationId);
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

  function handleSocketEvent(event: import('$lib/types/api').CognisWebSocketEvent): void {
    const currentId = conversationIdFromRoute();
    const eventSessionId = 'session_id' in event && typeof event.session_id === 'string' ? event.session_id : null;
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
        patchConversationInList(otherConvId, conversationStateConversationPatch(event.state));
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
          patchConversationInList(otherConvId, conversationStateConversationPatch(state));
        }
        return;
      }
      recordTodoSnapshotFromSocketEvent(event);
      if (event.type === 'conversation_updated' && event.conversation_id) {
        const patch = conversationUpdatedRowPatch(event);
        if (Object.keys(patch).length > 0) {
          patchConversationInList(event.conversation_id, patch, {
            touchUpdatedAt: typeof event.title === 'string' || typeof event.updated_at === 'string',
            touchLastMessageAt: typeof event.last_message_at === 'string',
          });
        }
      } else if (event.type === 'turn_started' || event.type === 'queued' || event.type === 'chunk' || event.type === 'assistant_stream_snapshot' || event.type === 'assistant_thinking_chunk' || event.type === 'assistant_thinking_block' || event.type === 'tool_call' || event.type === 'delegation_started') {
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
        rootSessionId: currentConversation?.active_session_id,
      })
    ) {
      return;
    }

    if (event.type === 'session_compaction_started') {
      timeline = applyWebSocketEvent(timeline, event);
      syncVisibleWindow();
      scrollToBottom();
      return;
    }

    if (event.type === 'session_compaction_finished') {
      timeline = applyWebSocketEvent(timeline, event);
      syncVisibleWindow();
      return;
    }

    if (currentConversation) {
      if (event.type === 'message_complete') {
        patchConversationInList(
          currentConversation.conversation_id,
          { has_unread: false, last_message_at: event.completed_at ?? undefined },
          { touchLastMessageAt: true }
        );
      } else if (
        event.type === 'chunk'
        || event.type === 'tool_call'
        || event.type === 'tool_result'
        || event.type === 'delegation_started'
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

    if (event.type === 'queued_messages_updated' || event.type === 'queued' || event.type === 'message_complete' || event.type === 'turn_settled') {
      queuedCount = event.queued_count ?? 0;
      if ('messages' in event && Array.isArray(event.messages)) {
        applyQueuedMessageSnapshot(event.messages, event.queued_count ?? event.messages.length);
      } else if (queuedCount === 0) {
        queuedMessages = [];
      }
    }

    if (event.type === 'turn_started' || event.type === 'queued') {
      turnInProgress = true;
      if (event.type === 'turn_started') {
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
      applyConversationRuntimeSnapshot(event);
      return;
    }

    if (timelinePatchHasActiveWork(event) || event.type === 'chunk' || event.type === 'assistant_stream_snapshot' || event.type === 'assistant_thinking_chunk' || event.type === 'assistant_thinking_block' || event.type === 'tool_call' || event.type === 'tool_progress' || event.type === 'tool_result_chunk' || event.type === 'tool_output_chunk' || event.type === 'delegation_started') {
      awaitingAssistantStart = false;
      turnInProgress = true;
      setConversationTurnIndicator(currentConversation?.conversation_id, true);
    }

    recordTodoSnapshotFromSocketEvent(event);

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

    if (event.type === 'turn_settled' || event.type === 'task_paused') {
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
      awaitingAssistantStart = false;
      turnInProgress = false;
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
      // Update context usage from message_complete
      if (event.type === 'message_complete' && event.context_usage) {
        contextUsage = event.context_usage;
      }
      if (currentConversation && event.type === 'message_complete') {
        const agentObj = agents.find((a) => a.agent_id === currentConversation?.agent_id);
        const agentLabel = agentObj?.display_name ?? agentObj?.name ?? 'Cognis';
        const convTitle = currentConversation.title ?? 'Conversation';
        notifyIfHidden(agentLabel, `New message in "${convTitle}"`, currentConversation.conversation_id, currentConversation.conversation_id);
      }
      // Mark as read since the user is viewing this conversation
      if (currentConversation && !document.hidden) {
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
      timeline = applyWebSocketEvent(timeline, event);
      syncVisibleWindow();
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
        scrollToBottom();
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
        if (typeof event.has_active_turn === 'boolean') {
          turnInProgress = event.has_active_turn;
        }
        if (Object.keys(patch).length > 0) {
          patchConversationInList(currentConversation.conversation_id, patch, {
            touchUpdatedAt: typeof event.title === 'string' || typeof event.updated_at === 'string',
            touchLastMessageAt: typeof event.last_message_at === 'string',
          });
        }
      }
      return;
    }

    // Handle session_compacted: add to timeline and refresh sessions
    if (event.type === 'session_compacted') {
      syncConversationActiveSession(event.session_id);
      timeline = applyWebSocketEvent(timeline, event);
      syncVisibleWindow();
      scrollToBottom();
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
      awaitingAssistantStart = false;
      turnInProgress = false;
      activeSessionLastSeq = 0;
      olderMessagesCursor = null;
      hasOlderMessages = false;
      setConversationTurnIndicator(currentConversation?.conversation_id, false);
      runtimeOverlay = createRuntimeTimelineOverlay();
      timeline = applyWebSocketEvent([], {
        type: 'system_message',
        conversation_id: event.conversation_id,
        text: 'Started a new session.',
      });
      syncVisibleWindow();
      scrollToBottom(true);
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
      runtimeOverlay = createRuntimeTimelineOverlay();
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
          preserveTimelineOnHistoryFailure: true,
        });
      }
      return;
    }

    if (event.type === 'reconnected') {
      const previousTurnInProgress = turnInProgress;
      awaitingAssistantStart = false;
      turnInProgress = event.has_active_turn ?? hasAnyActiveTurnTimelineItem();
      setConversationTurnIndicator(currentConversation?.conversation_id, turnInProgress);
      if (
        currentConversation &&
        shouldReconcileAfterReconnect({
          remoteLastSeq: event.last_seq,
          activeSessionLastSeq,
          remoteHasActiveTurn: event.has_active_turn,
          localTurnInProgress: previousTurnInProgress,
        })
      ) {
        const requestId = beginConversationLoad();
        void reloadConversationSubloads(currentConversation.conversation_id, requestId, {
          reloadSessions: true,
          reloadHistory: true,
          resubscribe: true,
          preserveTimelineOnHistoryFailure: true,
          preserveScroll: true,
          mergeTimeline: true,
        });
      }
      return;
    }

    if (event.type === 'workflow_step_question' && event.notification_id) {
      // Annotate the matching step_request_questions tool call so the user's
      // next reply can be routed to `respondStepQuestion` even if this
      // banner-level pendingDirectQuestion state gets cleared (reload,
      // compaction, or a message_complete arriving during submission).
      timeline = annotateStepRequestInputWithNotification(timeline, event.notification_id);
      if (!event.task_id) {
        pendingDirectQuestion = pendingDirectQuestionFromQuestionSet(
          event.notification_id,
          event.step_name,
          event.questions,
          event.context,
        );
        directQuestionAnswers = {};
        directQuestionPageIndex = 0;
        directQuestionCollapsed = false;
        directQuestionSubmitting = false;
        awaitingAssistantStart = false;
        turnInProgress = false;
        setConversationTurnIndicator(currentConversation?.conversation_id, false);
      }
    }

    if (event.type === 'auth_challenge' && event.notification_id) {
      timeline = annotateStepRequestInputWithNotification(timeline, event.notification_id);
      if (!event.task_id) {
        pendingDirectQuestion = pendingDirectQuestionFromAuthChallengeEvent(event);
        directQuestionPageIndex = 0;
        directQuestionCollapsed = false;
        directQuestionSubmitting = false;
        awaitingAssistantStart = false;
        turnInProgress = false;
        setConversationTurnIndicator(currentConversation?.conversation_id, false);
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

    if (isRuntimeTimelineEvent(event)) {
      runtimeOverlay = applyRuntimeTimelineEvent(runtimeOverlay, event);
      const runtimeEventSeq = (event as { seq?: unknown; last_seq?: unknown }).seq
        ?? (event as { last_seq?: unknown }).last_seq;
      if (typeof runtimeEventSeq === 'number') {
        activeSessionLastSeq = Math.max(activeSessionLastSeq, runtimeEventSeq);
      }
      if (event.type !== 'tool_progress' && event.type !== 'tool_result_chunk' && event.type !== 'tool_output_chunk') {
        syncVisibleWindow();
      }
      if (
        event.type === 'chunk'
        || event.type === 'assistant_stream_snapshot'
        || event.type === 'assistant_thinking_chunk'
        || event.type === 'assistant_thinking_block'
        || event.type === 'tool_progress'
        || event.type === 'tool_result_chunk'
        || event.type === 'tool_output_chunk'
      ) {
        scrollToBottom();
      }
      return;
    }

    const canonicalEvent = hydrateMessageCompleteFromRuntimeOverlay(event, runtimeOverlay);
    const beforePromotion = timeline;
    timeline = promoteRuntimeOverlayForCanonicalEvent(timeline, runtimeOverlay, canonicalEvent);
    const messageCompletionPromoted = canonicalEvent.type === 'message_complete' && timeline !== beforePromotion;
    runtimeOverlay = clearRuntimeOverlayForCanonicalEvent(runtimeOverlay, canonicalEvent);
    if (!messageCompletionPromoted) {
      timeline = canonicalEvent.type === 'timeline_patch'
        ? applyTimelinePatch(timeline, canonicalEvent.items, { includeItem: timelinePatchItemBelongsToActiveSession })
        : applyWebSocketEvent(timeline, canonicalEvent);
    }
    const eventSeq = (event as { seq?: unknown; last_seq?: unknown }).seq
      ?? (event as { last_seq?: unknown }).last_seq;
    if (typeof eventSeq === 'number') {
      activeSessionLastSeq = Math.max(activeSessionLastSeq, eventSeq);
    }
    if (
      hasOlderMessages
      && currentConversation
      && event.type === 'message_complete'
      && event.session_id
      && event.session_id !== currentConversation.active_session_id
    ) {
      olderMessagesCursor = null;
      hasOlderMessages = false;
    }
    saveCurrentConversationView();
    // Skip syncVisibleWindow for high-frequency streaming events to avoid
    // triggering a full virtual-scroll recalculation on every delta.
    if (
      event.type !== 'tool_call' &&
      event.type !== 'tool_progress' &&
      event.type !== 'tool_result' &&
      event.type !== 'tool_result_chunk' &&
      event.type !== 'tool_output_chunk' &&
      event.type !== 'assistant_thinking_chunk'
    ) {
      syncVisibleWindow();
    }

    // Auto-scroll on new content
    if (event.type === 'chunk' || event.type === 'assistant_stream_snapshot' || event.type === 'assistant_thinking_chunk' || event.type === 'assistant_thinking_block' || event.type === 'tool_progress' || event.type === 'tool_result_chunk' || event.type === 'tool_output_chunk' || event.type === 'message_complete' || event.type === 'delegation_started' || event.type === 'delegation_completed' || event.type === 'system_message' || event.type === 'user_message') {
      scrollToBottom();
    }

    // No longer polling for escalations — they arrive via push events
  }

  async function handleAgentFilterChange(next?: string): Promise<void> {
    if (typeof next === 'string') {
      selectedAgentId = next;
    }
    persistSelectedAgent();
    resetConversationSearchResults();
    await refreshAvailableChannelTypes();
    await Promise.all([loadAgentDirectChats(), loadConversationPage(true)]);
  }

  let subSessionPollTimer: number | null = null;

  async function handleViewSession(sessionId: string): Promise<void> {
    if (!currentConversation) return;
    subSessionId = sessionId;
    subSessionPanelOpen = true;
    subSessionLoading = true;
    subSessionError = '';
    subSessionTimeline = [];
    subSessionLog = null;
    subSessionPollDelayMs = SESSION_LOG_POLL_INTERVAL_MS;
    subSessionUserScrolledUp = false;
    subSessionInfo = null;
    subSessionInfoOpen = false;
    try {
      const conversationId = currentConversation.conversation_id;
      const result = await loadSessionLog(
        sessionId,
        (afterSeq, limit) => api.conversations.sessionEvents(conversationId, sessionId, afterSeq, limit),
      );
      subSessionLog = result;
      subSessionTimeline = result.timeline;
    } catch (err) {
      subSessionError = asApiError(err)?.message ?? 'Failed to load session events';
    } finally {
      subSessionLoading = false;
    }
    startSubSessionPolling();
  }

  function startSubSessionPolling(): void {
    stopSubSessionPolling();
    const tick = async (): Promise<void> => {
      try {
        if (document.hidden || !subSessionPanelOpen || !subSessionId || !currentConversation) {
          return;
        }
        if (!subSessionLog) return;
        const conversationId = currentConversation.conversation_id;
        const result = await refreshSessionLog(
          subSessionLog,
          (afterSeq, limit) => api.conversations.sessionEvents(conversationId, subSessionId, afterSeq, limit),
        );
        subSessionLog = result;
        subSessionTimeline = result.timeline;
        subSessionPollDelayMs = SESSION_LOG_POLL_INTERVAL_MS;
      } catch {
        subSessionPollDelayMs = nextPollDelayMs(subSessionPollDelayMs);
      } finally {
        if (subSessionPanelOpen) {
          subSessionPollTimer = window.setTimeout(() => {
            void tick();
          }, subSessionPollDelayMs);
        }
      }
    };
    subSessionPollTimer = window.setTimeout(() => {
      void tick();
    }, subSessionPollDelayMs);
  }

  function stopSubSessionPolling(): void {
    if (subSessionPollTimer !== null) {
      window.clearTimeout(subSessionPollTimer);
      subSessionPollTimer = null;
    }
  }

  function closeSubSessionPanel(): void {
    stopSubSessionPolling();
    subSessionClosing = true;
    setTimeout(() => {
      subSessionPanelOpen = false;
      subSessionClosing = false;
      subSessionId = '';
      subSessionTimeline = [];
      subSessionLog = null;
      subSessionUserScrolledUp = false;
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
    if (shouldShowChatTodoDrawer) {
      chatTodoDrawerOpen = true;
    }
  });

  $effect(() => {
    currentConversation?.conversation_id;
    conversationModeOpen;
    syncServiceWorkerActiveConversation();
  });

  $effect(() => {
    if ((!timelineContentEl && !footerChromeEl) || typeof ResizeObserver === 'undefined') {
      return;
    }
    const observer = new ResizeObserver(() => {
      scheduleScrollToBottom(false);
    });
    if (timelineContentEl) {
      observer.observe(timelineContentEl);
    }
    if (footerChromeEl) {
      observer.observe(footerChromeEl);
    }
    return () => observer.disconnect();
  });

  $effect(() => {
    const latestTodos = latestTodoSnapshot(timeline, currentConversation?.context?.type === 'web');
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
    return sortConversationsByActivity(list);
  });
  let visibleConversationSections = $derived.by(() => groupConversationsByActivity(visibleConversationList, historySectionNow));

  let visibleAgentDirectChats = $derived.by(() => {
    return sortAgentDirectChats(agentDirectChats);
  });
  let projectedTimeline = $derived.by(() => projectDisplayTimeline(timeline, runtimeOverlay));
  let displayedTimeline = $derived(projectedTimeline.slice(visibleStartIndex));
  let liveTailTimelineItemKey = $derived.by(() => {
    const lastItem = projectedTimeline[projectedTimeline.length - 1];
    return lastItem && isInlineLiveTimelineItem(lastItem) ? timelineItemKey(lastItem) : null;
  });
  const canRetryFailedTurn = $derived.by(() =>
    Boolean(currentConversation)
      && !turnInProgress
      && !awaitingAssistantStart
      && !pendingDirectQuestion
      && !directQuestionSubmitting
      && !isReadOnly(currentConversation)
      && hasRetryableFailedTurnTail(displayedTimeline)
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
    restoreEnterToSendPreference();
    restoreSelectedChannel();
    restoreChatSidebarState();
    mobileListOpen = !conversationIdFromRoute();
    unsubscribeWs = wsClient.subscribe(handleSocketEvent);
    unsubscribeComposerFocus = onChatComposerFocusRequest(() => {
      composerElement?.focus();
    });
    unsubscribeCancelTurn = onCancelActiveTurnRequest(() => {
      if (currentConversation && turnInProgress) {
        wsClient.cancelTurn(currentConversation.conversation_id);
      }
    });
    visibilityHandler = () => {
      syncServiceWorkerActiveConversation();
      if (!document.hidden) {
        scheduleForegroundReconcile();
        void refreshEscalations();
        void refreshPendingDirectQuestion();
      }
    };
    focusHandler = () => {
      syncServiceWorkerActiveConversation();
      scheduleForegroundReconcile();
    };
    pageShowHandler = () => {
      scheduleForegroundReconcile();
    };
    onlineHandler = () => {
      scheduleForegroundReconcile();
    };
    document.addEventListener('visibilitychange', visibilityHandler);
    window.addEventListener('focus', focusHandler);
    window.addEventListener('pageshow', pageShowHandler);
    window.addEventListener('online', onlineHandler);
    startNotificationRefreshPolling();
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
      if (activeConversationId) {
        wsClient.unsubscribeConversation(activeConversationId);
      }
      syncServiceWorkerActiveConversation(false);
      stopSubSessionPolling();
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
            <Button aria-expanded={conversationFiltersOpen} aria-label="Toggle filters" size="sm" variant="secondary" onclick={() => (conversationFiltersOpen = !conversationFiltersOpen)}>
              {#if conversationFiltersOpen}
                <ChevronUp class="h-4 w-4" />
              {:else}
                <ChevronDown class="h-4 w-4" />
              {/if}
            </Button>
            <Button aria-label="Close conversation list" class="lg:hidden" size="sm" variant="secondary" onclick={closeMobileList}>Close</Button>
          </div>
        </div>

        {#if agents.length === 0}
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
          <div class={`space-y-3 ${conversationFiltersOpen ? 'block' : 'hidden'}`}>
          <AgentSelect
            label="Agent"
            agents={agents.filter((a) => a.status === 'active' && a.agent_type === 'primary')}
            value={selectedAgentId}
            onchange={(next) => void handleAgentFilterChange(next)}
            allowAll
            allLabel="All agents"
          />

          <label class="block space-y-1">
            <span class="text-xs font-medium uppercase tracking-widest text-slate-500">Channel</span>
            <select
              bind:value={selectedChannel}
              onchange={persistSelectedChannel}
              class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
            >
              <option value="all">All channels</option>
              {#each channelTypes() as ch}
                <option value={ch}>{channelDisplayLabel(ch)}</option>
              {/each}
            </select>
          </label>
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
                <p id={`history-section-${section.key}`} class="sticky top-0 z-10 rounded-lg bg-slate-950/90 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500 backdrop-blur first:mt-0">{section.label}</p>
                {#each section.conversations as conversation (conversation.conversation_id)}
                  {@const agent = conversationAgent(conversation)}
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
                {@const agent = conversationAgent(currentConversation)}
                {#if agent}
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

                {@const backingSessions = backingSessionCount()}
                {#if backingSessions > 1}
                  <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400" title="Backing sessions">
                    {backingSessions} backing sessions
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
            <button
              class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-400 transition hover:bg-slate-800 hover:text-slate-100 sm:h-8 sm:w-8"
              onclick={toggleHeaderInfo}
              type="button"
              title="Session details"
              aria-label="Toggle session details"
              aria-expanded={headerInfoOpen}
            >
              <Info class="h-4 w-4" />
            </button>
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
        {@const panelAgent = conversationAgent(currentConversation)}
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
          <div class={`mb-3 flex-wrap items-center gap-2 text-sm text-slate-300 sm:hidden ${headerInfoMode === 'context' ? 'hidden' : 'flex'}`}>
            {#if panelAgent}
              <div class="flex items-center gap-2 rounded-lg bg-slate-900/80 px-2 py-1">
                <AgentAvatar name={panelAgent.display_name ?? panelAgent.name} avatarUrl={panelAgent.avatar_url ?? null} class="h-5 w-5" />
                <span>{panelAgent.display_name ?? panelAgent.name}</span>
              </div>
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
            {#if sessions.length > 1}
              <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400">{sessions.length} sessions</span>
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
            {@const panelContextUsage = sessionInfo.context_usage ?? contextUsage}
            {@const narrativeText = sessionNarrativeText(sessionInfo)}
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
                  {@const isEditing = canMutateQueue && queueEditingId === queued.queue_id}
                  <div class="rounded-xl border border-sky-300/20 bg-slate-950/40 px-2.5 py-2">
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
                    {#if isExpanded || isEditing}
                      <div class="mt-2 rounded-lg border border-slate-800/70 bg-slate-950/70 p-2.5">
                        {#if isEditing}
                          <textarea class="max-h-[36vh] min-h-24 w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100" bind:value={queueEditContent} rows="4"></textarea>
                          <div class="mt-2 flex flex-wrap justify-end gap-2">
                            <Button size="sm" variant="secondary" disabled={queueBusyId === queued.queue_id || !queueEditContent.trim()} onclick={() => void saveQueuedMessage(queued.queue_id)}>Save</Button>
                            <Button size="sm" variant="ghost" disabled={queueBusyId === queued.queue_id} onclick={cancelQueuedMessageEdit}>Cancel</Button>
                          </div>
                        {:else}
                          <p class="max-h-[32vh] overflow-auto whitespace-pre-wrap break-words text-slate-100">{queued.content}</p>
                        {/if}
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
                {#if lastRecoverableMessage}
                  <Button size="sm" variant="secondary" onclick={retryLastTurn}>Retry</Button>
                {/if}
                <button
                  aria-label="Dismiss error"
                  class="inline-flex h-8 w-8 items-center justify-center rounded-lg text-rose-100 transition hover:bg-rose-500/20"
                  onclick={() => { error = ''; lastRecoverableMessage = ''; }}
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

        {#if sessionsError && !isPreSessionConversation}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-3 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Session details are temporarily unavailable: {sessionsError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry</Button>
            </div>
          </div>
        {/if}

        {#if historyError && !isPreSessionConversation}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Conversation history is temporarily unavailable: {historyError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry history</Button>
            </div>
          </div>
        {/if}

        {#if escalationError}
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
          items={displayedTimeline}
          agent={currentConversation ? conversationAgent(currentConversation) ?? null : null}
          bind:viewportElement={timelineEl}
          bind:contentElement={timelineContentEl}
          bind:userScrolledUp
          class="relative min-h-0 flex-1 overflow-y-auto overscroll-contain px-2.5 py-1.5 sm:p-4"
          contentClass="space-y-3"
          onScroll={handleTimelineScroll}
          onWheel={handleTimelineWheel}
          onTouchStart={handleTimelineTouchStart}
          onTouchMove={handleTimelineTouchMove}
          onTouchEnd={handleTimelineTouchEnd}
          onKeydown={handleTimelineKeydown}
          onPointerDown={closeHeaderInfo}
          onViewSession={handleViewSession}
          followPausedLabel="Scroll to bottom"
        >
            {#if hasOlderMessages && visibleStartIndex === 0 && !loadingOlderMessages}
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
              <TimelineList
                items={displayedTimeline}
                agent={currentConversation ? conversationAgent(currentConversation) ?? null : null}
                searchQuery={chatSearchQuery}
                searchMatchedIds={chatSearchOpen ? chatSearchMatchedMessageIds : emptySearchMatchedIds}
                searchSelectedId={selectedChatSearchTargetId}
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

            {#if showTurnProgress}
              <div class="flex items-center gap-3 px-2 py-2">
                <LiveDots tone={activeTurnChatMode === 'plan' ? 'emerald' : activeTurnChatMode === 'build' ? 'amber' : 'sky'} />
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
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div class="min-w-0 flex-1">
                  <p class="font-medium">Agent work</p>
                  <p class="mt-1 text-sky-100/80">
                    Read-only target conversation · state {managedConversationState(currentConversation)} · turn {managedTurnState(currentConversation)}
                  </p>
                  {#if currentConversation.managed_agent?.controller_conversation_id}
                    <a
                      class="mt-2 inline-flex items-center gap-1 text-xs font-medium text-sky-100 underline-offset-4 hover:underline"
                      href={conversationUrl(currentConversation.managed_agent.controller_conversation_id)}
                    >
                      Open controlling conversation
                      <ExternalLink class="h-3 w-3" />
                    </a>
                  {/if}
                  {#if currentConversation.managed_agent?.last_error && managedTurnState(currentConversation) !== 'running' && managedTurnState(currentConversation) !== 'queued'}
                    <p class="mt-2 break-words text-xs text-rose-100">Last error: {currentConversation.managed_agent.last_error}</p>
                  {/if}
                </div>
              </div>
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
                    <span class="font-mono font-medium text-sky-400">{suggestion.command}</span>
                    <span class="opacity-70">{suggestion.description}</span>
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
                  class="relative inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-sky-500 text-slate-950 transition hover:bg-sky-400 disabled:opacity-50"
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
                  class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-800 text-slate-200 transition hover:bg-slate-700"
                  onclick={() => { haptic.warning(); currentConversation && wsClient.cancelTurn(currentConversation.conversation_id); }}
                >
                  <Square class="h-3 w-3 fill-current" />
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
          class={`absolute inset-0 z-20 bg-slate-950/50 backdrop-blur-sm transition-opacity duration-250 ${subSessionClosing ? 'opacity-0' : 'opacity-100'}`}
          onclick={closeSubSessionPanel}
          type="button"
          aria-label="Close sub-session"
        ></button>

        <!-- Drawer -->
        <aside class={`absolute inset-0 z-30 flex h-full w-full flex-col border-l border-slate-800/80 bg-slate-900/95 shadow-2xl backdrop-blur ${subSessionClosing ? 'animate-slide-out-right' : 'animate-slide-in-right'}`}>
          <div class="flex items-center gap-3 border-b border-slate-800/80 px-4 pb-3 pt-[calc(0.75rem+env(safe-area-inset-top))]">
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
            <button
              class="flex items-center gap-1 text-xs text-slate-500 transition hover:text-sky-300"
              onclick={() => { subSessionInfoOpen = !subSessionInfoOpen; if (subSessionInfoOpen && !subSessionInfo) void loadSubSessionInfo(); }}
              type="button"
              title="Session details"
            >
              <Info class="h-3.5 w-3.5" />
            </button>
          </div>

          <!-- Sub-session info popover -->
          {#if subSessionInfoOpen}
            <div class="border-b border-slate-800/60 px-4 py-3 text-sm">
              {#if subSessionInfoLoading}
                <p class="text-xs text-slate-500">Loading session details...</p>
              {:else if subSessionInfo}
                {@const subIntention = extractIntentionDisplay(subSessionInfo.intention)}
                {#if subSessionInfo.summary || subIntention.title || subIntention.intention}
                  <div class="mb-2">
                    <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">{subSessionInfo.summary ? 'Summary' : 'Intention'}</p>
                    {#if !subSessionInfo.summary && subIntention.title}
                      <p class="mt-0.5 text-sm font-medium text-white">{subIntention.title}</p>
                    {/if}
                    {#if subSessionInfo.summary}
                      <p class="mt-0.5 whitespace-pre-wrap text-sm text-slate-200">{subSessionInfo.summary}</p>
                    {:else if subIntention.intention}
                      <p class="mt-0.5 whitespace-pre-wrap text-sm text-slate-200">{subIntention.intention}</p>
                    {/if}
                  </div>
                {/if}
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div class="flex flex-wrap gap-3 text-xs text-slate-400">
                    <span>Status: <span class="text-slate-200">{subSessionInfo.status}</span></span>
                    <span>Calls: <span class="text-slate-200">{subSessionInfo.total_calls}</span></span>
                    <span class="text-emerald-400">{subSessionInfo.approved_count} approved</span>
                    <span class="text-rose-400">{subSessionInfo.denied_count} denied</span>
                    <span class="text-sky-400">{subSessionInfo.escalated_count} escalated</span>
                  </div>
                  <Button size="sm" variant="secondary" onclick={() => void openIntarisSession(subSessionInfo?.intaris_session_id ?? '')}>Open in Intaris</Button>
                </div>
              {:else}
                <p class="text-xs text-slate-500">Unable to load session details.</p>
              {/if}
            </div>
          {/if}

          <TimelineViewport
            items={subSessionTimeline}
            agent={subSessionAgent() ?? null}
            loading={subSessionLoading}
            error={subSessionError}
            emptyLabel="No events recorded yet."
            live={!subSessionLoading && !subSessionError}
            bind:viewportElement={subSessionTimelineEl}
            bind:contentElement={subSessionTimelineContentEl}
            bind:userScrolledUp={subSessionUserScrolledUp}
            onViewSession={handleViewSession}
          />
        </aside>
      {/if}
    </section>
  </div>
{/if}

<ConversationMode
  open={conversationModeOpen}
  conversationId={currentConversation?.conversation_id ?? ''}
  agent={currentConversation ? (conversationAgent(currentConversation) ?? null) : null}
  onclose={() => { conversationModeOpen = false; }}
  sendEnableTts={(voice) => wsClient.enableTts(voice)}
  sendDisableTts={() => wsClient.disableTts()}
  submitText={(text) => {
    if (!currentConversation) return;
    wsClient.sendMessage(currentConversation.conversation_id, text, []);
  }}
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
