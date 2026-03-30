<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { ArrowDown, ArrowLeft, ChevronsLeft, ChevronsRight, Search, Copy, Check, Info } from 'lucide-svelte';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
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
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { onCancelActiveTurnRequest, onChatComposerFocusRequest } from '$lib/shortcuts';
  import { workspaceHealth } from '$lib/system';
  import {
    appendOptimisticUserMessage,
    applyWebSocketEvent,
    finalizeReasoningItems,
    normalizeHistory,
    type TimelineItem
  } from '$lib/chat';
  import type { Agent, ContextUsage, Conversation, Escalation, MessageEvent, Session } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';

  let loading = true;
  let error = '';
  let conversations: Conversation[] = [];
  let conversationCursor: string | null = null;
  let conversationsHasMore = false;
  let conversationSearch = '';
  let agents: Agent[] = [];
  let currentConversation: Conversation | null = null;
  let sessions: Session[] = [];
  let composer = '';
  let composerElement: HTMLTextAreaElement | null = null;
  let selectedAgentId = '';
  let archivingConversation = false;
  let deletingConversation = false;
  let mobileListOpen = false;
  let enterToSend = true;
  let queuedCount = 0;
  let timeline: TimelineItem[] = [];
  let visibleStartIndex = 0;
  let activeConversationId = '';
  let escalationTimeoutSeconds = 300;
  let escalations: Escalation[] = [];
  let escalationBusyCallId: string | null = null;
  let escalationError = '';
  let escalationCountdownTimer: number | null = null;
  let awaitingAssistantStart = false;
  let turnInProgress = false;
  let lastSubmittedMessage = '';
  let lastRecoverableMessage = '';
  let editingTitle = false;
  let editTitleValue = '';
  let sessionIdCopied = false;
  let subSessionPanelOpen = false;
  let subSessionClosing = false;
  let subSessionId = '';
  let subSessionTimeline: TimelineItem[] = [];
  let subSessionLoading = false;
  let subSessionError = '';
  let timelineEl: HTMLDivElement | null = null;
  let userScrolledUp = false;
  let selectedChannel = 'all';
  let chatSidebarCollapsed = false;
  interface SessionInfoData {
    intention: string | null;
    status: string;
    total_calls: number;
    approved_count: number;
    denied_count: number;
    escalated_count: number;
  }
  let sessionInfoOpen = false;
  let sessionInfo: SessionInfoData | null = null;
  let sessionInfoLoading = false;
  let contextUsage: ContextUsage | null = null;
  let subSessionInfoOpen = false;
  let subSessionInfo: SessionInfoData | null = null;
  let subSessionInfoLoading = false;

  const sessionIds = new Set<string>();

  let unsubscribeWs: (() => void) | null = null;
  let unsubscribeComposerFocus: (() => void) | null = null;
  let unsubscribeCancelTurn: (() => void) | null = null;
  let visibilityHandler: (() => void) | null = null;

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

  function rootSessionStatus(): string | null {
    if (!currentConversation?.root_session_id) return null;
    const root = sessions.find((s) => s.session_id === currentConversation?.root_session_id);
    return root?.status ?? null;
  }

  function isSessionBlocked(): boolean {
    const status = rootSessionStatus();
    return status !== null && BLOCKED_SESSION_STATES.has(status);
  }

  function contextTypeBadge(conversation: Conversation): string {
    const t = conversation.context?.type ?? 'unknown';
    return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase();
  }

  function filteredConversations(): Conversation[] {
    let list = conversations;
    if (selectedChannel !== 'all') {
      list = list.filter((c) => (c.context?.type?.toLowerCase() ?? 'unknown') === selectedChannel);
    }
    const query = conversationSearch.trim().toLowerCase();
    if (query) {
      list = list.filter((c) => conversationTitle(c).toLowerCase().includes(query));
    }
    return list;
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
    return event.message;
  }

  async function loadConversationPage(reset = false): Promise<void> {
    const response = await api.conversations.list(reset ? null : conversationCursor);
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
      selectedAgentId = agents.find((a) => a.status === 'active')?.agent_id ?? agents[0]?.agent_id ?? '';
    }
  }

  function persistSelectedAgent(): void {
    if (typeof window === 'undefined' || !selectedAgentId) return;
    window.localStorage.setItem('cognis-chat-selected-agent', selectedAgentId);
  }

  function conversationIdFromRoute(): string {
    return $page.params.conversationId ?? '';
  }

  function conversationTitle(conversation: Conversation): string {
    return conversation.title?.trim() || 'Untitled conversation';
  }

  function conversationAgent(conversation: Conversation): Agent | undefined {
    return agents.find((agent) => agent.agent_id === conversation.agent_id);
  }

  function latestSeq(events: MessageEvent[]): number {
    return events.reduce((max, event) => {
      if (typeof event.seq === 'number') {
        return Math.max(max, event.seq);
      }
      return max;
    }, 0);
  }

  async function loadHistory(conversationId: string): Promise<MessageEvent[]> {
    const events: MessageEvent[] = [];
    let afterSeq = 0;

    while (true) {
      const response = await api.conversations.messages(conversationId, afterSeq, 200);
      events.push(...response.items);
      if (!response.has_more || response.items.length === 0) {
        return events;
      }

      const lastItem = response.items[response.items.length - 1];
      afterSeq = typeof lastItem?.seq === 'number' ? lastItem.seq : afterSeq;
      if (afterSeq === 0) {
        return events;
      }
    }
  }

  async function refreshSidebarData(): Promise<void> {
    [agents] = await Promise.all([api.agents.listAll()]);
    await loadConversationPage(true);
    restoreSelectedAgent();
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
      const allEscalations = await api.escalations.list();
      const filtered = allEscalations.filter(
        (item) => sessionIds.size === 0 || item.session_id === null || sessionIds.has(item.session_id)
      );
      const now = Date.now();
      for (const item of filtered) {
        if (!escalations.some((e) => e.call_id === item.call_id)) {
          item.received_at = now;
          item.timeout_seconds = escalationTimeoutSeconds;
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
    requestAnimationFrame(() => {
      if (timelineEl) {
        timelineEl.scrollTop = timelineEl.scrollHeight;
      }
    });
  }

  function handleTimelineScroll(): void {
    if (!timelineEl) return;
    const distanceFromBottom = timelineEl.scrollHeight - timelineEl.scrollTop - timelineEl.clientHeight;
    userScrolledUp = distanceFromBottom > 80;
  }

  function jumpToBottom(): void {
    userScrolledUp = false;
    if (timelineEl) {
      timelineEl.scrollTop = timelineEl.scrollHeight;
    }
  }

  function channelTypes(): string[] {
    const types = new Set<string>();
    for (const c of conversations) {
      types.add(c.context?.type?.toLowerCase() ?? 'unknown');
    }
    return [...types].sort();
  }

  function persistSelectedChannel(): void {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem('cognis-chat-selected-channel', selectedChannel);
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

  async function loadSessionInfo(): Promise<void> {
    const sid = currentConversation?.root_session_id;
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

  async function openConversation(conversationId: string): Promise<void> {
    if (!conversationId || conversationId === activeConversationId) return;

    loading = true;
    error = '';

    if (activeConversationId) {
      wsClient.unsubscribeConversation(activeConversationId);
    }

    try {
      const [conversation, sessionList, events] = await Promise.all([
        api.conversations.detail(conversationId),
        api.conversations.sessions(conversationId),
        loadHistory(conversationId)
      ]);

      activeConversationId = conversationId;
      currentConversation = conversation;
      if (!conversations.some((item) => item.conversation_id === conversation.conversation_id)) {
        conversations = [conversation, ...conversations];
      }
      sessions = sessionList;
      resetSessionFilter();
      timeline = normalizeHistory(events);
      syncVisibleWindow();
      queuedCount = 0;
      turnInProgress = false;
      awaitingAssistantStart = false;
      lastRecoverableMessage = '';
      editingTitle = false;
      contextUsage = null;

      wsClient.subscribeConversation(conversationId, latestSeq(events));
      await refreshEscalations();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
      // Scroll to bottom after DOM renders the timeline
      userScrolledUp = false;
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          if (timelineEl) {
            timelineEl.scrollTop = timelineEl.scrollHeight;
          }
        });
      });
    }
  }

  async function initialize(): Promise<void> {
    loading = true;
    error = '';

    try {
      await refreshSidebarData();
      await openConversation(conversationIdFromRoute());
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      loading = false;
    }
  }

  async function createNewConversation(): Promise<void> {
    if (!selectedAgentId) {
      error = 'Create or activate an agent before starting a conversation.';
      return;
    }

    persistSelectedAgent();

    try {
      const conversation = await api.conversations.create({
        agent_id: selectedAgentId,
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
    const sid = currentConversation?.root_session_id;
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
  const SYSTEM_SLASH_COMMANDS = ['/approve', '/deny', '/compact', '/summarize', '/new', '/reset', '/clear', '/context', '/info', '/lsp', '/model', '/thinking', '/help'];

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
    { command: '/approve', description: 'Approve tool escalation' },
    { command: '/deny', description: 'Deny tool escalation' },
  ];

  let slashSuggestionsVisible = false;
  let slashFilteredSuggestions: typeof SLASH_SUGGESTIONS = [];
  let slashSelectedIndex = 0;

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
    composerElement?.focus();
  }

  async function handleSend(): Promise<void> {
    const content = composer.trim();
    if (!content || !currentConversation || isReadOnly(currentConversation)) return;

    const isSlashCommand = SYSTEM_SLASH_COMMANDS.some((cmd) => content.startsWith(cmd));

    if (!isSlashCommand) {
      timeline = appendOptimisticUserMessage(timeline, content);
      lastSubmittedMessage = content;
      lastRecoverableMessage = '';
      turnInProgress = true;
      awaitingAssistantStart = true;
    }
    error = '';
    composer = '';
    syncVisibleWindow();
    userScrolledUp = false;
    scrollToBottom();
    wsClient.sendMessage(currentConversation.conversation_id, content);
  }

  async function retryLastTurn(): Promise<void> {
    if (!currentConversation || !lastSubmittedMessage) return;
    composer = lastSubmittedMessage;
    await handleSend();
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

  async function handleEscalationDecision(callId: string, decision: 'approve' | 'deny'): Promise<void> {
    escalationBusyCallId = callId;
    wsClient.resolveEscalation(callId, decision);
    // Optimistically remove after a short delay (server push will confirm)
    setTimeout(() => {
      if (escalationBusyCallId === callId) {
        escalations = escalations.filter((e) => e.call_id !== callId);
        escalationBusyCallId = null;
        if (escalations.length === 0) stopEscalationCountdown();
      }
    }, 3000);
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
      return;
    }

    // Filter sub-session tool/chunk events from the main timeline (defense-in-depth)
    const rootSid = currentConversation?.root_session_id;
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
      timeline = finalizeReasoningItems(timeline);
      // Update context usage from message_complete
      if (event.type === 'message_complete' && event.context_usage) {
        contextUsage = event.context_usage;
      }
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

    timeline = applyWebSocketEvent(timeline, event);
    if (event.type !== 'tool_call' && event.type !== 'tool_result' && event.type !== 'reasoning') {
      syncVisibleWindow();
    }

    // Auto-scroll on new content
    if (event.type === 'chunk' || event.type === 'message_complete' || event.type === 'delegation_started' || event.type === 'delegation_completed' || event.type === 'system_message') {
      scrollToBottom();
    }

    // No longer polling for escalations — they arrive via push events
  }

  function handleAgentFilterChange(): void {
    persistSelectedAgent();
  }

  async function handleViewSession(sessionId: string): Promise<void> {
    if (!currentConversation) return;
    subSessionId = sessionId;
    subSessionPanelOpen = true;
    subSessionLoading = true;
    subSessionError = '';
    subSessionTimeline = [];
    subSessionInfo = null;
    subSessionInfoOpen = false;
    try {
      const result = await api.conversations.sessionEvents(currentConversation.conversation_id, sessionId, 0, 200);
      subSessionTimeline = normalizeHistory(result.items ?? []);
    } catch (err) {
      subSessionError = asApiError(err)?.message ?? 'Failed to load session events';
    } finally {
      subSessionLoading = false;
    }
  }

  function closeSubSessionPanel(): void {
    subSessionClosing = true;
    setTimeout(() => {
      subSessionPanelOpen = false;
      subSessionClosing = false;
      subSessionId = '';
      subSessionTimeline = [];
      subSessionInfo = null;
      subSessionInfoOpen = false;
    }, 250);
  }

  $: if ($page.params.conversationId && $page.params.conversationId !== activeConversationId) {
    void openConversation($page.params.conversationId);
  }

  $: displayedTimeline = timeline.slice(visibleStartIndex);

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
        void refreshEscalations();
      }
    };
    document.addEventListener('visibilitychange', visibilityHandler);

    void initialize();

    return () => {
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
    };
  });
</script>

<svelte:head>
  <title>{currentConversation ? `${conversationTitle(currentConversation)} · Chat · Cognis` : 'Chat · Cognis'}</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading conversation" description="Fetching history, restoring workflow prompts, and preparing the live stream." />
{:else}
  <div class={`grid h-full gap-4 overflow-hidden ${chatSidebarCollapsed ? '' : 'xl:grid-cols-[320px_minmax(0,1fr)]'}`}>
    <!-- Sidebar -->
    <aside class={`${chatSidebarCollapsed ? 'hidden' : `${mobileListOpen || !currentConversation ? 'flex' : 'hidden'} xl:flex`} min-h-0 flex-col rounded-3xl border border-slate-800/80 bg-slate-900/70 shadow-card backdrop-blur`}>
      <!-- Static top: filters -->
      <div class="shrink-0 space-y-3 p-4 pb-2">
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
          <label class="block space-y-1">
            <span class="text-xs font-medium uppercase tracking-widest text-slate-500">Agent</span>
            <select
              bind:value={selectedAgentId}
              onchange={handleAgentFilterChange}
              class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
            >
              {#each agents.filter((a) => a.status === 'active') as agent}
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
          {#if filteredConversations().length === 0}
            <p class="rounded-2xl border border-dashed border-slate-700 px-4 py-6 text-center text-sm text-slate-400">
              No conversations found.
            </p>
          {:else}
            {#each filteredConversations() as conversation}
              {@const agent = conversationAgent(conversation)}
              <a
                class={`flex items-start gap-3 rounded-2xl border px-3 py-2.5 transition ${conversation.conversation_id === currentConversation?.conversation_id ? 'border-sky-400/40 bg-sky-500/10' : 'border-transparent bg-slate-950/60 hover:border-slate-700 hover:bg-slate-900'}`}
                href={`/chat/${conversation.conversation_id}`}
                onclick={() => { mobileListOpen = false; }}
              >
                <AgentAvatar name={agent?.display_name ?? agent?.name ?? conversation.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-8 w-8" />
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm font-medium text-white">{conversationTitle(conversation)}</p>
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
    <section class={`${mobileListOpen && currentConversation ? 'hidden' : 'flex'} relative min-h-0 flex-col rounded-3xl border border-slate-800/80 bg-slate-900/70 shadow-card backdrop-blur xl:flex`}>
      <!-- Header -->
      <div class="border-b border-slate-800/80 px-5 py-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <div class="mb-2 flex items-center gap-2">
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
                <Button size="sm" variant="secondary" onclick={() => (mobileListOpen = true)}>
                  <ArrowLeft class="mr-2 h-4 w-4" />
                  Conversations
                </Button>
              </div>
            </div>

            <!-- Editable title -->
            {#if editingTitle}
              <!-- svelte-ignore a11y_autofocus -->
              <input
                class="w-full rounded-lg border border-sky-500/50 bg-slate-950/80 px-2 py-1 text-xl font-semibold text-white focus:outline-none focus:ring-1 focus:ring-sky-400"
                bind:value={editTitleValue}
                onblur={saveTitle}
                onkeydown={handleTitleKeydown}
                autofocus
              />
            {:else}
              <button
                class="text-left text-xl font-semibold text-white transition hover:text-sky-300"
                onclick={startEditTitle}
                type="button"
                title="Click to edit title"
              >
                {currentConversation ? conversationTitle(currentConversation) : 'Conversation'}
              </button>
            {/if}

            <!-- Sub-header info row -->
            <div class="mt-1.5 flex flex-wrap items-center gap-3 text-sm text-slate-400">
              {#if currentConversation}
                {@const agent = conversationAgent(currentConversation)}
                {#if agent}
                  <div class="flex items-center gap-1.5">
                    <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url ?? null} class="h-5 w-5" />
                    <span>{agent.display_name ?? agent.name}</span>
                  </div>
                {/if}

                {#if currentConversation.root_session_id}
                  <button
                    class="flex items-center gap-1 font-mono text-xs text-slate-500 transition hover:text-slate-300"
                    onclick={copySessionId}
                    type="button"
                    title="Copy full session ID"
                  >
                    {currentConversation.root_session_id.slice(0, 12)}
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

                <!-- Session info button -->
                <button
                  class="flex items-center gap-1 text-xs text-slate-500 transition hover:text-sky-300"
                  onclick={() => { sessionInfoOpen = !sessionInfoOpen; if (sessionInfoOpen && !sessionInfo) void loadSessionInfo(); }}
                  type="button"
                  title="Session details"
                >
                  <Info class="h-3.5 w-3.5" />
                </button>

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
              <div class="mt-2 rounded-xl border border-slate-700 bg-slate-900/95 px-4 py-3 text-sm">
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

          <div class="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" disabled={!currentConversation || archivingConversation} onclick={archiveConversation}>
              {archivingConversation ? 'Archiving...' : 'Archive'}
            </Button>
            <Button size="sm" variant="danger" disabled={!currentConversation || deletingConversation} onclick={deleteConversation}>
              {deletingConversation ? 'Deleting...' : 'Delete'}
            </Button>
          </div>
        </div>

        {#if queuedCount > 0}
          <p class="mt-3 rounded-2xl border border-sky-400/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-100">
            {queuedCount} additional message{queuedCount === 1 ? '' : 's'} queued for this conversation.
          </p>
        {/if}

        {#if error}
          <div class="mt-3 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-3 py-3 text-sm text-rose-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <p>{error}</p>
              {#if lastRecoverableMessage}
                <Button size="sm" variant="secondary" onclick={retryLastTurn}>Retry</Button>
              {/if}
            </div>
          </div>
        {/if}
      </div>

      <!-- Message area + composer -->
      <div class="flex min-h-0 flex-1 flex-col gap-4 p-4">
        {#if isMemoryDegraded()}
          <div class="rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
            Memory is currently unavailable — this conversation won't have access to past context.
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
          class="relative min-h-0 flex-1 space-y-3 overflow-y-auto p-4"
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
                <div class={`flex ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
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
            {#if rootSessionStatus() === 'suspended'}
              This session is suspended.
            {:else if rootSessionStatus() === 'terminated'}
              This session has been terminated.
            {:else}
              This session has ended ({rootSessionStatus()}).
            {/if}
          </div>
        {:else}
          <form class="shrink-0 space-y-3 rounded-3xl border border-slate-800/80 bg-slate-900/80 p-4" onsubmit={(event) => { event.preventDefault(); void handleSend(); }}>
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
            <textarea
              bind:this={composerElement}
              bind:value={composer}
              class="min-h-[80px] w-full resize-none rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
              disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()}
              onkeydown={handleComposerKeydown}
              oninput={updateSlashSuggestions}
              placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : 'Send a message to Cognis...'}
            ></textarea>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <label class="flex items-center gap-2 text-xs text-slate-400">
                <input bind:checked={enterToSend} class="h-4 w-4 rounded border-slate-700 bg-slate-950" onchange={persistEnterToSendPreference} type="checkbox" />
                <span>Press Enter to send</span>
              </label>
              <div class="flex gap-2">
                <Button size="sm" variant="secondary" type="button" onclick={() => currentConversation && wsClient.cancelTurn(currentConversation.conversation_id)}>
                  Cancel turn
                </Button>
                <Button size="sm" type="submit" disabled={!composer.trim() || !currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()}>
                  Send
                </Button>
              </div>
            </div>
          </form>
        {/if}
      </div>

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
                  <div class={`flex ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
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
