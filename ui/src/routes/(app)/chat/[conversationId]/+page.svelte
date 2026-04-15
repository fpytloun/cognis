<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount, tick } from 'svelte';
  import { fade } from 'svelte/transition';
  import { ArrowDown, ArrowLeft, Check, ChevronDown, ChevronUp, ChevronsLeft, ChevronsRight, Copy, Info, Maximize2, Minimize2, Paperclip, Search, X } from 'lucide-svelte';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AgentProfilePopover from '$lib/components/AgentProfilePopover.svelte';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import CompactionCard from '$lib/components/CompactionCard.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ReasoningBlock from '$lib/components/ReasoningBlock.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { api, asApiError } from '$lib/api/client';
  import {
    getConversationRetryScope,
    getNextHistoryAfterSeq,
    isCurrentConversationLoad,
    nextConversationLoadId,
    nextPollDelayMs,
    SESSION_LOG_BOOTSTRAP_MAX_PAGES,
    SESSION_LOG_PAGE_SIZE,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { onCancelActiveTurnRequest, onChatComposerFocusRequest } from '$lib/shortcuts';
  import { isSupported as notificationsSupported, isGranted as notificationsGranted, requestPermission, notifyIfHidden, hasAskedPermission } from '$lib/notifications';
  import { workspaceHealth } from '$lib/system';
  import {
    appendOptimisticUserMessage,
    applyWebSocketEvent,
    finalizeReasoningItems,
    normalizeHistory,
    type TimelineItem
  } from '$lib/chat';
  import type { Agent, AttachmentRef, ContextUsage, Conversation, Escalation, MessageEvent, Session } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';

  let initializing = $state(true);
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
  let composer = $state('');
  let composerElement = $state<HTMLTextAreaElement | null>(null);
  let expandedComposerElement = $state<HTMLTextAreaElement | null>(null);
  let attachmentInput = $state<HTMLInputElement | null>(null);
  let composerAttachments = $state<AttachmentRef[]>([]);
  let composerExpanded = $state(false);
  let showDropZone = $state(false);
  let dragCounter = 0;
  let selectedAgentId = $state('');
  let archivingConversation = $state(false);
  let deletingConversation = $state(false);
  let mobileListOpen = $state(false);
  let mobileFilterOpen = $state(false);
  let mobileHeaderDetailsOpen = $state(false);
  let enterToSend = $state(true);
  let queuedCount = $state(0);
  let timeline = $state<TimelineItem[]>([]);

  let visibleStartIndex = $state(0);
  let activeConversationId = '';
  const escalationTimeoutSeconds = 300;
  let escalations = $state<Escalation[]>([]);
  let escalationBusyCallId = $state<string | null>(null);
  let escalationError = $state('');
  let escalationCountdownTimer: number | null = null;
  let awaitingAssistantStart = $state(false);
  let turnInProgress = $state(false);
  let lastSubmittedMessage = '';
  let lastRecoverableMessage = $state('');
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
  let userScrolledUp = $state(false);
  let programmaticScroll = false;
  let selectedChannel = $state('all');
  let chatSidebarCollapsed = $state(false);
  interface SessionInfoData {
    intention: string | null;
    status: string;
    total_calls: number;
    approved_count: number;
    denied_count: number;
    escalated_count: number;
  }
  let sessionInfoOpen = $state(false);
  let sessionInfo = $state<SessionInfoData | null>(null);
  let sessionInfoLoading = $state(false);
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
  let pendingDirectQuestion = $state<PendingDirectQuestion | null>(null);

  const sessionIds = new Set<string>();

  let unsubscribeWs: (() => void) | null = null;
  let unsubscribeComposerFocus: (() => void) | null = null;
  let unsubscribeCancelTurn: (() => void) | null = null;
  let visibilityHandler: (() => void) | null = null;
  let conversationLoadRequestId = 0;
  let mobileDrawerPreviouslyFocused: HTMLElement | null = null;

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

  function contextTypeBadge(conversation: Conversation): string {
    const t = conversation.context?.type ?? 'unknown';
    return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase();
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

  async function loadConversationPage(reset = false): Promise<void> {
    const channelFilter = selectedChannel !== 'all' ? selectedChannel : null;
    const agentFilter = selectedAgentId !== 'all' ? selectedAgentId : null;
    const response = await api.conversations.list(reset ? null : conversationCursor, {
      contextType: channelFilter,
      agentId: agentFilter,
    });
    conversations = reset ? response.items : [...conversations, ...response.items];
    conversationCursor = response.cursor;
    conversationsHasMore = response.has_more;
  }

  function persistEnterToSendPreference(): void {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem('cognis-chat-enter-to-send', enterToSend ? '1' : '0');
  }

  function restoreEnterToSendPreference(): void {
    if (typeof window === 'undefined') return;
    enterToSend = window.localStorage.getItem('cognis-chat-enter-to-send') !== '0';
  }

  function restoreSelectedAgent(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem('cognis-chat-selected-agent');
    if (stored && agents.some((a) => a.agent_id === stored && a.status === 'active')) {
      selectedAgentId = stored;
    } else {
      selectedAgentId = 'all';
    }
  }

  function persistSelectedAgent(): void {
    if (typeof window === 'undefined' || !selectedAgentId) return;
    if (selectedAgentId === 'all') {
      window.localStorage.removeItem('cognis-chat-selected-agent');
      return;
    }
    window.localStorage.setItem('cognis-chat-selected-agent', selectedAgentId);
  }

  function conversationIdFromRoute(): string {
    return page.params.conversationId ?? '';
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
    const allConversations = await api.conversations.listAll({ agentId: agentFilter, contextType: null });
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
    if (document.hidden) return;
    try {
      const filtered = (await api.notifications.list(currentConversation?.conversation_id ?? null))
        .filter((item) => item.notification_type === 'escalation')
        .filter(
          (item) => sessionIds.size === 0 || item.session_id === null || sessionIds.has(item.session_id)
        )
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
      escalations = filtered;
      escalationError = '';
      startEscalationCountdown();
    } catch (caughtError) {
      escalationError = asApiError(caughtError).message;
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

  function syncVisibleWindow(): void {
    visibleStartIndex = Math.max(0, timeline.length - 100);
  }

  function scrollToBottom(): void {
    if (!timelineEl || userScrolledUp) return;
    programmaticScroll = true;
    requestAnimationFrame(() => {
      if (timelineEl) {
        timelineEl.scrollTop = timelineEl.scrollHeight;
      }
      programmaticScroll = false;
    });
  }

  function handleTimelineScroll(): void {
    if (!timelineEl || programmaticScroll) return;
    const distanceFromBottom = timelineEl.scrollHeight - timelineEl.scrollTop - timelineEl.clientHeight;
    userScrolledUp = distanceFromBottom > 80;
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    programmaticScroll = true;
    if (timelineEl) {
      timelineEl.scrollTop = timelineEl.scrollHeight;
    }
    programmaticScroll = false;
  }

  function channelTypes(): string[] {
    return availableChannelTypes;
  }

  async function persistSelectedChannel(): Promise<void> {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem('cognis-chat-selected-channel', selectedChannel);
    await loadConversationPage(true);
  }

  function restoreSelectedChannel(): void {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem('cognis-chat-selected-channel');
    if (stored) selectedChannel = stored;
  }

  function restoreChatSidebarState(): void {
    if (typeof window === 'undefined') return;
    chatSidebarCollapsed = window.localStorage.getItem('cognis-chat-sidebar-collapsed') === '1';
  }

  function toggleChatSidebar(): void {
    chatSidebarCollapsed = !chatSidebarCollapsed;
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('cognis-chat-sidebar-collapsed', chatSidebarCollapsed ? '1' : '0');
    }
  }

  function openMobileList(): void {
    mobileDrawerPreviouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    mobileListOpen = true;
    document.body.style.overflow = 'hidden';
  }

  function closeMobileList(): void {
    mobileListOpen = false;
    document.body.style.overflow = '';
    mobileDrawerPreviouslyFocused?.focus();
    mobileDrawerPreviouslyFocused = null;
  }

  async function loadSessionInfo(): Promise<void> {
    const sid = currentConversation?.active_session_id;
    if (!sid) return;
    sessionInfoLoading = true;
    try {
      const detail = await api.sessions.intarisDetail(sid);
      sessionInfo = {
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
    options: { reloadSessions?: boolean; reloadHistory?: boolean; resubscribe?: boolean } = {},
  ): Promise<void> {
    const reloadSessions = options.reloadSessions ?? true;
    const reloadHistory = options.reloadHistory ?? true;
    const shouldResubscribe = options.resubscribe ?? false;

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

    if (reloadSessions && sessionResult.status === 'fulfilled') {
      sessions = sessionResult.value;
      resetSessionFilter();
    } else if (reloadSessions && sessionResult.status === 'rejected') {
      sessions = [];
      sessionIds.clear();
      sessionsError = asApiError(sessionResult.reason).message;
    }

    if (reloadHistory && historyResult.status === 'fulfilled') {
      timeline = normalizeHistory(historyResult.value.items);
      syncVisibleWindow();
      userScrolledUp = false;
    } else if (reloadHistory && historyResult.status === 'rejected') {
      timeline = [];
      syncVisibleWindow();
      historyError = asApiError(historyResult.reason).message;
    }

    if (shouldResubscribe) {
      wsClient.subscribeConversation(
        conversationId,
        reloadHistory && historyResult.status === 'fulfilled'
          ? (historyResult.value.active_session_last_seq ?? 0)
          : 0
      );
    }

    if (!sessionsError) {
      await refreshEscalations();
      if (isStaleConversationLoad(requestId)) {
        return;
      }
    } else {
      escalations = [];
      escalationError = '';
    }

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (timelineEl && !userScrolledUp) {
          timelineEl.scrollTop = timelineEl.scrollHeight;
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
    await reloadConversationSubloads(currentConversation.conversation_id, requestId, {
      reloadSessions: retryScope.sessions,
      reloadHistory: retryScope.history,
      resubscribe: false,
    });
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
    mobileHeaderDetailsOpen = false;
    mobileListOpen = false;
    document.body.style.overflow = '';

    if (previousConversationId) {
      wsClient.unsubscribeConversation(previousConversationId);
    }

    try {
      const conversation = await api.conversations.detail(conversationId);
      if (isStaleConversationLoad(requestId)) {
        return;
      }

      activeConversationId = conversationId;
      currentConversation = conversation;
      if (!conversations.some((item) => item.conversation_id === conversation.conversation_id)) {
        conversations = [conversation, ...conversations];
      }
      queuedCount = 0;
      turnInProgress = false;
      awaitingAssistantStart = false;
      pendingDirectQuestion = null;
      lastRecoverableMessage = '';
      editingTitle = false;
      contextUsage = null;
      subSessionPanelOpen = false;

      await reloadConversationSubloads(conversationId, requestId, {
        reloadSessions: true,
        reloadHistory: true,
        resubscribe: true,
      });
      if (isStaleConversationLoad(requestId)) {
        return;
      }

      api.conversations.markRead(conversationId).catch(() => {});
      currentConversation = { ...conversation, has_unread: false };
      const idx = conversations.findIndex((c) => c.conversation_id === conversationId);
      if (idx >= 0) {
        conversations[idx] = { ...conversations[idx], has_unread: false };
        conversations = [...conversations];
      }
    } catch (caughtError) {
      if (isStaleConversationLoad(requestId)) {
        return;
      }
      error = asApiError(caughtError).message;
      currentConversation = null;
      sessions = [];
      timeline = [];
      escalations = [];
      pendingDirectQuestion = null;
      sessionIds.clear();
    } finally {
      if (!isStaleConversationLoad(requestId)) {
        initializing = false;
        switchingConversation = false;
        initialConversationResolved = true;
      }
    }
  }

  async function initialize(): Promise<void> {
    initializing = true;
    error = '';

    try {
      await refreshSidebarData();
      if (!conversationIdFromRoute()) {
        initializing = false;
        initialConversationResolved = true;
      }
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      initializing = false;
      initialConversationResolved = true;
    }

    if (notificationsSupported() && !notificationsGranted() && !hasAskedPermission()) {
      setTimeout(() => { void requestPermission(); }, 5000);
    }
  }

  async function createNewConversation(): Promise<void> {
    const agentId = selectedAgentId !== 'all'
      ? selectedAgentId
      : agents.find((agent) => agent.status === 'active' && agent.agent_type === 'primary')?.agent_id ?? '';

    if (!agentId) {
      error = 'Create or activate an agent before starting a conversation.';
      return;
    }

    persistSelectedAgent();

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
      await refreshSidebarData();
      addToast('Conversation created.', 'success');
      await goto(`/chat/${conversation.conversation_id}`);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create conversation');
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
      currentConversation = await api.conversations.update(currentConversation.conversation_id, { archived: true });
      await refreshSidebarData();
      addToast('Conversation archived.', 'success');
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
      await api.conversations.remove(currentConversation.conversation_id);
      await refreshSidebarData();
      const nextConversation = conversations.find((c) => c.conversation_id !== currentConversation?.conversation_id);
      addToast('Conversation deleted.', 'success');
      await goto(nextConversation ? `/chat/${nextConversation.conversation_id}` : '/chat/new');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete conversation');
    } finally {
      deletingConversation = false;
    }
  }

  async function saveTitle(): Promise<void> {
    if (!currentConversation) return;
    const newTitle = editTitleValue.trim();
    editingTitle = false;
    if (!newTitle || newTitle === (currentConversation.title ?? '').trim()) return;

    try {
      currentConversation = await api.conversations.update(currentConversation.conversation_id, { title: newTitle });
      // Update sidebar
      const idx = conversations.findIndex((c) => c.conversation_id === currentConversation?.conversation_id);
      if (idx >= 0 && currentConversation) {
        conversations[idx] = currentConversation;
        conversations = [...conversations];
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

  /** Slash commands that are handled as system actions, not chat messages. */
  const SYSTEM_SLASH_COMMANDS = ['/approve', '/deny', '/compact', '/summarize', '/new', '/reset', '/clear', '/stop', '/cancel', '/context', '/info', '/lsp', '/model', '/thinking', '/help'];

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
    const val = composer.trimStart();
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
    if (composerExpanded) {
      expandedComposerElement?.focus();
      return;
    }
    composerElement?.focus();
  }

  async function handleSend(): Promise<void> {
    const content = composer.trim();
    if ((!content && composerAttachments.length === 0) || !currentConversation || isReadOnly(currentConversation)) return;
    const shouldRestoreInlineFocus = composerExpanded;

    const isSlashCommand = SYSTEM_SLASH_COMMANDS.some((cmd) => content.startsWith(cmd));

    if (pendingDirectQuestion && !isSlashCommand && composerAttachments.length > 0) {
      addToast('Attachments are not supported for clarification responses.', 'error');
      return;
    }

    if (!isSlashCommand) {
      timeline = appendOptimisticUserMessage(timeline, content, composerAttachments);
      lastSubmittedMessage = content;
      lastRecoverableMessage = '';
      turnInProgress = true;
      awaitingAssistantStart = true;
    }
    error = '';
    composerExpanded = false;
    composer = '';
    syncComposerHeight();
    if (shouldRestoreInlineFocus) {
      await tick();
      syncComposerHeight();
      composerElement?.focus();
    }
    const attachments = [...composerAttachments];
    composerAttachments = [];
    syncVisibleWindow();
    userScrolledUp = false;
    scrollToBottom();
    if (pendingDirectQuestion && !isSlashCommand) {
      wsClient.respondStepQuestion(
        pendingDirectQuestion.notificationId,
        content,
        pendingDirectQuestion.stepName
      );
      pendingDirectQuestion = null;
      return;
    }
    wsClient.sendMessage(currentConversation.conversation_id, content, attachments);
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

  async function handleAttachmentSelect(event: Event): Promise<void> {
    const files = (event.currentTarget as HTMLInputElement).files;
    if (!files || files.length === 0) return;
    await uploadFiles(Array.from(files));
    if (attachmentInput) attachmentInput.value = '';
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

  async function openExpandedComposer(): Promise<void> {
    if (composerExpanded) return;
    composerExpanded = true;
    await tick();
    expandedComposerElement?.focus();
  }

  async function closeExpandedComposer(): Promise<void> {
    if (!composerExpanded) return;
    composerExpanded = false;
    await tick();
    syncComposerHeight();
    composerElement?.focus();
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
    if (!enterToSend || event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    void handleSend();
  }

  function handleExpandedComposerKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape' && slashSuggestionsVisible) {
      event.preventDefault();
      slashSuggestionsVisible = false;
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      void closeExpandedComposer();
      return;
    }
    handleComposerKeydown(event);
  }

  function handleExpandedComposerOverlayKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      void closeExpandedComposer();
    }
  }

  async function handleEscalationDecision(callId: string, decision: 'approve' | 'deny'): Promise<void> {
    escalationBusyCallId = callId;
    wsClient.resolveEscalation(callId, decision);
  }

  function escalationSecondsRemaining(esc: Escalation): number {
    const timeout = esc.timeout_seconds ?? escalationTimeoutSeconds;
    const elapsed = (Date.now() - (esc.received_at ?? Date.now())) / 1000;
    return Math.max(0, Math.ceil(timeout - elapsed));
  }

  function loadOlder(): void {
    visibleStartIndex = Math.max(0, visibleStartIndex - 50);
  }

  function handleSocketEvent(event: import('$lib/types/api').CognisWebSocketEvent): void {
    const currentId = conversationIdFromRoute();
    if ('conversation_id' in event && event.conversation_id && event.conversation_id !== currentId) {
      // Event for a different conversation — mark it as unread locally
      // and show a browser notification if appropriate.
      const otherConvId = event.conversation_id;
      if (event.type === 'message_complete' || event.type === 'workflow_completed' || event.type === 'workflow_failed') {
        const idx = conversations.findIndex((c) => c.conversation_id === otherConvId);
        if (idx >= 0 && !conversations[idx].has_unread) {
          conversations[idx] = { ...conversations[idx], has_unread: true };
          conversations = [...conversations];
        }
        // Browser notification
        const convTitle = idx >= 0 ? (conversations[idx].title ?? 'Conversation') : 'Conversation';
        const agentId = idx >= 0 ? conversations[idx].agent_id : '';
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

    // Filter sub-session tool/chunk events from the main timeline (defense-in-depth)
    const rootSid = currentConversation?.active_session_id;
    if (rootSid && 'session_id' in event && event.session_id && event.session_id !== rootSid) {
      if (event.type === 'tool_call' || event.type === 'tool_result' || event.type === 'chunk' || event.type === 'reasoning') {
        return;
      }
    }

    if (event.type === 'queued' || event.type === 'message_complete') {
      queuedCount = event.queued_count;
    }

    if (event.type === 'error') {
      error = socketErrorMessage(event);
      awaitingAssistantStart = false;
      turnInProgress = false;
      if (event.code === 'turn_cancelled') {
        pendingDirectQuestion = null;
      }
      if (event.recoverable) {
        lastRecoverableMessage = lastSubmittedMessage;
      }
      return;
    }

    if (event.type === 'chunk' || event.type === 'tool_call' || event.type === 'delegation_started' || event.type === 'reasoning') {
      awaitingAssistantStart = false;
    }

    if (event.type === 'message_complete' || event.type === 'workflow_completed' || event.type === 'workflow_failed' || event.type === 'workflow_cancelled') {
      awaitingAssistantStart = false;
      turnInProgress = false;
      pendingDirectQuestion = null;
      timeline = finalizeReasoningItems(timeline);
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
      if (!existing) {
        escalations = [...escalations, {
          call_id: event.call_id,
          session_id: event.session_id ?? null,
          tool_name: event.tool_name,
          decision: 'escalate',
          resolved: false,
          reasoning: event.reasoning,
          risk: event.risk,
          timeout_seconds: event.timeout_seconds,
          received_at: Date.now(),
        }];
        startEscalationCountdown();
        scrollToBottom();
      }
      return;
    }

    if (event.type === 'escalation_resolved') {
      escalations = escalations.filter((e) => e.call_id !== event.call_id);
      escalationBusyCallId = null;
      if (escalations.length === 0) stopEscalationCountdown();
      return;
    }

    // Handle conversation_updated for title changes
    if (event.type === 'conversation_updated') {
      if (currentConversation && event.conversation_id === currentConversation.conversation_id) {
        if (typeof event.title === 'string') {
          currentConversation = { ...currentConversation, title: event.title };
          const idx = conversations.findIndex((c) => c.conversation_id === currentConversation?.conversation_id);
          if (idx >= 0 && currentConversation) {
            conversations[idx] = { ...conversations[idx], title: event.title };
            conversations = [...conversations];
          }
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
      timeline = [];
      syncVisibleWindow();
      // Refresh session list
      if (currentConversation) {
        api.conversations.sessions(currentConversation.conversation_id).then((s) => { sessions = s; }).catch(() => {});
      }
      return;
    }

    // Handle conversation_created: navigate to new conversation
    if (event.type === 'conversation_created') {
      void goto(`/chat/${event.conversation_id}`);
      return;
    }

    if (event.type === 'workflow_step_question' && !event.task_id && event.notification_id) {
      pendingDirectQuestion = {
        notificationId: event.notification_id,
        stepName: event.step_name,
        question: event.question?.trim() || 'The assistant needs more input to continue.',
        options: Array.isArray(event.options)
          ? event.options
              .map((option) => {
                if (typeof option === 'string') return option;
                if (option && typeof option === 'object') {
                  const label = (option as Record<string, unknown>).label;
                  if (typeof label === 'string') return label;
                }
                return '';
              })
              .filter((value) => value.length > 0)
          : [],
        context:
          typeof event.context === 'string'
            ? event.context
            : event.context && typeof event.context === 'object' && typeof event.context.context === 'string'
              ? event.context.context
              : ''
      };
    }

    timeline = applyWebSocketEvent(timeline, event);
    if (event.type !== 'tool_call' && event.type !== 'tool_result' && event.type !== 'reasoning') {
      syncVisibleWindow();
    }

    // Auto-scroll on new content
    if (event.type === 'chunk' || event.type === 'message_complete' || event.type === 'delegation_started' || event.type === 'delegation_completed' || event.type === 'system_message' || event.type === 'user_message') {
      scrollToBottom();
    }

    // No longer polling for escalations — they arrive via push events
  }

  async function handleAgentFilterChange(): Promise<void> {
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
    return list;
  });

  let displayedTimeline = $derived(timeline.slice(visibleStartIndex));

  onMount(() => {
    restoreEnterToSendPreference();
    restoreSelectedChannel();
    restoreChatSidebarState();
    mobileListOpen = !conversationIdFromRoute();
    if (mobileListOpen) {
      document.body.style.overflow = 'hidden';
    }
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
        void refreshEscalations();
      }
    };
    document.addEventListener('visibilitychange', visibilityHandler);

    void initialize();

    return () => {
      document.body.style.overflow = '';
      unsubscribeWs?.();
      unsubscribeComposerFocus?.();
      unsubscribeCancelTurn?.();
      stopEscalationCountdown();
      if (visibilityHandler) {
        document.removeEventListener('visibilitychange', visibilityHandler);
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

{#if initializing}
  <LoadingState label="Loading conversation" description="Fetching history, restoring workflow prompts, and preparing the live stream." />
{:else}
  <div class={`relative flex h-full min-h-0 flex-col gap-3 overflow-hidden ${chatSidebarCollapsed ? '' : 'xl:grid xl:grid-cols-[320px_minmax(0,1fr)] xl:gap-4'}`}>
    {#if mobileListOpen}
      <button
        aria-label="Close conversation list"
        class="fixed inset-0 z-30 bg-slate-950/80 backdrop-blur-sm xl:hidden"
        onclick={closeMobileList}
        type="button"
        transition:fade={{ duration: 180 }}
      ></button>
    {/if}

    <!-- Sidebar -->
    <aside
      aria-label="Conversation list"
      aria-modal={mobileListOpen ? 'true' : undefined}
      class={`fixed inset-y-3 left-3 z-40 flex w-[min(22rem,calc(100vw-1.5rem))] min-h-0 flex-col rounded-[1.75rem] border border-slate-800/80 bg-slate-900/95 shadow-card backdrop-blur transition-transform duration-200 ease-out xl:static xl:z-auto xl:w-auto xl:translate-x-0 xl:rounded-3xl xl:bg-slate-900/70 ${chatSidebarCollapsed ? 'xl:hidden' : 'xl:flex'} ${mobileListOpen || !currentConversation ? 'translate-x-0' : '-translate-x-[120%] pointer-events-none xl:pointer-events-auto'}`}
      role={mobileListOpen ? 'dialog' : undefined}
    >
      <!-- Static top: filters -->
      <div class="shrink-0 space-y-3 p-4 pb-2 sm:p-4">
        <div class="flex items-center justify-between xl:hidden">
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
          <div class={`space-y-3 ${mobileFilterOpen ? 'block' : 'hidden xl:block'}`}>
          <label class="block space-y-1">
            <span class="text-xs font-medium uppercase tracking-widest text-slate-500">Agent</span>
            <select
              bind:value={selectedAgentId}
              onchange={handleAgentFilterChange}
              class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
            >
              <option value="all">All agents</option>
              {#each agents.filter((a) => a.status === 'active' && a.agent_type === 'primary') as agent}
                <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
              {/each}
            </select>
          </label>

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
            onclick={createNewConversation}
            type="button"
          >+ New</button>
        </div>

        <div class="relative">
          <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
          <Input bind:value={conversationSearch} class="pl-9" placeholder="Filter by title" />
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
                class={`flex items-start gap-3 rounded-2xl border px-3 py-2.5 transition ${isActive ? 'border-sky-400/40 bg-sky-500/10' : 'border-transparent bg-slate-950/60 hover:border-slate-700 hover:bg-slate-900'}`}
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

      <!-- Static bottom: collapse button -->
      <div class="hidden shrink-0 border-t border-slate-800/60 p-3 xl:block">
        <button
          class="flex w-full items-center justify-center gap-2 rounded-xl py-1.5 text-xs text-slate-400 transition hover:bg-slate-800 hover:text-white"
          onclick={toggleChatSidebar}
          type="button"
          title="Hide conversations"
        >
          <ChevronsLeft class="h-3.5 w-3.5" />
          <span>Collapse</span>
        </button>
      </div>
    </aside>

    <!-- Main chat area -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <section
      class="relative flex min-h-0 flex-1 flex-col rounded-[1.75rem] border border-slate-800/80 bg-slate-900/70 shadow-card backdrop-blur xl:rounded-3xl"
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
      <!-- Header -->
      <div class="border-b border-slate-800/80 px-3 py-2.5 sm:px-5 sm:py-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2">
              {#if chatSidebarCollapsed}
                <button
                  class="hidden rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-800 hover:text-white xl:inline-flex"
                  onclick={toggleChatSidebar}
                  type="button"
                  title="Show conversations"
                >
                  <ChevronsRight class="h-4 w-4" />
                </button>
              {/if}
              <div class="xl:hidden">
                <Button aria-label="Open conversations" size="sm" variant="secondary" onclick={openMobileList}>
                  <ArrowLeft class="h-4 w-4" />
                </Button>
              </div>
              <!-- Editable title -->
              {#if editingTitle}
                <!-- svelte-ignore a11y_autofocus -->
                <input
                  class="min-w-0 flex-1 rounded-lg border border-sky-500/50 bg-slate-950/80 px-2 py-1 text-lg font-semibold text-white focus:outline-none focus:ring-1 focus:ring-sky-400 sm:text-xl"
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

                <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-slate-400">
                  {contextTypeBadge(currentConversation)}
                </span>

                {#if sessions.length > 1}
                  <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400" title="Sub-sessions">
                    {sessions.length} sessions
                  </span>
                {/if}

                <!-- Context usage badge (right-aligned) -->
                {#if contextUsage}
                  <span class="ml-auto flex items-center gap-1.5 text-[10px] font-medium {contextUsage.percentage > 85 ? 'text-rose-400' : contextUsage.percentage > 60 ? 'text-amber-400' : 'text-slate-400'}" title="Context: {contextUsage.prompt_tokens.toLocaleString()} / {contextUsage.max_context_tokens.toLocaleString()} tokens ({contextUsage.model}){contextUsage.reasoning_effort ? ` | reasoning: ${contextUsage.reasoning_effort}` : ''}">
                    <span class="font-mono">{contextUsage.prompt_tokens.toLocaleString()}</span>
                    <span class="opacity-50">({contextUsage.percentage}%)</span>
                    {#if contextUsage.reasoning_effort}
                      <span class="rounded border border-violet-500/30 px-1 text-violet-400">{contextUsage.reasoning_effort}</span>
                    {/if}
                  </span>
                {/if}
              {:else}
                <span>No active conversation selected</span>
              {/if}
            </div>

            <!-- Session info popover -->
            {#if sessionInfoOpen}
              <div class={`mt-2 rounded-xl border border-slate-700 bg-slate-900/95 px-4 py-3 text-sm ${mobileHeaderDetailsOpen ? '' : 'hidden sm:block'}`}>
                {#if sessionInfoLoading}
                  <p class="text-xs text-slate-500">Loading session details...</p>
                {:else if sessionInfo}
                  {#if sessionInfo.intention}
                    <div class="mb-2">
                      <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Intention</p>
                      <p class="mt-0.5 text-sm text-slate-200">{sessionInfo.intention}</p>
                    </div>
                  {/if}
                  <div class="flex flex-wrap gap-3 text-xs text-slate-400">
                    <span>Status: <span class="text-slate-200">{sessionInfo.status}</span></span>
                    <span>Calls: <span class="text-slate-200">{sessionInfo.total_calls}</span></span>
                    <span class="text-emerald-400">{sessionInfo.approved_count} approved</span>
                    <span class="text-rose-400">{sessionInfo.denied_count} denied</span>
                    <span class="text-amber-400">{sessionInfo.escalated_count} escalated</span>
                  </div>
                {:else}
                  <p class="text-xs text-slate-500">Unable to load session details.</p>
                {/if}
              </div>
            {/if}
          </div>

          <div class="flex items-center gap-2">
            <button
              class="inline-flex rounded-lg border border-slate-700 px-2 py-1.5 text-xs text-slate-300 transition hover:bg-slate-800 hover:text-white sm:hidden"
              onclick={() => (mobileHeaderDetailsOpen = !mobileHeaderDetailsOpen)}
              type="button"
              aria-label="Toggle conversation details"
            >
              {#if mobileHeaderDetailsOpen}
                <ChevronUp class="h-4 w-4" />
              {:else}
                <ChevronDown class="h-4 w-4" />
              {/if}
            </button>
            <div class="hidden flex-wrap gap-2 sm:flex">
              <button
                class="flex items-center gap-1 text-xs text-slate-500 transition hover:text-sky-300"
                onclick={() => { sessionInfoOpen = !sessionInfoOpen; if (sessionInfoOpen && !sessionInfo) void loadSessionInfo(); }}
                type="button"
                title="Session details"
              >
                <Info class="h-3.5 w-3.5" />
              </button>
              <Button size="sm" variant="secondary" disabled={!currentConversation || archivingConversation} onclick={archiveConversation}>
                {archivingConversation ? 'Archiving...' : 'Archive'}
              </Button>
              <Button size="sm" variant="danger" disabled={!currentConversation || deletingConversation} onclick={deleteConversation}>
                {deletingConversation ? 'Deleting...' : 'Delete'}
              </Button>
            </div>
          </div>
        </div>

        {#if mobileHeaderDetailsOpen && currentConversation}
          {@const agent = conversationAgent(currentConversation)}
          <div class="mt-3 space-y-3 rounded-2xl border border-slate-800/80 bg-slate-950/50 px-3 py-3 sm:hidden">
            <div class="flex flex-wrap items-center gap-2 text-sm text-slate-300">
              {#if agent}
                <div class="flex items-center gap-2 rounded-lg bg-slate-900/80 px-2 py-1">
                  <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url ?? null} class="h-5 w-5" />
                  <span>{agent.display_name ?? agent.name}</span>
                </div>
              {/if}
              <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-slate-400">
                {contextTypeBadge(currentConversation)}
              </span>
              {#if sessions.length > 1}
                <span class="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 text-[10px] font-medium text-slate-400">{sessions.length} sessions</span>
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
            {#if contextUsage}
              <div class="text-[10px] text-slate-400">
                Context {contextUsage.prompt_tokens.toLocaleString()} / {contextUsage.max_context_tokens.toLocaleString()} ({contextUsage.percentage}%)
              </div>
            {/if}
            <div class="flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" onclick={() => { sessionInfoOpen = !sessionInfoOpen; if (sessionInfoOpen && !sessionInfo) void loadSessionInfo(); }}>
                Session details
              </Button>
              <Button size="sm" variant="secondary" disabled={!currentConversation || archivingConversation} onclick={archiveConversation}>
                {archivingConversation ? 'Archiving...' : 'Archive'}
              </Button>
              <Button size="sm" variant="danger" disabled={!currentConversation || deletingConversation} onclick={deleteConversation}>
                {deletingConversation ? 'Deleting...' : 'Delete'}
              </Button>
            </div>
          </div>
        {/if}
      </div>

      <!-- Message area + composer -->
      <div class="flex min-h-0 flex-1 flex-col gap-3 p-3 sm:gap-4 sm:p-4">
        {#if isMemoryDegraded()}
          <div class="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
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
              <p>{error}</p>
              {#if lastRecoverableMessage}
                <Button size="sm" variant="secondary" onclick={retryLastTurn}>Retry</Button>
              {/if}
            </div>
          </div>
        {/if}

        {#if sessionsError}
          <div class="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-3 py-3 text-sm text-amber-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Session details are temporarily unavailable: {sessionsError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry</Button>
            </div>
          </div>
        {/if}

        {#if historyError}
          <div class="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Conversation history is temporarily unavailable: {historyError}</p>
              <Button size="sm" variant="secondary" onclick={retryConversationSubloads}>Retry history</Button>
            </div>
          </div>
        {/if}

        {#if escalationError}
          <div class="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>Escalation updates are temporarily unavailable: {escalationError}</p>
              <Button size="sm" variant="secondary" onclick={() => refreshEscalations()}>Refresh escalations</Button>
            </div>
          </div>
        {/if}

        <!-- Timeline -->
        <div
          class="relative min-h-0 flex-1 space-y-3 overflow-y-auto p-2 sm:p-4"
          bind:this={timelineEl}
          onscroll={handleTimelineScroll}
        >
          {#if visibleStartIndex > 0}
            <div class="flex justify-center">
              <Button size="sm" variant="secondary" onclick={loadOlder}>Load older messages</Button>
            </div>
          {/if}

          {#if displayedTimeline.length === 0}
            <p class="rounded-2xl border border-dashed border-slate-700 px-4 py-10 text-center text-sm text-slate-400">
              Send the first message to start this conversation.
            </p>
          {:else}
            {#each displayedTimeline as item (item.id)}
              {#if item.kind === 'message'}
                <div class={`flex min-w-0 ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <ChatMessage {item} />
                </div>
              {:else if item.kind === 'tool_call'}
                <ToolCallBlock {item} />
              {:else if item.kind === 'reasoning'}
                <ReasoningBlock {item} />
              {:else if item.kind === 'delegation'}
                <DelegationCard {item} onViewSession={handleViewSession} />
              {:else if item.kind === 'compaction'}
                <CompactionCard {item} onViewPreviousSession={handleViewSession} />
              {:else if item.kind === 'system_message'}
                <p class="py-1 text-center text-xs italic text-slate-500 whitespace-pre-line">{item.text}</p>
              {:else}
                <article class={`rounded-3xl border px-4 py-4 text-sm shadow-card ${item.tone === 'warning' ? 'border-amber-500/30 bg-amber-500/10 text-amber-100' : item.tone === 'error' ? 'border-rose-500/30 bg-rose-500/10 text-rose-100' : 'border-slate-700 bg-slate-900 text-slate-200'}`}>
                  <h3 class="font-semibold">{item.title}</h3>
                  <p class="mt-2 leading-6">{item.description}</p>
                </article>
              {/if}
            {/each}
          {/if}

          <!-- Escalation prompts (sequential: show one at a time) -->
          {#if escalations.length > 0}
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

          {#if turnInProgress && awaitingAssistantStart}
            <div class="flex items-center gap-3 px-2 py-2">
              <div class="flex items-center gap-1.5 rounded-2xl border border-slate-800 bg-slate-900/80 px-4 py-2.5">
                <span class="h-2 w-2 animate-bounce rounded-full bg-sky-400 [animation-delay:0ms]"></span>
                <span class="h-2 w-2 animate-bounce rounded-full bg-sky-400 [animation-delay:150ms]"></span>
                <span class="h-2 w-2 animate-bounce rounded-full bg-sky-400 [animation-delay:300ms]"></span>
              </div>
            </div>
          {/if}

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

        <!-- Composer or read-only banner -->
        {#if currentConversation && !isWebConversation(currentConversation)}
          <div class="rounded-2xl border border-slate-700/60 bg-slate-900/60 px-4 py-3 text-center text-sm text-slate-400">
            This conversation is from <span class="font-medium text-slate-300">{contextTypeBadge(currentConversation)}</span>. Read-only in web UI.
          </div>
        {:else if currentConversation && currentConversation.status !== 'active'}
          <div class="rounded-2xl border border-slate-700/60 bg-slate-900/60 px-4 py-3 text-center text-sm text-slate-400">
            This conversation is archived.
          </div>
        {:else if isSessionBlocked()}
          <div class="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-center text-sm text-amber-100">
            {#if activeSessionStatus() === 'suspended'}
              This session is suspended.
            {:else if activeSessionStatus() === 'terminated'}
              This session has been terminated.
            {:else}
              This session has ended ({activeSessionStatus()}).
            {/if}
          </div>
        {:else}
          <form class="shrink-0 space-y-3 rounded-[1.5rem] border border-slate-800/80 bg-slate-900/90 p-3 sm:rounded-3xl sm:p-4" onsubmit={(event) => { event.preventDefault(); void handleSend(); }}>
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
                <p class="font-semibold">Assistant requested more input</p>
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
                        onclick={() => { composer = option; syncComposerHeight(); focusActiveComposer(); }}
                      >
                        {option}
                      </button>
                    {/each}
                  </div>
                {/if}
              </div>
            {/if}
            {#if composerAttachments.length > 0}
              <div class="flex flex-wrap gap-2">
                {#each composerAttachments as attachment}
                  <div class="flex items-center gap-2 rounded-full border border-slate-700 bg-slate-950/70 px-3 py-2 text-xs text-slate-200">
                    <span class="truncate max-w-[220px]">{attachment.filename}</span>
                    <button type="button" class="text-slate-400 hover:text-white" onclick={() => removeAttachment(attachment.artifact_id)} aria-label="Remove attachment">
                      <X class="h-3.5 w-3.5" />
                    </button>
                  </div>
                {/each}
              </div>
            {/if}
            <div class="relative">
              <textarea
                bind:this={composerElement}
                bind:value={composer}
                class="min-h-[56px] w-full resize-none rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 pr-12 text-sm text-slate-100 placeholder:text-slate-500"
                disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()}
                onkeydown={handleComposerKeydown}
                oninput={() => { updateSlashSuggestions(); syncComposerHeight(); }}
                onpaste={(event) => void handlePaste(event)}
                placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : pendingDirectQuestion ? 'Answer the pending clarification request...' : 'Send a message to Cognis...'}
              ></textarea>
              <button
                type="button"
                class="absolute bottom-3 right-3 inline-flex h-7 w-7 items-center justify-center rounded-full border border-slate-700 bg-slate-900/90 text-slate-400 transition hover:border-slate-600 hover:text-slate-100 disabled:opacity-50"
                title="Expand composer"
                aria-label="Expand composer"
                disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()}
                onclick={() => void openExpandedComposer()}
              >
                <Maximize2 class="h-3.5 w-3.5" />
              </button>
            </div>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <label class="hidden items-center gap-2 text-xs text-slate-400 sm:flex">
                <input bind:checked={enterToSend} class="h-4 w-4 rounded border-slate-700 bg-slate-950" onchange={persistEnterToSendPreference} type="checkbox" />
                <span>Press Enter to send</span>
              </label>
              <div class="flex flex-wrap justify-end gap-2">
                <input bind:this={attachmentInput} class="hidden" type="file" multiple onchange={(event) => void handleAttachmentSelect(event)} />
                <Button size="sm" variant="secondary" type="button" onclick={() => attachmentInput?.click()}>
                  <Paperclip class="h-4 w-4 sm:mr-2" /> <span class="hidden sm:inline">Attach</span>
                </Button>
                {#if turnInProgress}
                  <Button size="sm" variant="secondary" type="button" onclick={() => currentConversation && wsClient.cancelTurn(currentConversation.conversation_id)}>
                    Cancel turn
                  </Button>
                {/if}
                {#if pendingDirectQuestion}
                  <Button size="sm" variant="secondary" type="button" onclick={() => { composer = '/stop'; void handleSend(); }}>
                    Stop
                  </Button>
                {/if}
                <Button size="sm" type="submit" disabled={(!composer.trim() && composerAttachments.length === 0) || !currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()}>
                  {pendingDirectQuestion ? 'Answer' : 'Send'}
                </Button>
              </div>
            </div>
          </form>
        {/if}
      </div>

      {#if composerExpanded}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <div class="fixed inset-0 z-[85] flex items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur" role="presentation" tabindex="-1" onclick={() => void closeExpandedComposer()} onkeydown={handleExpandedComposerOverlayKeydown}>
          <div
            class="w-full max-w-5xl rounded-3xl border border-slate-800 bg-slate-950 shadow-card"
            role="dialog"
            aria-modal="true"
            aria-label="Expanded chat composer"
            tabindex="-1"
            onclick={(event) => event.stopPropagation()}
          >
            <div class="flex items-center justify-between border-b border-slate-800 px-5 py-4">
              <div>
                <p class="text-sm font-semibold text-white">Expanded Composer</p>
                <p class="mt-1 text-xs text-slate-400">Use this space for longer prompts and structured notes.</p>
              </div>
              <button
                type="button"
                class="inline-flex h-9 w-9 items-center justify-center rounded-full border border-slate-800 text-slate-400 transition hover:border-slate-700 hover:text-slate-100"
                title="Collapse composer"
                aria-label="Collapse composer"
                onclick={() => void closeExpandedComposer()}
              >
                <Minimize2 class="h-4 w-4" />
              </button>
            </div>
            <div class="space-y-4 px-5 py-5">
              {#if slashSuggestionsVisible}
                <div class="rounded-xl border border-slate-700 bg-slate-900/95 py-1 text-sm shadow-lg">
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
              {#if composerAttachments.length > 0}
                <div class="flex flex-wrap gap-2">
                  {#each composerAttachments as attachment}
                    <div class="flex items-center gap-2 rounded-full border border-slate-700 bg-slate-900/80 px-3 py-2 text-xs text-slate-200">
                      <span class="max-w-[320px] truncate">{attachment.filename}</span>
                      <button type="button" class="text-slate-400 hover:text-white" onclick={() => removeAttachment(attachment.artifact_id)} aria-label="Remove attachment">
                        <X class="h-3.5 w-3.5" />
                      </button>
                    </div>
                  {/each}
                </div>
              {/if}
              <textarea
                bind:this={expandedComposerElement}
                bind:value={composer}
                class="min-h-[55vh] w-full resize-none rounded-3xl border border-slate-800 bg-slate-900/60 px-5 py-4 text-sm leading-6 text-slate-100 placeholder:text-slate-500"
                disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()}
                onkeydown={handleExpandedComposerKeydown}
                oninput={updateSlashSuggestions}
                onpaste={(event) => void handlePaste(event)}
                placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : pendingDirectQuestion ? 'Answer the pending clarification request...' : 'Send a longer message to Cognis...'}
              ></textarea>
              <div class="flex flex-wrap items-center justify-between gap-3">
                <label class="hidden items-center gap-2 text-xs text-slate-400 sm:flex">
                  <input bind:checked={enterToSend} class="h-4 w-4 rounded border-slate-700 bg-slate-950" onchange={persistEnterToSendPreference} type="checkbox" />
                  <span>Press Enter to send</span>
                </label>
                <div class="flex flex-wrap justify-end gap-2">
                  <Button size="sm" variant="secondary" type="button" onclick={() => attachmentInput?.click()}>
                    <Paperclip class="h-4 w-4 sm:mr-2" /> <span class="hidden sm:inline">Attach</span>
                  </Button>
                  <Button size="sm" variant="secondary" type="button" onclick={() => void closeExpandedComposer()}>
                    Close
                  </Button>
                  <Button size="sm" type="button" disabled={(!composer.trim() && composerAttachments.length === 0) || !currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()} onclick={() => void handleSend()}>
                    {pendingDirectQuestion ? 'Answer' : 'Send'}
                  </Button>
                </div>
              </div>
            </div>
          </div>
        </div>
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
          <div class="flex items-center gap-3 border-b border-slate-800/80 px-4 py-3">
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
                {#if subSessionInfo.intention}
                  <div class="mb-2">
                    <p class="text-[10px] font-semibold uppercase tracking-widest text-slate-500">Intention</p>
                    <p class="mt-0.5 text-sm text-slate-200">{subSessionInfo.intention}</p>
                  </div>
                {/if}
                <div class="flex flex-wrap gap-3 text-xs text-slate-400">
                  <span>Status: <span class="text-slate-200">{subSessionInfo.status}</span></span>
                  <span>Calls: <span class="text-slate-200">{subSessionInfo.total_calls}</span></span>
                  <span class="text-emerald-400">{subSessionInfo.approved_count} approved</span>
                  <span class="text-rose-400">{subSessionInfo.denied_count} denied</span>
                  <span class="text-amber-400">{subSessionInfo.escalated_count} escalated</span>
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
                    <ChatMessage {item} />
                  </div>
                {:else if item.kind === 'tool_call'}
                  <ToolCallBlock {item} />
                {:else if item.kind === 'reasoning'}
                  <ReasoningBlock {item} />
                {:else if item.kind === 'delegation'}
                  <DelegationCard {item} />
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
