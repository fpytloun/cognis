<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { fade } from 'svelte/transition';
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
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
import Info from 'lucide-svelte/icons/info';
import Menu from 'lucide-svelte/icons/menu';
import Search from 'lucide-svelte/icons/search';
import X from 'lucide-svelte/icons/x';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AgentProfilePopover from '$lib/components/AgentProfilePopover.svelte';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import CompactionCard from '$lib/components/CompactionCard.svelte';
  import ThinkingBlock from '$lib/components/ThinkingBlock.svelte';
  import ComposerAttachments from '$lib/components/ComposerAttachments.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import WorkflowComposedCard from '$lib/components/WorkflowComposedCard.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import NewChatModal from '$lib/components/NewChatModal.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { api, asApiError } from '$lib/api/client';
  import {
    getConversationRetryScope,
    getNextHistoryAfterSeq,
    isMissingSessionError,
    isCurrentConversationLoad,
    isPreSessionChatConversation,
    shouldAdoptConversationSessionId,
    shouldSuppressPreSessionSocketError,
    nextConversationLoadId,
    nextPollDelayMs,
    CHAT_STORAGE_KEYS,
    SESSION_LOG_BOOTSTRAP_MAX_PAGES,
    SESSION_LOG_PAGE_SIZE,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { edgeSwipe } from '$lib/actions/edgeSwipe';
  import { scrollPersist } from '$lib/actions/scrollPersist';
  import { confirmAction } from '$lib/stores/confirm';
  import { mobileNavOpen as mobileNavOpenStore, requestOpenMobileNav } from '$lib/stores/mobileNav';
  import { registerOverlay } from '$lib/stores/overlays';
  import { onTabReset } from '$lib/stores/tabReset';
  import { addToast } from '$lib/stores/toasts';
  import { onCancelActiveTurnRequest, onChatComposerFocusRequest } from '$lib/shortcuts';
  import { isSupported as notificationsSupported, isGranted as notificationsGranted, requestPermission, notifyIfHidden, hasAskedPermission } from '$lib/notifications';
  import { buildLinkedServiceUrl, openUrlInNewTab } from '$lib/config';
  import { workspaceHealth } from '$lib/system';
  import {
    annotateStepRequestInputWithNotification,
    appendOptimisticUserMessage,
    findPendingStepRequestInputCall,
    optimisticallyResolveStepRequestInput,
    applyWebSocketEvent,
    normalizeHistory,
    type ThinkingTimelineItem,
    type TimelineItem,
    type ToolCallTimelineItem
  } from '$lib/chat';
  import type { Agent, AttachmentRef, ContextUsage, Conversation, Escalation, MessageEvent, Notification, Session } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';

  let initializing = $state(true);
  let initialLoadTimedOut = $state(false);
  let switchingConversation = $state(false);
  let initialConversationResolved = $state(false);
  let error = $state('');
  let historyError = $state('');
  let sessionsError = $state('');
  let conversations = $state<Conversation[]>([]);
  let availableChannelTypes = $state<string[]>([]);
  let conversationCursor: string | null = null;
  let conversationsHasMore = $state(false);
  let conversationSearch = $state('');
  let agents = $state<Agent[]>([]);
  let currentConversation = $state<Conversation | null>(null);
  let sessions = $state<Session[]>([]);
  let conversationSubloadsLoading = $state(false);
  let composer = $state('');
  let composerElement = $state<HTMLTextAreaElement | null>(null);
  let composerAttachments = $state<AttachmentRef[]>([]);

  // Composer drafts persist across tab switches (per conversation) via
  // sessionStorage. A single draft key is kept up to date with the
  // current conversation; loading a different conversation saves the
  // prior draft first, then hydrates the new one.
  const DRAFT_PREFIX = 'cognis-chat-draft:';
  let currentDraftKey: string | null = null;
  let draftSaveTimer: number | null = null;

  function readDraft(key: string): string {
    if (typeof sessionStorage === 'undefined') return '';
    try {
      return sessionStorage.getItem(key) ?? '';
    } catch {
      return '';
    }
  }

  function writeDraft(key: string, value: string): void {
    if (typeof sessionStorage === 'undefined') return;
    try {
      if (value) sessionStorage.setItem(key, value);
      else sessionStorage.removeItem(key);
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
  let selectedAgentId = $state('');
  let selectedConversationStatus = $state<'active' | 'archived'>('active');
  let archivingConversation = $state(false);
  let deletingConversation = $state(false);
  let mobileListOpen = $state(false);
  let mobileListOverlayCleanup: (() => void) | null = null;
  let mobileFilterOpen = $state(false);
  // Unified flag for the expanded header info panel. Replaces the older
  // pair of `sessionInfoOpen` (desktop popover) + `mobileHeaderDetailsOpen`
  // (mobile-only panel) with one state so the Info button has a single,
  // predictable effect regardless of viewport size.
  let headerInfoOpen = $state(false);
  // Default to iMessage-style: Enter inserts a newline and the user taps
  // the send button (or presses Cmd/Ctrl+Enter) to submit. Users who
  // previously opted into Enter-to-send keep their choice via localStorage.
  let enterToSend = $state(false);
  let queuedCount = $state(0);
  let timeline = $state<TimelineItem[]>([]);

  let visibleStartIndex = $state(0);
  let activeConversationId = '';
  const escalationTimeoutSeconds = 300;
  let escalations = $state<Escalation[]>([]);
  let escalationBusyCallId = $state<string | null>(null);
  let escalationResolutionPending = $state<Escalation | null>(null);
  let escalationError = $state('');
  let escalationCountdownTimer: number | null = null;
  let notificationRefreshTimer: number | null = null;
  let awaitingAssistantStart = $state(false);
  let turnInProgress = $state(false);
  let lastSubmittedMessage = '';
  let lastRecoverableMessage = $state('');
  let showNewChatModal = $state(false);
  let newChatAgentId = $state('');
  let newChatCreating = $state(false);
  let newChatError = $state('');
  let editingTitle = $state(false);
  let editTitleValue = $state('');
  let sessionIdCopied = $state(false);
  let showAgentProfile = $state(false);
  let subSessionPanelOpen = $state(false);
  let subSessionClosing = $state(false);
  let subSessionId = $state('');
  let subSessionEvents = $state<MessageEvent[]>([]);
  let subSessionTimeline = $state<TimelineItem[]>([]);
  let subSessionLoading = $state(false);
  let subSessionError = $state('');
  let subSessionLastSeq = $state(0);
  let subSessionPollDelayMs = $state(SESSION_LOG_POLL_INTERVAL_MS);
  let timelineEl = $state<HTMLDivElement | null>(null);
  let timelineContentEl = $state<HTMLDivElement | null>(null);
  let userScrolledUp = $state(false);
  let loadingOlderMessages = $state(false);
  let programmaticScroll = false;
  let lastTimelineScrollTop = $state(0);
  let footerChromeEl = $state<HTMLDivElement | null>(null);
  let selectedChannel = $state('all');
  let chatSidebarCollapsed = $state(false);
  interface SessionInfoData {
    intaris_session_id: string;
    intention: string | null;
    status: string;
    total_calls: number;
    approved_count: number;
    denied_count: number;
    escalated_count: number;
  }
  let sessionInfo = $state<SessionInfoData | null>(null);
  let sessionInfoLoading = $state(false);

  function toggleHeaderInfo(): void {
    headerInfoOpen = !headerInfoOpen;
    if (headerInfoOpen && !sessionInfo) {
      void loadSessionInfo();
    }
  }

  function closeHeaderInfo(): void {
    headerInfoOpen = false;
  }
  let contextUsage = $state<ContextUsage | null>(null);
  let subSessionInfoOpen = $state(false);
  let subSessionInfo = $state<SessionInfoData | null>(null);
  let subSessionInfoLoading = $state(false);
  interface PendingDirectQuestion {
    notificationId: string;
    stepName?: string;
    question: string;
    options: string[];
    context: string;
  }

  interface ChatTodo {
    content: string;
    status: string;
    priority: string;
  }

  let pendingDirectQuestion = $state<PendingDirectQuestion | null>(null);
  let directQuestionSubmitting = $state(false);
  let chatTodoDrawerOpen = $state(true);
  let retainedChatTodos = $state<ChatTodo[]>([]);

  const sessionIds = new Set<string>();

  let unsubscribeWs: (() => void) | null = null;
  let unsubscribeComposerFocus: (() => void) | null = null;
  let unsubscribeCancelTurn: (() => void) | null = null;
  let visibilityHandler: (() => void) | null = null;
  let focusHandler: (() => void) | null = null;
  let pageShowHandler: ((event: PageTransitionEvent) => void) | null = null;
  let onlineHandler: (() => void) | null = null;
  let foregroundSyncTimer: number | null = null;
  let conversationLoadRequestId = 0;
  let mobileDrawerPreviouslyFocused: HTMLElement | null = null;
  let initialLoadTimeoutTimer: number | null = null;

  const CHAT_INITIAL_LOAD_TIMEOUT_MS = 10000;

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

  function isReadOnly(conversation: Conversation | null): boolean {
    if (!conversation) return true;
    if (conversation.status !== 'active') return true;
    if (!isWebConversation(conversation)) return true;
    return false;
  }

  const TERMINAL_SESSION_STATES = new Set(['terminated', 'failed', 'cancelled']);
  const BLOCKED_SESSION_STATES = new Set(['terminated', 'failed', 'cancelled', 'suspended']);

  function activeSessionStatus(): string | null {
    if (!currentConversation?.active_session_id) return null;
    const root = sessions.find((s) => s.session_id === currentConversation?.active_session_id);
    return root?.status ?? null;
  }

  function isSessionBlocked(): boolean {
    const status = activeSessionStatus();
    return status !== null && BLOCKED_SESSION_STATES.has(status);
  }

  function normalizeToolName(name: string): string {
    return name.toLowerCase().replace(/_/g, '');
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
  ): PendingDirectQuestion {
    return {
      notificationId,
      stepName,
      question: typeof question === 'string' && question.trim().length > 0
        ? question.trim()
        : 'The assistant needs more input to continue.',
      options: directQuestionOptions(options),
      context: directQuestionContext(context)
    };
  }

  function pendingDirectQuestionFromNotification(notification: Notification): PendingDirectQuestion | null {
    if (notification.notification_type !== 'step_question' || notification.task_id || notification.status !== 'pending') {
      return null;
    }
    return pendingDirectQuestionFromParts(
      notification.notification_id,
      notification.step_name ?? undefined,
      notification.payload.question,
      notification.payload.options,
      notification.payload.context,
    );
  }

  function parseChatTodos(value: unknown): ChatTodo[] {
    if (!Array.isArray(value)) return [];
    return value
      .map((item) => {
        if (!item || typeof item !== 'object') return null;
        const record = item as Record<string, unknown>;
        const content = typeof record.content === 'string' ? record.content.trim() : '';
        if (!content) return null;
        return {
          content,
          status: typeof record.status === 'string' ? record.status : 'pending',
          priority: typeof record.priority === 'string' ? record.priority : 'medium'
        } satisfies ChatTodo;
      })
      .filter((item): item is ChatTodo => item !== null);
  }

  function parsedToolResult(item: ToolCallTimelineItem): Record<string, unknown> | null {
    if (typeof item.result !== 'string') return null;
    try {
      const parsed = JSON.parse(item.result.replace(/^<tool_result[^>]*>\n?/, '').replace(/\n?<\/tool_result>\s*$/, ''));
      return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
    } catch {
      return null;
    }
  }

  function latestChatTodos(items: TimelineItem[], resetOnUserMessage = true): ChatTodo[] {
    let lowerBound = 0;
    if (resetOnUserMessage) {
      for (let index = items.length - 1; index >= 0; index -= 1) {
        const item = items[index];
        if (item?.kind === 'message' && item.role === 'user') {
          lowerBound = index;
          break;
        }
      }
    }

    for (let index = items.length - 1; index >= 0; index -= 1) {
      if (index < lowerBound) break;
      const item = items[index];
      if (item?.kind !== 'tool_call') continue;
      const toolName = normalizeToolName(item.toolName);
      if (toolName === 'steptodowrite') {
        const parsed = parsedToolResult(item);
        if (Array.isArray(parsed?.todos)) {
          return parseChatTodos(parsed.todos);
        }
        if (item.status === 'started' && Array.isArray(item.arguments?.todos)) {
          return parseChatTodos(item.arguments.todos);
        }
        return [];
      }
      if (toolName === 'steptodolist') {
        const parsed = parsedToolResult(item);
        if (Array.isArray(parsed?.todos)) {
          return parseChatTodos(parsed.todos);
        }
        return [];
      }
    }
    return [];
  }

  /**
   * Compact status indicator for a todo row. Returns the background
   * colour for a tiny dot rendered next to the todo content — we drop
   * the old bordered pill + coloured bubble in favour of a single
   * line-of-text representation where the status reads at a glance
   * from the colour of a 6px dot.
   */
  function todoStatusDot(status: string): string {
    if (status === 'completed') return 'bg-emerald-400';
    if (status === 'cancelled') return 'bg-slate-600';
    if (status === 'in_progress') return 'bg-sky-400';
    return 'bg-sky-400';
  }

  function todoPriorityClass(priority: string): string {
    if (priority === 'high') return 'text-rose-300';
    if (priority === 'low') return 'text-slate-500';
    return 'text-slate-400';
  }

  function sortEscalations(items: Escalation[]): Escalation[] {
    return [...items].sort((left, right) => (left.received_at ?? 0) - (right.received_at ?? 0));
  }

  function persistLastOpenedConversation(conversation: Conversation): void {
    if (typeof window === 'undefined') return;
    if (conversation.status === 'active' && isWebConversation(conversation)) {
      window.localStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, conversation.conversation_id);
    }
  }

  function clearLastOpenedConversation(conversationId: string | null | undefined = null): void {
    if (typeof window === 'undefined') return;
    if (!conversationId) {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.lastOpenedConversation);
      return;
    }
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.lastOpenedConversation);
    if (stored === conversationId) {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.lastOpenedConversation);
    }
  }

  function nextVisibleConversationId(excludingConversationId: string): string | null {
    return conversations.find((conversation) => conversation.conversation_id !== excludingConversationId)?.conversation_id ?? null;
  }

  const terminalTodoStatuses = new Set(['completed', 'cancelled']);

  let chatTodos = $derived.by(() => {
    const latestTodos = latestChatTodos(timeline, currentConversation?.context?.type === 'web');
    if (latestTodos.length > 0) {
      return latestTodos;
    }
    if (pendingDirectQuestion !== null || directQuestionSubmitting) {
      return retainedChatTodos;
    }
    return [];
  });
  let activeChatTodos = $derived.by(() => chatTodos.filter((todo) => !terminalTodoStatuses.has(todo.status)));
  // Keep the latest todo snapshot visible even after everything is
  // completed so the user can still inspect what just finished.
  let shouldShowChatTodoDrawer = $derived(chatTodos.length > 0);
  let chatTodoCounts = $derived.by(() => ({
    inProgress: activeChatTodos.filter((todo) => todo.status === 'in_progress').length,
    pending: activeChatTodos.filter((todo) => todo.status === 'pending').length,
  }));
  let showTurnProgress = $derived.by(() =>
    turnInProgress
      && !timeline.some((item) => item.kind === 'message' && item.role === 'assistant' && item.streaming)
  );
  let isPreSessionConversation = $derived.by(() =>
    isPreSessionChatConversation(currentConversation, sessions.length)
  );

  function contextTypeBadge(conversation: Conversation): string {
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
    if (event.code === 'turn_cancelled') {
      return 'The current turn was cancelled.';
    }
    if (event.code === 'pending_question') {
      return 'Answer the pending clarification request to continue.';
    }
    return event.message;
  }

  function timestampValue(value: string | null | undefined): number {
    if (!value) return 0;
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? 0 : parsed;
  }

  function conversationActivityValue(conversation: Conversation): number {
    return Math.max(
      timestampValue(conversation.last_message_at),
      timestampValue(conversation.updated_at),
      timestampValue(conversation.created_at)
    );
  }

  function sortConversationsByActivity(items: Conversation[]): Conversation[] {
    return [...items].sort((left, right) => {
      const activityDelta = conversationActivityValue(right) - conversationActivityValue(left);
      if (activityDelta !== 0) return activityDelta;
      const updatedDelta = timestampValue(right.updated_at) - timestampValue(left.updated_at);
      if (updatedDelta !== 0) return updatedDelta;
      return right.conversation_id.localeCompare(left.conversation_id);
    });
  }

  function mergeConversationList(items: Conversation[], { reset = false }: { reset?: boolean } = {}): void {
    const next = reset ? [] : [...conversations];
    const indexById = new Map(next.map((conversation, index) => [conversation.conversation_id, index]));
    for (const conversation of items) {
      const index = indexById.get(conversation.conversation_id);
      if (index === undefined) {
        indexById.set(conversation.conversation_id, next.length);
        next.push(conversation);
      } else {
        next[index] = conversation;
      }
    }
    conversations = sortConversationsByActivity(next);
  }

  function patchConversationInList(
    conversationId: string,
    patch: Partial<Conversation>,
    options: { touchUpdatedAt?: boolean; touchLastMessageAt?: boolean } = {}
  ): void {
    const index = conversations.findIndex((conversation) => conversation.conversation_id === conversationId);
    if (index < 0) return;
    const now = new Date().toISOString();
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
    if (currentConversation?.conversation_id === conversationId) {
      currentConversation = { ...currentConversation, ...updated };
    }
  }

  function syncConversationActiveSession(activeSessionId: string | null | undefined): void {
    if (!currentConversation || !activeSessionId || currentConversation.active_session_id === activeSessionId) {
      return;
    }
    currentConversation = { ...currentConversation, active_session_id: activeSessionId };
    patchConversationInList(currentConversation.conversation_id, { active_session_id: activeSessionId }, { touchUpdatedAt: true });
  }

  async function loadConversationPage(reset = false): Promise<void> {
    const channelFilter = selectedChannel !== 'all' ? selectedChannel : null;
    const agentFilter = selectedAgentId !== 'all' ? selectedAgentId : null;
    const response = await api.conversations.list(reset ? null : conversationCursor, {
      contextType: channelFilter,
      agentId: agentFilter,
      status: selectedConversationStatus,
    });
    mergeConversationList(response.items, { reset });
    conversationCursor = response.cursor;
    conversationsHasMore = response.has_more;
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

  function restoreSelectedAgent(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
    if (stored && agents.some((a) => a.agent_id === stored && a.status === 'active')) {
      selectedAgentId = stored;
    } else {
      selectedAgentId = 'all';
    }
  }

  function persistSelectedAgent(): void {
    if (typeof window === 'undefined' || !selectedAgentId) return;
    if (selectedAgentId === 'all') {
      window.localStorage.removeItem(CHAT_STORAGE_KEYS.selectedAgent);
      return;
    }
    window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedAgent, selectedAgentId);
  }

  function conversationIdFromRoute(): string {
    return page.params.conversationId ?? '';
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
    return conversation.title?.trim() || 'Untitled conversation';
  }

  function beginConversationLoad(): number {
    conversationLoadRequestId = nextConversationLoadId(conversationLoadRequestId);
    return conversationLoadRequestId;
  }

  function isStaleConversationLoad(requestId: number): boolean {
    return !isCurrentConversationLoad(requestId, conversationLoadRequestId);
  }

  function conversationAgent(conversation: Conversation): Agent | undefined {
    return agents.find((agent) => agent.agent_id === conversation.agent_id);
  }

  // Display name of the current conversation's agent for composer placeholders
  // and any other in-page prompts. Falls back to "Cognis" only while the
  // conversation and agent list have not been resolved yet.
  const currentAgentDisplayName = $derived.by(() => {
    if (!currentConversation) return 'Cognis';
    const agent = conversationAgent(currentConversation);
    return agent?.display_name ?? agent?.name ?? 'Cognis';
  });

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

  async function loadHistory(conversationId: string): Promise<import('$lib/types/api').MessageHistoryResponse> {
    const events: MessageEvent[] = [];
    let afterSeq = 0;
    let activeSessionId: string | null | undefined = null;
    let activeSessionLastSeq = 0;
    let historyTruncated = false;
    let truncationReason: string | null | undefined = null;

    while (true) {
      const response = await api.conversations.messages(conversationId, afterSeq, 200);
      events.push(...response.items);
      activeSessionId = response.active_session_id;
      activeSessionLastSeq = response.active_session_last_seq ?? activeSessionLastSeq;
      historyTruncated = response.history_truncated ?? historyTruncated;
      truncationReason = response.truncation_reason ?? truncationReason;
      if (!response.has_more || response.items.length === 0) {
        return {
          items: events,
          last_seq: response.last_seq,
          has_more: response.has_more,
          active_session_id: activeSessionId,
          active_session_last_seq: activeSessionLastSeq,
          history_truncated: historyTruncated,
          truncation_reason: truncationReason
        };
      }

      afterSeq = getNextHistoryAfterSeq(response);
      if (afterSeq === 0) {
        return {
          items: events,
          last_seq: response.last_seq,
          has_more: response.has_more,
          active_session_id: activeSessionId,
          active_session_last_seq: activeSessionLastSeq,
          history_truncated: historyTruncated,
          truncation_reason: truncationReason
        };
      }
    }
  }

  async function loadSessionHistory(
    conversationId: string,
    sessionId: string,
  ): Promise<{ events: MessageEvent[]; lastSeq: number; truncated: boolean }> {
    const events: MessageEvent[] = [];
    let afterSeq = 0;
    let pageCount = 0;
    let lastSeq = 0;

    while (pageCount < SESSION_LOG_BOOTSTRAP_MAX_PAGES) {
      const response = await api.conversations.sessionEvents(conversationId, sessionId, afterSeq, SESSION_LOG_PAGE_SIZE);
      events.push(...(response.items ?? []));
      lastSeq = response.last_seq;
      pageCount += 1;
      if (!response.has_more || response.items.length === 0) {
        return { events, lastSeq, truncated: false };
      }
      afterSeq = getNextHistoryAfterSeq(response);
      if (afterSeq === 0) {
        return { events, lastSeq, truncated: false };
      }
    }

    events.push({
      seq: null,
      type: 'history_gap',
      data: { reason: 'bootstrap_cap_reached', session_id: sessionId },
      timestamp: new Date().toISOString()
    });
    return { events, lastSeq, truncated: true };
  }

  async function refreshSidebarData(): Promise<void> {
    [agents] = await Promise.all([api.agents.listAll()]);
    restoreSelectedAgent();
    await refreshAvailableChannelTypes();
    await loadConversationPage(true);
  }

  async function refreshAvailableChannelTypes(): Promise<void> {
    const agentFilter = selectedAgentId !== 'all' ? selectedAgentId : null;
    const allConversations = await api.conversations.listAll({
      agentId: agentFilter,
      contextType: null,
      status: selectedConversationStatus
    });
    const types = new Set(
      allConversations.map((conversation) => conversation.context?.type?.toLowerCase() ?? 'unknown')
    );
    if (selectedChannel && selectedChannel !== 'all') {
      types.add(selectedChannel.toLowerCase());
    }
    availableChannelTypes = [...types].sort();
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
      for (const item of filtered) {
        if (!escalations.some((e) => e.call_id === item.call_id)) {
          item.timeout_seconds = item.timeout_seconds ?? escalationTimeoutSeconds;
        }
      }
      const pendingStillExists = escalationResolutionPending
        ? filtered.some((item) => item.call_id === escalationResolutionPending?.call_id)
        : false;
      if (escalationResolutionPending && !pendingStillExists) {
        if (escalationBusyCallId === escalationResolutionPending.call_id) {
          escalationBusyCallId = null;
        }
        escalationResolutionPending = null;
      }
      escalations = sortEscalations(
        escalationResolutionPending
          ? filtered.filter((item) => item.call_id !== escalationResolutionPending?.call_id)
          : filtered
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
        (item) => item.notification_type === 'step_question' && item.status === 'pending',
      );
      // Annotate any pending step_request_input tool call with a
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
      pendingDirectQuestion = directQuestion;
      if (directQuestion === null) {
        directQuestionSubmitting = false;
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
      // Auto-remove expired escalations (server handles timeout, this is UI cleanup)
      const now = Date.now();
      escalations = escalations.filter((e) => {
        const elapsed = (now - (e.received_at ?? now)) / 1000;
        const timeout = e.timeout_seconds ?? escalationTimeoutSeconds;
        return elapsed < timeout + 5; // 5s grace for network latency
      });
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

  function scrollToBottom(force = false): void {
    if (!timelineEl || (!force && userScrolledUp)) return;
    programmaticScroll = true;
    requestAnimationFrame(() => {
      if (timelineEl) {
        timelineEl.scrollTop = timelineEl.scrollHeight;
        lastTimelineScrollTop = timelineEl.scrollTop;
      }
      programmaticScroll = false;
    });
  }

  function handleTimelineScroll(): void {
    if (!timelineEl || programmaticScroll) return;
    const currentScrollTop = timelineEl.scrollTop;
    const distanceFromBottom = timelineEl.scrollHeight - timelineEl.scrollTop - timelineEl.clientHeight;

    // Pause live-follow as soon as the user actively scrolls upward, instead
    // of waiting until they are far away from the tail. This makes it much
    // easier to escape a fast-moving stream on touch devices.
    if (currentScrollTop < lastTimelineScrollTop - 2 && distanceFromBottom > 0) {
      userScrolledUp = true;
    } else if (distanceFromBottom <= 24) {
      userScrolledUp = false;
    } else if (distanceFromBottom > 80) {
      userScrolledUp = true;
    }

    lastTimelineScrollTop = currentScrollTop;

    if (currentScrollTop <= 24 && visibleStartIndex > 0) {
      void loadOlder();
    }
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    scrollToBottom(true);
  }

  function channelTypes(): string[] {
    return availableChannelTypes;
  }

  async function persistSelectedChannel(): Promise<void> {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(CHAT_STORAGE_KEYS.selectedChannel, selectedChannel);
    await loadConversationPage(true);
  }

  function restoreSelectedChannel(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedChannel);
    if (stored) selectedChannel = stored;
  }

  function restoreChatSidebarState(): void {
    if (typeof window === 'undefined') return;
    chatSidebarCollapsed = window.localStorage.getItem(CHAT_STORAGE_KEYS.sidebarCollapsed) === '1';
  }

  function toggleChatSidebar(): void {
    chatSidebarCollapsed = !chatSidebarCollapsed;
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(CHAT_STORAGE_KEYS.sidebarCollapsed, chatSidebarCollapsed ? '1' : '0');
    }
  }

  function openMobileList(): void {
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
    if (!isMobileViewport()) return;
    if (get(mobileNavOpenStore)) return; // already at the top of the stack
    if (mobileListOpen) {
      requestOpenMobileNav();
      return;
    }
    openMobileList();
  }

  function handleChatRightEdgeSwipe(): void {
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
    const sid = currentConversation?.active_session_id;
    if (!sid) return;
    sessionInfoLoading = true;
    try {
      const detail = await api.sessions.intarisDetail(sid);
      sessionInfo = {
        intaris_session_id: detail.intaris_session_id,
        intention: detail.intention,
        status: detail.status,
        total_calls: detail.total_calls,
        approved_count: detail.approved_count,
        denied_count: detail.denied_count,
        escalated_count: detail.escalated_count
      };
    } catch {
      sessionInfo = null;
    } finally {
      sessionInfoLoading = false;
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
        status: detail.status,
        total_calls: detail.total_calls,
        approved_count: detail.approved_count,
        denied_count: detail.denied_count,
        escalated_count: detail.escalated_count
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
    } = {},
  ): Promise<void> {
    const reloadSessions = options.reloadSessions ?? true;
    const reloadHistory = options.reloadHistory ?? true;
    const shouldResubscribe = options.resubscribe ?? false;
    const preserveTimelineOnHistoryFailure = options.preserveTimelineOnHistoryFailure ?? false;

    const [sessionResult, historyResult] = await Promise.allSettled([
      reloadSessions ? api.conversations.sessions(conversationId) : Promise.resolve(sessions),
      reloadHistory
        ? loadHistory(conversationId)
        : Promise.resolve({
            items: [],
            last_seq: 0,
            has_more: false,
            active_session_id: null,
            active_session_last_seq: 0,
            history_truncated: false,
            truncation_reason: null
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
      timeline = normalizeHistory(historyResult.value.items);
      syncConversationActiveSession(nextActiveSessionId);
      syncVisibleWindow();
      userScrolledUp = false;
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

    if (shouldResubscribe) {
      wsClient.subscribeConversation(
        conversationId,
        reloadHistory && historyResult.status === 'fulfilled'
          ? (historyResult.value.active_session_last_seq ?? 0)
          : 0,
        reloadHistory && historyResult.status === 'fulfilled'
          ? (historyResult.value.active_session_id ?? null)
          : nextActiveSessionId,
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
      directQuestionSubmitting = false;
    }

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
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
      switchingConversation = false;
      initialConversationResolved = true;
      return;
    }

    if (conversationId === activeConversationId && currentConversation) {
      initializing = false;
      switchingConversation = false;
      initialConversationResolved = true;
      return;
    }

    const requestId = beginConversationLoad();
    const previousConversationId = activeConversationId;
    const isInitialLoad = !initialConversationResolved && !currentConversation;

    showAgentProfile = false;
    switchingConversation = !isInitialLoad;
    error = '';
    historyError = '';
    sessionsError = '';
    escalationError = '';
    escalationResolutionPending = null;
    headerInfoOpen = false;
    mobileListOpen = false;

    if (previousConversationId) {
      wsClient.unsubscribeConversation(previousConversationId);
    }

    try {
      const conversation = await api.conversations.detail(conversationId);
      if (isStaleConversationLoad(requestId)) {
        return;
      }

      const desiredStatusFilter: 'active' | 'archived' = conversation.status === 'archived' ? 'archived' : 'active';
      if (selectedConversationStatus !== desiredStatusFilter) {
        selectedConversationStatus = desiredStatusFilter;
        await refreshAvailableChannelTypes();
        await loadConversationPage(true);
        if (isStaleConversationLoad(requestId)) {
          return;
        }
      }

      activeConversationId = conversationId;
      currentConversation = conversation;
      initialLoadTimedOut = false;
      persistLastOpenedConversation(conversation);
      mergeConversationList([conversation]);
      queuedCount = 0;
      turnInProgress = false;
      awaitingAssistantStart = false;
      pendingDirectQuestion = null;
      directQuestionSubmitting = false;
      lastRecoverableMessage = '';
      editingTitle = false;
      contextUsage = null;
      subSessionPanelOpen = false;

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
      escalations = [];
      escalationResolutionPending = null;
      pendingDirectQuestion = null;
      directQuestionSubmitting = false;
      sessionIds.clear();
      conversationSubloadsLoading = false;
    } finally {
      if (!isStaleConversationLoad(requestId)) {
        initializing = false;
        switchingConversation = false;
        initialConversationResolved = true;
        if (currentConversation || error) {
          stopInitialLoadTimeout();
        }
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

    if (notificationsSupported() && !notificationsGranted() && !hasAskedPermission()) {
      setTimeout(() => { void requestPermission(); }, 5000);
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
    // On mobile the conversation list is a sliding overlay. Hide it before
    // showing the modal or the modal is obscured by the list drawer.
    mobileListOpen = false;
    showNewChatModal = true;
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
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {}
        }
      });
      selectedAgentId = agentId;
      persistSelectedAgent();
      await refreshSidebarData();
      showNewChatModal = false;
      addToast('Conversation created.', 'success');
      await goto(`/chat/${conversation.conversation_id}`);
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
      await refreshSidebarData();
      const nextConversationId = nextVisibleConversationId(archivedConversation.conversation_id);
      addToast('Conversation archived.', 'success');
      await goto(nextConversationId ? `/chat/${nextConversationId}` : '/chat/new');
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
      await refreshSidebarData();
      const nextConversationId = nextVisibleConversationId(deletedConversationId);
      addToast('Conversation deleted.', 'success');
      await goto(nextConversationId ? `/chat/${nextConversationId}` : '/chat/new');
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

  async function setConversationStatusFilter(status: 'active' | 'archived'): Promise<void> {
    if (selectedConversationStatus === status) return;
    selectedConversationStatus = status;
    await refreshAvailableChannelTypes();
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

  function startEditTitle(): void {
    if (!currentConversation) return;
    editTitleValue = currentConversation.title ?? '';
    editingTitle = true;
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
        })
      );
    } catch (caughtError) {
      addToast(asApiError(caughtError).message, 'error', 4_000, 'Unable to open Intaris');
    }
  }

  /** Slash commands that are handled as system actions, not chat messages. */
  const SYSTEM_SLASH_COMMANDS = ['/approve', '/deny', '/compact', '/summarize', '/new', '/reset', '/clear', '/stop', '/cancel', '/context', '/info', '/lsp', '/model', '/thinking', '/help'];

  function normalizeSlashCommandInput(value: string): string {
    const trimmed = value.trim();
    if (!trimmed.startsWith('/')) return trimmed;
    return `/${trimmed.slice(1).trimStart()}`;
  }

  function isSystemSlashCommand(value: string): boolean {
    const normalized = normalizeSlashCommandInput(value);
    return SYSTEM_SLASH_COMMANDS.some((command) => normalized === command || normalized.startsWith(`${command} `));
  }

  /** Slash command suggestions shown when user types /. */
  const SLASH_SUGGESTIONS = [
    { command: '/help', description: 'Show available commands' },
    { command: '/model', description: 'List or switch LLM model' },
    { command: '/thinking', description: 'Set reasoning effort' },
    { command: '/context', description: 'Show context usage' },
    { command: '/info', description: 'Show session details' },
    { command: '/lsp', description: 'Show LSP diagnostics status' },
    { command: '/compact', description: 'Compact conversation' },
    { command: '/new', description: 'Start new conversation' },
    { command: '/stop', description: 'Stop current work' },
    { command: '/cancel', description: 'Alias for /stop' },
    { command: '/approve', description: 'Approve tool escalation' },
    { command: '/deny', description: 'Deny tool escalation' },
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
    const needsArg = ['/model', '/thinking', '/approve', '/deny'].includes(suggestion.command);
    composer = needsArg ? suggestion.command + ' ' : suggestion.command;
    slashSuggestionsVisible = false;
    focusActiveComposer();
  }

  function focusActiveComposer(): void {
    composerElement?.focus();
  }

  async function handleSend(): Promise<void> {
    const content = composer.trim();
    if ((!content && composerAttachments.length === 0) || !currentConversation || isReadOnly(currentConversation)) return;
    if (pendingDirectQuestion && directQuestionSubmitting) return;

    const normalizedSlashCommand = normalizeSlashCommandInput(content);
    const isSlashCommand = isSystemSlashCommand(content);
    const outboundContent = isSlashCommand ? normalizedSlashCommand : content;

    // Detect a `step_request_input` tool call sitting in the timeline
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

    // Optimistic UI. When the message is a step_request_input reply we do
    // not append a separate user bubble — the tool call block will show the
    // user's answer inline as the resolution. Adding a bubble too would
    // duplicate the content and leave the tool call block stuck as pending.
    if (!isSlashCommand && !isStepInputReply) {
      timeline = appendOptimisticUserMessage(timeline, content, composerAttachments);
    }
    if (!isSlashCommand) {
      lastSubmittedMessage = content;
      lastRecoverableMessage = '';
      awaitingAssistantStart = true;
      patchConversationInList(currentConversation.conversation_id, { has_unread: false }, {
        touchUpdatedAt: true,
        touchLastMessageAt: true,
      });
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
      //   3. A fresh fetch of pending step_question notifications.
      let notificationId = pendingStepTool?.notificationId ?? pendingDirectQuestion?.notificationId ?? '';
      if (!notificationId && currentConversation) {
        try {
          const list = await api.notifications.list(currentConversation.conversation_id);
          const match = list.find(
            (item) => item.notification_type === 'step_question' && item.status === 'pending',
          );
          if (match) notificationId = match.notification_id;
        } catch {
          // Fall through — we'll send as a regular message below if lookup fails.
        }
      }

      if (notificationId) {
        directQuestionSubmitting = true;
        if (pendingStepTool) {
          // Show the user's answer inside the tool call block immediately
          // so the Resolution section stops saying "Waiting for user input"
          // even before the backend tool_result arrives.
          timeline = optimisticallyResolveStepRequestInput(timeline, pendingStepTool.id, content);
        }
        syncVisibleWindow();
        userScrolledUp = false;
        scrollToBottom();
        const stepName =
          typeof pendingStepTool?.arguments?.step_name === 'string'
            ? (pendingStepTool.arguments.step_name as string)
            : pendingDirectQuestion?.stepName;
        wsClient.respondStepQuestion(notificationId, content, stepName);
        return;
      }

      // Fall back to a regular message: we saw a pending step_request_input
      // but could not resolve a notification_id. Replace the missing
      // optimistic bubble so the user still sees their message.
      timeline = appendOptimisticUserMessage(timeline, content, attachments);
    }

    syncVisibleWindow();
    userScrolledUp = false;
    scrollToBottom();
    wsClient.sendMessage(currentConversation.conversation_id, outboundContent, attachments);
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
    const items = event.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (const item of items) {
      if (item.kind === 'file') {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length > 0) {
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
    if (!timelineEl || loadingOlderMessages || visibleStartIndex === 0) return;

    const previousScrollHeight = timelineEl.scrollHeight;
    const previousScrollTop = timelineEl.scrollTop;
    loadingOlderMessages = true;
    programmaticScroll = true;

    visibleStartIndex = Math.max(0, visibleStartIndex - 50);
    await tick();

    requestAnimationFrame(() => {
      if (timelineEl) {
        timelineEl.scrollTop = previousScrollTop + (timelineEl.scrollHeight - previousScrollHeight);
        lastTimelineScrollTop = timelineEl.scrollTop;
      }
      loadingOlderMessages = false;
      programmaticScroll = false;
    });
  }

  function handleSocketEvent(event: import('$lib/types/api').CognisWebSocketEvent): void {
    const currentId = conversationIdFromRoute();
    if ('conversation_id' in event && event.conversation_id && event.conversation_id !== currentId) {
      // Event for a different conversation — mark it as unread locally
      // and show a browser notification if appropriate.
      const otherConvId = event.conversation_id;
      if (event.type === 'message_complete' || event.type === 'workflow_completed' || event.type === 'workflow_failed') {
        const idx = conversations.findIndex((c) => c.conversation_id === otherConvId);
        const conversation = idx >= 0 ? conversations[idx] : null;
        if (idx >= 0) {
          patchConversationInList(
            otherConvId,
            { has_unread: true },
            {
              touchUpdatedAt: true,
              touchLastMessageAt: event.type === 'message_complete',
            }
          );
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

    if (currentConversation) {
      if (event.type === 'user_message' || event.type === 'message_complete') {
        patchConversationInList(currentConversation.conversation_id, { has_unread: false }, {
          touchUpdatedAt: true,
          touchLastMessageAt: true,
        });
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
        patchConversationInList(currentConversation.conversation_id, { has_unread: false }, { touchUpdatedAt: true });
      }
    }

    // Filter sub-session tool/chunk events from the main timeline (defense-in-depth)
    const rootSid = currentConversation?.active_session_id;
    if (rootSid && 'session_id' in event && event.session_id && event.session_id !== rootSid) {
      if (event.type === 'tool_call' || event.type === 'tool_result' || event.type === 'chunk') {
        return;
      }
    }

    if (event.type === 'queued' || event.type === 'message_complete' || event.type === 'turn_settled') {
      queuedCount = event.queued_count ?? 0;
    }

    if (event.type === 'turn_started' || event.type === 'queued') {
      turnInProgress = true;
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
        directQuestionSubmitting = false;
        pendingDirectQuestion = null;
        if (escalationBusyCallId) {
          escalationBusyCallId = null;
          escalationResolutionPending = null;
          void refreshEscalations();
        }
        return;
      }
      error = nextError;
      awaitingAssistantStart = false;
      turnInProgress = false;
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

    if (event.type === 'chunk' || event.type === 'tool_call' || event.type === 'delegation_started') {
      awaitingAssistantStart = false;
      turnInProgress = true;
    }

    if (
      'session_id' in event
      && shouldAdoptConversationSessionId(currentConversation?.active_session_id, event.type, event.session_id)
    ) {
      syncConversationActiveSession(event.session_id);
    }

    if (event.type === 'turn_settled') {
      awaitingAssistantStart = false;
      turnInProgress = false;
      directQuestionSubmitting = false;
    }

    if (event.type === 'message_complete' || event.type === 'workflow_completed' || event.type === 'workflow_failed' || event.type === 'workflow_cancelled') {
      awaitingAssistantStart = false;
      turnInProgress = false;
      if (directQuestionSubmitting) {
        pendingDirectQuestion = null;
      }
      directQuestionSubmitting = false;
      // Update context usage from message_complete
      if (event.type === 'message_complete' && event.context_usage) {
        contextUsage = event.context_usage;
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

    // Handle conversation_updated for title changes
    if (event.type === 'conversation_updated') {
      if (currentConversation && event.conversation_id === currentConversation.conversation_id) {
        if (typeof event.title === 'string') {
          currentConversation = { ...currentConversation, title: event.title };
          patchConversationInList(currentConversation.conversation_id, { title: event.title }, { touchUpdatedAt: true });
        }
      }
      return;
    }

    // Handle session_compacted: add to timeline and refresh sessions
    if (event.type === 'session_compacted') {
      timeline = applyWebSocketEvent(timeline, event);
      syncVisibleWindow();
      scrollToBottom();
      // Refresh session list to show the new session
      if (currentConversation) {
        api.conversations.sessions(currentConversation.conversation_id).then((s) => { sessions = s; }).catch(() => {});
      }
      return;
    }

    // Handle session_reset: clear timeline for new session
    if (event.type === 'session_reset') {
      awaitingAssistantStart = false;
      turnInProgress = false;
      timeline = applyWebSocketEvent([], {
        type: 'system_message',
        conversation_id: event.conversation_id,
        text: 'Started a new session.',
      });
      syncVisibleWindow();
      scrollToBottom(true);
      // Refresh session list
      if (currentConversation) {
        api.conversations.sessions(currentConversation.conversation_id).then((s) => { sessions = s; }).catch(() => {});
      }
      return;
    }

    if (event.type === 'reconnected') {
      awaitingAssistantStart = false;
      turnInProgress = false;
    }

    // Handle conversation_created: navigate to new conversation
    if (event.type === 'conversation_created') {
      void goto(`/chat/${event.conversation_id}`);
      return;
    }

    if (event.type === 'workflow_step_question' && event.notification_id) {
      // Annotate the matching step_request_input tool call so the user's
      // next reply can be routed to `respondStepQuestion` even if this
      // banner-level pendingDirectQuestion state gets cleared (reload,
      // compaction, or a message_complete arriving during submission).
      timeline = annotateStepRequestInputWithNotification(timeline, event.notification_id);
      if (!event.task_id) {
        pendingDirectQuestion = pendingDirectQuestionFromParts(
          event.notification_id,
          event.step_name,
          event.question,
          event.options,
          event.context,
        );
        directQuestionSubmitting = false;
        awaitingAssistantStart = false;
        turnInProgress = false;
      }
    }

    if (event.type === 'workflow_step_question_resolved') {
      if (pendingDirectQuestion && event.notification_id === pendingDirectQuestion.notificationId) {
        pendingDirectQuestion = null;
      }
      directQuestionSubmitting = false;
    }

    timeline = applyWebSocketEvent(timeline, event);
    // Skip syncVisibleWindow for high-frequency streaming events to avoid
    // triggering a full virtual-scroll recalculation on every delta.
    if (
      event.type !== 'tool_call' &&
      event.type !== 'tool_result' &&
      event.type !== 'assistant_thinking_chunk'
    ) {
      syncVisibleWindow();
    }

    // Auto-scroll on new content
    if (event.type === 'chunk' || event.type === 'message_complete' || event.type === 'delegation_started' || event.type === 'delegation_completed' || event.type === 'system_message' || event.type === 'user_message') {
      scrollToBottom();
    }

    // No longer polling for escalations — they arrive via push events
  }

  async function handleAgentFilterChange(next?: string): Promise<void> {
    if (typeof next === 'string') {
      selectedAgentId = next;
    }
    persistSelectedAgent();
    await refreshAvailableChannelTypes();
    await loadConversationPage(true);
  }

  let subSessionPollTimer: number | null = null;

  async function handleViewSession(sessionId: string): Promise<void> {
    if (!currentConversation) return;
    subSessionId = sessionId;
    subSessionPanelOpen = true;
    subSessionLoading = true;
    subSessionError = '';
    subSessionEvents = [];
    subSessionTimeline = [];
    subSessionLastSeq = 0;
    subSessionPollDelayMs = SESSION_LOG_POLL_INTERVAL_MS;
    subSessionInfo = null;
    subSessionInfoOpen = false;
    try {
      const result = await loadSessionHistory(currentConversation.conversation_id, sessionId);
      subSessionEvents = result.events;
      subSessionTimeline = normalizeHistory(result.events);
      subSessionLastSeq = result.lastSeq;
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
        const result = await api.conversations.sessionEvents(
          currentConversation.conversation_id,
          subSessionId,
          subSessionLastSeq,
          SESSION_LOG_PAGE_SIZE
        );
        if ((result.items ?? []).length > 0) {
          subSessionEvents = [...subSessionEvents, ...(result.items ?? [])];
          subSessionTimeline = normalizeHistory(subSessionEvents);
        }
        subSessionLastSeq = result.last_seq;
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
      subSessionEvents = [];
      subSessionTimeline = [];
      subSessionLastSeq = 0;
      subSessionInfo = null;
      subSessionInfoOpen = false;
    }, 250);
  }

  $effect(() => {
    if (page.params.conversationId && page.params.conversationId !== activeConversationId) {
      void openConversation(page.params.conversationId);
    } else if (!page.params.conversationId) {
      initializing = false;
      initialConversationResolved = true;
    }
  });

  $effect(() => {
    if (shouldShowChatTodoDrawer) {
      chatTodoDrawerOpen = true;
    }
  });

  $effect(() => {
    if ((!timelineContentEl && !footerChromeEl) || typeof ResizeObserver === 'undefined') {
      return;
    }
    const observer = new ResizeObserver(() => {
      requestAnimationFrame(() => scrollToBottom());
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
    const latestTodos = latestChatTodos(timeline, currentConversation?.context?.type === 'web');
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

  let visibleConversationList = $derived.by(() => {
    let list = conversations;
    const query = conversationSearch.trim().toLowerCase();
    if (query) {
      list = list.filter((c) => conversationTitle(c).toLowerCase().includes(query));
    }
    return sortConversationsByActivity(list);
  });

  let displayedTimeline = $derived(timeline.slice(visibleStartIndex));

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
      if (!document.hidden) {
        scheduleForegroundReconcile();
        void refreshEscalations();
        void refreshPendingDirectQuestion();
      }
    };
    focusHandler = () => {
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
    class={`relative flex h-full min-h-0 flex-col gap-3 overflow-hidden ${chatSidebarCollapsed ? '' : 'lg:grid lg:grid-cols-[320px_minmax(0,1fr)] lg:gap-4'}`}
    use:edgeSwipe={{ edge: 'left', onTrigger: handleChatLeftEdgeSwipe }}
    use:edgeSwipe={{ edge: 'right', onTrigger: handleChatRightEdgeSwipe }}
  >
    {#if mobileListOpen}
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
    <aside
      aria-label="Conversation list"
      aria-modal={mobileListOpen ? 'true' : undefined}
      class={`fixed left-3 right-3 top-[calc(1rem+env(safe-area-inset-top))] bottom-[calc(0.75rem+env(safe-area-inset-bottom))] z-40 flex max-w-[22rem] min-h-0 flex-col rounded-[1.75rem] border border-slate-800/80 bg-slate-900/95 shadow-card backdrop-blur transition-transform duration-200 ease-out lg:static lg:right-auto lg:max-w-none lg:w-[18rem] lg:translate-x-0 lg:rounded-none lg:border-0 lg:border-r lg:border-slate-800/60 lg:bg-transparent lg:shadow-none lg:backdrop-blur-0 ${chatSidebarCollapsed ? 'lg:hidden' : 'lg:flex'} ${mobileListOpen || !currentConversation ? 'translate-x-0' : '-translate-x-[120%] pointer-events-none lg:pointer-events-auto'}`}
      inert={(!mobileListOpen && Boolean(currentConversation) && isMobileViewport()) || undefined}
      role={mobileListOpen ? 'dialog' : undefined}
    >
      <!-- Static top: filters -->
      <div class="shrink-0 space-y-3 p-4 pb-2 sm:p-4">
        <div class="flex items-center justify-between lg:hidden">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Conversations</p>
          <div class="flex items-center gap-2">
            <Button aria-label="Toggle filters" size="sm" variant="secondary" onclick={() => (mobileFilterOpen = !mobileFilterOpen)}>
              {#if mobileFilterOpen}
                <ChevronUp class="h-4 w-4" />
              {:else}
                <ChevronDown class="h-4 w-4" />
              {/if}
            </Button>
            <Button aria-label="Close conversation list" size="sm" variant="secondary" onclick={closeMobileList}>Close</Button>
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
          <div class={`space-y-3 ${mobileFilterOpen ? 'block' : 'hidden lg:block'}`}>
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
                <option value={ch}>{ch.charAt(0).toUpperCase() + ch.slice(1)}</option>
              {/each}
            </select>
          </label>
          </div>
        {/if}

        <div class="flex items-center justify-between gap-3 border-t border-slate-800/60 pt-3">
          <h2 class="text-sm font-semibold text-white">History</h2>
          <button
            class="text-xs font-medium text-sky-400 transition hover:text-sky-300"
            onclick={openNewConversationModal}
            type="button"
          >+ New</button>
        </div>

        <div class="relative">
          <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
          <Input bind:value={conversationSearch} class="pl-9" placeholder="Filter by title" />
        </div>

        <div class="grid grid-cols-2 gap-2">
          <Button
            size="sm"
            variant={selectedConversationStatus === 'active' ? 'primary' : 'secondary'}
            onclick={() => void setConversationStatusFilter('active')}
          >Active</Button>
          <Button
            size="sm"
            variant={selectedConversationStatus === 'archived' ? 'primary' : 'secondary'}
            onclick={() => void setConversationStatusFilter('archived')}
          >Archived</Button>
        </div>
      </div>

      <!-- Scrollable middle: conversation list -->
      <div class="min-h-0 flex-1 overflow-y-auto px-4 py-2">
        <div class="space-y-1">
          {#if visibleConversationList.length === 0}
            <p class="rounded-2xl border border-dashed border-slate-700 px-4 py-6 text-center text-sm text-slate-400">
              No conversations found.
            </p>
          {:else}
            {#each visibleConversationList as conversation}
              {@const agent = conversationAgent(conversation)}
              {@const isActive = conversation.conversation_id === currentConversation?.conversation_id}
              {@const unread = conversation.has_unread && !isActive}
              <a
                class={`flex items-start gap-3 rounded-xl px-3 py-2.5 transition ${isActive ? 'bg-sky-500/15 text-white' : 'text-slate-200 hover:bg-slate-900/60'}`}
                href={`/chat/${conversation.conversation_id}`}
                onclick={closeMobileList}
              >
                <div class="relative shrink-0">
                  <AgentAvatar name={agent?.display_name ?? agent?.name ?? conversation.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-8 w-8" />
                  {#if unread}
                    <span class="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-slate-950 bg-sky-400"></span>
                  {/if}
                </div>
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm {unread ? 'font-semibold text-white' : 'font-medium text-white'}">{conversationTitle(conversation)}</p>
                  <div class="mt-0.5 flex items-center gap-2">
                    <span class="truncate text-xs text-slate-400">{agent?.display_name ?? agent?.name ?? conversation.agent_id}</span>
                    {#if (conversation.context?.type ?? 'web').toLowerCase() !== 'web'}
                      <span class="shrink-0 rounded-full border border-slate-700 bg-slate-800/60 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-widest text-slate-500">
                        {contextTypeBadge(conversation)}
                      </span>
                    {/if}
                  </div>
                </div>
              </a>
            {/each}
          {/if}

          {#if conversationsHasMore}
            <div class="pt-2">
              <Button class="w-full justify-center" size="sm" variant="secondary" onclick={() => loadConversationPage(false)}>Load more conversations</Button>
            </div>
          {/if}
        </div>
      </div>

    </aside>

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
        Chat header. On mobile the global top bar is hidden on chat detail
        (to maximise conversation space), so this header is the one that
        carries the status-bar clearance on iOS PWAs. Pad the top by
        `env(safe-area-inset-top)` on mobile; on lg+ there's no status
        bar to worry about so padding stays normal.
      -->
      <div class="border-b border-slate-800/80 px-2.5 pt-[calc(0.5rem+env(safe-area-inset-top))] pb-2 sm:px-4 sm:pt-[calc(0.75rem+env(safe-area-inset-top))] sm:pb-3 lg:px-5 lg:pt-4 lg:pb-4">
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
              <div class="flex items-center gap-1.5 lg:hidden">
                <Button aria-label="Open navigation" size="sm" variant="secondary" onclick={requestOpenMobileNav}>
                  <Menu class="h-4 w-4" />
                </Button>
                <Button aria-label="Open conversations" size="sm" variant="secondary" onclick={openMobileList}>
                  <ArrowLeft class="h-4 w-4" />
                </Button>
              </div>
              <!-- Editable title -->
              {#if editingTitle}
                <!-- svelte-ignore a11y_autofocus -->
                <input
                  class="min-w-0 flex-1 rounded-lg border border-sky-500/50 bg-slate-950/80 px-2 py-1 text-lg font-semibold text-white focus:outline-none focus:ring-1 focus:ring-sky-300 sm:text-xl"
                  bind:value={editTitleValue}
                  onblur={saveTitle}
                  onkeydown={handleTitleKeydown}
                  autofocus
                />
              {:else}
                <button
                  class="min-w-0 flex-1 truncate text-left text-lg font-semibold text-white transition hover:text-sky-300 sm:text-xl"
                  onclick={startEditTitle}
                  type="button"
                  title="Click to edit title"
                >
                  {currentConversation ? conversationTitle(currentConversation) : 'Conversation'}
                </button>
              {/if}
            </div>

            <!-- Sub-header info row -->
            <div class="mt-1.5 hidden flex-wrap items-center gap-3 text-sm text-slate-400 sm:flex">
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
                  <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400" title="Sub-sessions">
                    {sessions.length} sessions
                  </span>
                {/if}

                <!-- Context usage badge (right-aligned) -->
                {#if contextUsage}
                  <span class="ml-auto flex items-center gap-1.5 text-[10px] font-medium {contextUsage.percentage > 85 ? 'text-rose-400' : contextUsage.percentage > 60 ? 'text-sky-400' : 'text-slate-400'}" title="Context: {contextUsage.prompt_tokens.toLocaleString()} / {contextUsage.max_context_tokens.toLocaleString()} tokens ({contextUsage.model}){contextUsage.reasoning_effort ? ` | thinking: ${contextUsage.reasoning_effort}` : ''}">
                    <span class="font-mono">{contextUsage.prompt_tokens.toLocaleString()}</span>
                    <span class="opacity-50">({contextUsage.percentage}%)</span>
                    {#if contextUsage.reasoning_effort}
                      <span class="rounded border border-cyan-500/30 px-1 text-cyan-400">{contextUsage.reasoning_effort}</span>
                    {/if}
                  </span>
                {/if}
              {:else}
                <span>No active conversation selected</span>
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
          <div class="flex items-center gap-2">
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
          </div>
        </div>
      </div>

      <!--
        Expanded session details panel. Sits directly below the header
        so it extends the header's width, not the title column's. On
        mobile, it also carries the agent / context / session-id
        chips that are hidden in the sub-header row above.
      -->
      {#if headerInfoOpen && currentConversation}
        {@const panelAgent = conversationAgent(currentConversation)}
        <div class="border-b border-slate-800/80 bg-slate-900/40 px-3 py-3 sm:px-4 sm:py-4 lg:px-5">
          <div class="mb-3 flex flex-wrap items-center gap-2 text-sm text-slate-300 sm:hidden">
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
            {@const intentionView = extractIntentionDisplay(sessionInfo.intention)}
            {#if intentionView.title || intentionView.intention}
              <div class="mb-3">
                <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Intention</p>
                {#if intentionView.title}
                  <p class="mt-1 text-sm font-medium text-white">{intentionView.title}</p>
                {/if}
                {#if intentionView.intention}
                  <p class="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-200">{intentionView.intention}</p>
                {/if}
              </div>
            {/if}
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
          {:else}
            <p class="text-xs text-slate-500">Unable to load session details.</p>
          {/if}
          <div class="mt-3 flex flex-wrap gap-2 sm:hidden">
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

        {#if queuedCount > 0}
          <p class="rounded-2xl border border-sky-400/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-100">
            {queuedCount} additional message{queuedCount === 1 ? '' : 's'} queued for this conversation.
          </p>
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

        <!-- Timeline -->
        <div
          class="relative min-h-0 flex-1 overflow-y-auto overscroll-contain px-2.5 py-1.5 sm:p-4"
          bind:this={timelineEl}
          onscroll={handleTimelineScroll}
          onpointerdown={closeHeaderInfo}
        >
          <div bind:this={timelineContentEl} class="space-y-3">
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
              {#each displayedTimeline as item (item.id)}
                {#if item.kind === 'message'}
                  <div class={`flex min-w-0 ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                    <ChatMessage {item} agent={currentConversation ? conversationAgent(currentConversation) ?? null : null} />
                  </div>
                {:else if item.kind === 'thinking'}
                  <div><ThinkingBlock item={item as ThinkingTimelineItem} /></div>
                {:else if item.kind === 'tool_call'}
                  <div><ToolCallBlock {item} /></div>
                {:else if item.kind === 'delegation'}
                  <div><DelegationCard {item} onViewSession={handleViewSession} /></div>
                {:else if item.kind === 'workflow_composed'}
                  <div><WorkflowComposedCard {item} /></div>
                {:else if item.kind === 'compaction'}
                  <div><CompactionCard {item} onViewPreviousSession={handleViewSession} /></div>
                {:else if item.kind === 'system_message'}
                  <p class="py-1 text-center text-xs italic text-slate-500 whitespace-pre-line">{item.text}</p>
                {:else}
                  <article class={`rounded-3xl border px-4 py-4 text-sm shadow-card ${item.tone === 'warning' ? 'border-sky-500/30 bg-sky-500/10 text-sky-100' : item.tone === 'error' ? 'border-rose-500/30 bg-rose-500/10 text-rose-100' : 'border-slate-700 bg-slate-900 text-slate-200'}`}>
                    <h3 class="font-semibold">{item.title}</h3>
                    <p class="mt-2 leading-6">{item.description}</p>
                  </article>
                {/if}
              {/each}
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
                <LiveDots />
              </div>
            {/if}
          </div>

          <!-- Scroll to bottom button -->
          {#if userScrolledUp}
            <button
              class="sticky bottom-2 left-1/2 z-10 -translate-x-1/2 rounded-full border border-slate-700 bg-slate-900/90 p-2 shadow-lg transition hover:bg-slate-800"
              onclick={jumpToBottom}
              type="button"
              title="Scroll to bottom"
            >
              <ArrowDown class="h-4 w-4 text-slate-300" />
            </button>
          {/if}
        </div>

        <div bind:this={footerChromeEl} class="shrink-0 space-y-3 bg-slate-950/95 backdrop-blur">
          {#if shouldShowChatTodoDrawer}
            <div class="rounded-xl border border-slate-800/60 bg-slate-900/40">
              <button
                class="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-sm transition hover:bg-slate-800/40"
                onclick={() => { chatTodoDrawerOpen = !chatTodoDrawerOpen; }}
                type="button"
              >
                <span class="truncate text-slate-300">
                  <span class="font-medium text-slate-200">Todos</span>
                  <span class="text-slate-500"> · {activeChatTodos.length} active{#if chatTodoCounts.inProgress > 0} · {chatTodoCounts.inProgress} in progress{/if}{#if chatTodoCounts.pending > 0} · {chatTodoCounts.pending} pending{/if}</span>
                </span>
                {#if chatTodoDrawerOpen}
                  <ChevronUp class="h-3.5 w-3.5 shrink-0 text-slate-500" />
                {:else}
                  <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
                {/if}
              </button>
              {#if chatTodoDrawerOpen}
                <ul class="divide-y divide-slate-800/40 border-t border-slate-800/60">
                  {#each chatTodos as todo}
                    <li class="flex items-center gap-2 px-3 py-1.5 text-sm text-slate-200">
                      <span
                        class={`inline-block h-2 w-2 shrink-0 rounded-full ${todoStatusDot(todo.status)}`}
                        aria-label={todo.status.replace('_', ' ')}
                        title={todo.status.replace('_', ' ')}
                      ></span>
                      <span class="min-w-0 flex-1 truncate">{todo.content}</span>
                      {#if todo.priority !== 'medium'}
                        <span class={`shrink-0 text-xs ${todoPriorityClass(todo.priority)}`}>{todo.priority}</span>
                      {/if}
                    </li>
                  {/each}
                </ul>
              {/if}
            </div>
          {/if}

          <!-- Composer or read-only banner -->
          {#if currentConversation && !isWebConversation(currentConversation)}
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
            <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-center text-sm text-sky-100">
              {#if activeSessionStatus() === 'suspended'}
                This session is suspended.
              {:else if activeSessionStatus() === 'terminated'}
                This session has been terminated.
              {:else}
                This session has ended ({activeSessionStatus()}).
              {/if}
            </div>
          {:else}
          <!--
            Composer: sits flush at the bottom of the viewport and spans
            the full width of the chat area. No outer card, no recessed
            background — the textarea below carries its own border. The
            visualViewport-sized app shell moves the footer above the
            keyboard; `--app-bottom-inset` now stays at 0 so the pill
            also sits flush with the bottom edge in standalone PWA mode.
            On tablet and up we keep a small padding so the pill never
            bleeds into the window chrome.
          -->
          <form class="shrink-0 space-y-2 border-t border-slate-800/60 px-3 pt-3 pb-[var(--app-bottom-inset,0px)] sm:space-y-3 sm:px-5 sm:py-4 sm:pb-[calc(0.5rem+var(--app-bottom-inset,0px))]" onsubmit={(event) => { event.preventDefault(); void handleSend(); }}>
            <!-- Slash command suggestions dropdown -->
            {#if slashSuggestionsVisible}
              <div class="mb-1 rounded-xl border border-slate-700 bg-slate-900/95 py-1 text-sm shadow-lg">
                {#each slashFilteredSuggestions as suggestion, i}
                  <button
                    class="flex w-full items-center gap-3 px-3 py-1.5 text-left text-xs transition {i === slashSelectedIndex ? 'bg-slate-700/60 text-slate-100' : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'}"
                    onmousedown={(e: MouseEvent) => { e.preventDefault(); acceptSlashSuggestion(i); }}
                    type="button"
                  >
                    <span class="font-mono font-medium text-sky-400">{suggestion.command}</span>
                    <span class="opacity-70">{suggestion.description}</span>
                  </button>
                {/each}
              </div>
            {/if}

            {#if pendingDirectQuestion}
              <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-50">
                <div class="flex items-center justify-between gap-3">
                  <p class="font-semibold">Assistant requested more input</p>
                  {#if directQuestionSubmitting}
                    <LiveDots inline={true} size="sm" label="Answering" />
                  {/if}
                </div>
                <p class="mt-1 leading-6">{pendingDirectQuestion.question}</p>
                {#if pendingDirectQuestion.context}
                  <p class="mt-2 text-xs text-sky-100/80">{pendingDirectQuestion.context}</p>
                {/if}
                {#if pendingDirectQuestion.options.length > 0}
                  <div class="mt-3 flex flex-wrap gap-2">
                    {#each pendingDirectQuestion.options as option}
                      <button
                        class="rounded-full border border-sky-400/30 bg-sky-400/10 px-3 py-1 text-xs text-sky-100 transition hover:bg-sky-400/20"
                        type="button"
                        disabled={directQuestionSubmitting}
                        onclick={() => { composer = option; syncComposerHeight(); focusActiveComposer(); }}
                      >
                        {option}
                      </button>
                    {/each}
                  </div>
                {/if}
              </div>
            {/if}
            <ComposerAttachments
              attachments={composerAttachments}
              onremove={removeAttachment}
              disabled={directQuestionSubmitting}
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
              * The trailing icon is context-sensitive: a stop square
                while a turn is streaming, a send arrow when there is
                content to submit, and nothing when the field is
                empty.
              * Enter defaults to newline; Cmd/Ctrl+Enter always
                submits, and the stored Enter-to-send preference still
                applies for users who opted in.
            -->
            <div class="flex items-center gap-1 rounded-3xl border border-slate-700 bg-transparent px-2 py-1 transition focus-within:border-sky-400/50 focus-within:ring-2 focus-within:ring-sky-300/20">
              <label
                aria-label="Attach files"
                class={`inline-flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-200 focus-within:bg-slate-800/60 focus-within:text-slate-200 ${directQuestionSubmitting ? 'pointer-events-none opacity-40' : ''}`}
              >
                <Paperclip class="h-4 w-4 pointer-events-none" />
                <input
                  class="sr-only"
                  type="file"
                  multiple
                  disabled={directQuestionSubmitting}
                  onchange={(event) => {
                    const files = (event.currentTarget as HTMLInputElement).files;
                    if (!files || files.length === 0) return;
                    void uploadFiles(Array.from(files));
                    (event.currentTarget as HTMLInputElement).value = '';
                  }}
                />
              </label>
              <textarea
                bind:this={composerElement}
                bind:value={composer}
                rows={1}
                class="min-h-[36px] max-h-[200px] flex-1 resize-none self-center bg-transparent px-1 py-[0.4rem] text-[16px] leading-5 text-slate-100 placeholder:text-slate-500 focus:outline-none focus-visible:outline-none focus-visible:ring-0 focus-visible:ring-offset-0 sm:text-sm"
                disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup() || directQuestionSubmitting}
                enterkeyhint={enterToSend ? 'send' : 'enter'}
                autocapitalize="sentences"
                spellcheck="true"
                onkeydown={handleComposerKeydown}
                oninput={() => { updateSlashSuggestions(); syncComposerHeight(); }}
                onpaste={(event) => void handlePaste(event)}
                placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : pendingDirectQuestion ? 'Answer the pending clarification request...' : `Message ${currentAgentDisplayName}`}
              ></textarea>
              {#if turnInProgress}
                <button
                  type="button"
                  aria-label="Cancel turn"
                  title="Cancel turn"
                  class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-800 text-slate-200 transition hover:bg-slate-700"
                  onclick={() => currentConversation && wsClient.cancelTurn(currentConversation.conversation_id)}
                >
                  <Square class="h-3 w-3 fill-current" />
                </button>
              {:else if canSendNow}
                <button
                  type="submit"
                  aria-label={pendingDirectQuestion ? 'Answer' : 'Send'}
                  title={pendingDirectQuestion ? 'Answer' : 'Send'}
                  class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-sky-500 text-slate-950 transition hover:bg-sky-400 disabled:opacity-50"
                  disabled={directQuestionSubmitting}
                >
                  <ArrowUp class="h-4 w-4" stroke-width="2.5" />
                </button>
              {/if}
            </div>
          </form>
          {/if}
        </div>
      </div>

      {#if showNewChatModal}
        <NewChatModal
          agents={agents}
          bind:selectedAgentId={newChatAgentId}
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
                <LiveDots inline={true} size="sm" label="Following latest" />
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
                {#if subIntention.title || subIntention.intention}
                  <div class="mb-2">
                    <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Intention</p>
                    {#if subIntention.title}
                      <p class="mt-0.5 text-sm font-medium text-white">{subIntention.title}</p>
                    {/if}
                    {#if subIntention.intention}
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

          <div class="flex-1 overflow-y-auto px-4 py-4 space-y-4">
            {#if subSessionLoading}
              <LoadingState />
            {:else if subSessionError}
              <p class="text-sm text-rose-400">{subSessionError}</p>
            {:else if subSessionTimeline.length === 0}
              <p class="text-sm text-slate-500">No events recorded yet.</p>
            {:else}
              {#each subSessionTimeline as item (item.id)}
                {#if item.kind === 'message'}
                  <div class={`flex min-w-0 ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                    <ChatMessage {item} agent={currentConversation ? conversationAgent(currentConversation) ?? null : null} />
                  </div>
                {:else if item.kind === 'thinking'}
                  <ThinkingBlock item={item as ThinkingTimelineItem} />
                {:else if item.kind === 'tool_call'}
                  <ToolCallBlock {item} />
                {:else if item.kind === 'delegation'}
                  <DelegationCard {item} />
                {:else if item.kind === 'workflow_composed'}
                  <WorkflowComposedCard {item} />
                {:else if item.kind === 'system_message'}
                  <p class="py-1 text-center text-xs italic text-slate-500 whitespace-pre-line">{item.text}</p>
                {:else if item.kind === 'notice'}
                  <div class="rounded-xl border border-slate-800/60 bg-slate-900/50 px-3 py-2 text-xs text-slate-400">
                    <p class="font-medium">{item.title}</p>
                    {#if item.description}<p class="mt-1 opacity-75">{item.description}</p>{/if}
                  </div>
                {/if}
              {/each}
            {/if}
          </div>
        </aside>
      {/if}
    </section>
  </div>
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
</style>
