<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { ArrowLeft, Search, Copy, Check } from 'lucide-svelte';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
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
  import type { Agent, Conversation, Escalation, MessageEvent, Session } from '$lib/types/api';
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
  let awaitingAssistantStart = false;
  let turnInProgress = false;
  let lastSubmittedMessage = '';
  let lastRecoverableMessage = '';
  let editingTitle = false;
  let editTitleValue = '';
  let sessionIdCopied = false;
  let subSessionPanelOpen = false;
  let subSessionId = '';
  let subSessionTimeline: TimelineItem[] = [];
  let subSessionLoading = false;
  let subSessionError = '';

  const escalationFirstSeen = new Map<string, number>();
  const sessionIds = new Set<string>();

  let unsubscribeWs: (() => void) | null = null;
  let unsubscribeComposerFocus: (() => void) | null = null;
  let unsubscribeCancelTurn: (() => void) | null = null;
  let visibilityHandler: (() => void) | null = null;
  let escalationPollTimer: number | null = null;

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

  function contextTypeBadge(conversation: Conversation): string {
    const t = conversation.context?.type ?? 'unknown';
    return t.charAt(0).toUpperCase() + t.slice(1).toLowerCase();
  }

  function filteredConversations(): Conversation[] {
    const query = conversationSearch.trim().toLowerCase();
    const list = query
      ? conversations.filter((c) => conversationTitle(c).toLowerCase().includes(query))
      : conversations;
    return list;
  }

  function groupedConversations(): { web: Conversation[]; other: Conversation[] } {
    const filtered = filteredConversations();
    const web: Conversation[] = [];
    const other: Conversation[] = [];
    for (const c of filtered) {
      if (c.context?.type?.toLowerCase() === 'web') {
        web.push(c);
      } else {
        other.push(c);
      }
    }
    return { web, other };
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

  async function loadEscalationTimeout(): Promise<void> {
    try {
      const groups = await api.settings.list();
      const setting = groups.flatMap((group) => group.items).find((item) => item.key === 'session.escalation_timeout_seconds');
      if (typeof setting?.value === 'number') {
        escalationTimeoutSeconds = setting.value;
      }
    } catch {
      escalationTimeoutSeconds = 300;
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

  async function refreshEscalations(): Promise<void> {
    if (document.hidden) return;

    try {
      const allEscalations = await api.escalations.list();
      const filtered = allEscalations.filter((item) => sessionIds.size === 0 || item.session_id === null || sessionIds.has(item.session_id));
      const now = Date.now();
      const present = new Set(filtered.map((item) => item.call_id));
      for (const item of filtered) {
        if (!escalationFirstSeen.has(item.call_id)) {
          escalationFirstSeen.set(item.call_id, now);
        }
      }
      for (const key of [...escalationFirstSeen.keys()]) {
        if (!present.has(key)) {
          escalationFirstSeen.delete(key);
        }
      }
      escalations = filtered;
      escalationError = '';
    } catch (caughtError) {
      escalationError = asApiError(caughtError).message;
    }
  }

  function stopEscalationPolling(): void {
    if (escalationPollTimer !== null) {
      window.clearInterval(escalationPollTimer);
      escalationPollTimer = null;
    }
  }

  const ESCALATION_POLL_ACTIVE_MS = 5_000;
  const ESCALATION_POLL_IDLE_MS = 15_000;

  function startEscalationPolling(): void {
    stopEscalationPolling();
    if (typeof document === 'undefined' || document.hidden) return;
    const interval = escalations.length > 0 ? ESCALATION_POLL_ACTIVE_MS : ESCALATION_POLL_IDLE_MS;
    escalationPollTimer = window.setInterval(() => {
      void refreshEscalations().then(() => {
        const nextInterval = escalations.length > 0 ? ESCALATION_POLL_ACTIVE_MS : ESCALATION_POLL_IDLE_MS;
        if (nextInterval !== interval) {
          startEscalationPolling();
        }
      });
    }, interval);
  }

  function syncVisibleWindow(): void {
    visibleStartIndex = Math.max(0, timeline.length - 100);
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

      wsClient.subscribeConversation(conversationId, latestSeq(events));
      await refreshEscalations();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function initialize(): Promise<void> {
    loading = true;
    error = '';

    try {
      await Promise.all([refreshSidebarData(), loadEscalationTimeout()]);
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

  async function handleSend(): Promise<void> {
    const content = composer.trim();
    if (!content || !currentConversation || isReadOnly(currentConversation)) return;

    timeline = appendOptimisticUserMessage(timeline, content);
    lastSubmittedMessage = content;
    lastRecoverableMessage = '';
    turnInProgress = true;
    awaitingAssistantStart = true;
    error = '';
    composer = '';
    syncVisibleWindow();
    wsClient.sendMessage(currentConversation.conversation_id, content);
  }

  async function retryLastTurn(): Promise<void> {
    if (!currentConversation || !lastSubmittedMessage) return;
    composer = lastSubmittedMessage;
    await handleSend();
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    if (!enterToSend || event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    void handleSend();
  }

  async function handleEscalationDecision(callId: string, decision: 'approve' | 'deny'): Promise<void> {
    escalationBusyCallId = callId;
    try {
      await api.escalations.resolve(callId, { decision });
      await refreshEscalations();
      addToast(`Escalation ${decision}d.`, 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to resolve escalation');
    } finally {
      escalationBusyCallId = null;
    }
  }

  function escalationSecondsRemaining(callId: string): number {
    const firstSeen = escalationFirstSeen.get(callId) ?? Date.now();
    return escalationTimeoutSeconds - Math.floor((Date.now() - firstSeen) / 1000);
  }

  function loadOlder(): void {
    visibleStartIndex = Math.max(0, visibleStartIndex - 50);
  }

  function handleSocketEvent(event: import('$lib/types/api').CognisWebSocketEvent): void {
    const currentId = conversationIdFromRoute();
    if ('conversation_id' in event && event.conversation_id && event.conversation_id !== currentId) {
      return;
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

    timeline = applyWebSocketEvent(timeline, event);
    if (event.type !== 'tool_call' && event.type !== 'tool_result' && event.type !== 'reasoning') {
      syncVisibleWindow();
    }

    if (
      event.type === 'workflow_completed' ||
      event.type === 'workflow_failed' ||
      event.type === 'workflow_cancelled' ||
      event.type === 'workflow_gate' ||
      event.type === 'workflow_step_question'
    ) {
      void refreshEscalations();
    }
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
    subSessionPanelOpen = false;
    subSessionId = '';
    subSessionTimeline = [];
  }

  $: if ($page.params.conversationId && $page.params.conversationId !== activeConversationId) {
    void openConversation($page.params.conversationId);
  }

  $: displayedTimeline = timeline.slice(visibleStartIndex);

  onMount(() => {
    restoreEnterToSendPreference();
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
      if (document.hidden) {
        stopEscalationPolling();
      } else {
        void refreshEscalations();
        startEscalationPolling();
      }
    };
    document.addEventListener('visibilitychange', visibilityHandler);

    void initialize().then(() => {
      startEscalationPolling();
    });

    return () => {
      unsubscribeWs?.();
      unsubscribeComposerFocus?.();
      unsubscribeCancelTurn?.();
      stopEscalationPolling();
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
  <div class={`grid min-h-[calc(100vh-12rem)] gap-4 ${subSessionPanelOpen ? 'xl:grid-cols-[320px_minmax(0,1fr)_420px]' : 'xl:grid-cols-[320px_minmax(0,1fr)]'}`}>
    <!-- Sidebar -->
    <aside class={`${mobileListOpen || !currentConversation ? 'block' : 'hidden'} space-y-4 rounded-3xl border border-slate-800/80 bg-slate-900/70 p-4 shadow-card backdrop-blur xl:block`}>
      {#if agents.length === 0}
        <Card class="p-4">
          <div class="space-y-4">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Setup incomplete</p>
            <h2 class="text-lg font-semibold text-white">Create an agent to start chatting</h2>
            <p class="text-sm leading-6 text-slate-400">You need at least one active agent before conversations can start.</p>
            <Button class="w-full justify-center" onclick={() => goto('/agents/new')}>Create agent</Button>
          </div>
        </Card>
      {:else if isLlmUnavailableForSetup()}
        <Card class="p-4">
          <div class="space-y-4">
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Setup incomplete</p>
            <h2 class="text-lg font-semibold text-white">Configure an LLM provider to start chatting</h2>
            <p class="text-sm leading-6 text-slate-400">Chat and task execution need a configured provider before they can run.</p>
            <Button class="w-full justify-center" onclick={() => goto('/settings?tab=providers')}>Open provider settings</Button>
          </div>
        </Card>
      {:else}
        <!-- Agent filter -->
        <label class="block space-y-2">
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
      {/if}

      <!-- Conversation list -->
      <Card class="p-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Conversations</p>
            <h2 class="mt-1 text-lg font-semibold text-white">History</h2>
          </div>
          <button
            class="text-xs font-medium text-sky-400 transition hover:text-sky-300"
            onclick={createNewConversation}
            type="button"
          >+ New</button>
        </div>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Search</span>
          <div class="relative">
            <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
            <Input bind:value={conversationSearch} class="pl-9" placeholder="Filter by title" />
          </div>
        </label>

        <div class="mt-4 space-y-1">
          {#if filteredConversations().length === 0}
            <p class="rounded-2xl border border-dashed border-slate-700 px-4 py-6 text-sm text-slate-400">
              No conversations loaded yet.
            </p>
          {:else}
            {@const grouped = groupedConversations()}

            <!-- Web conversations -->
            {#if grouped.web.length > 0}
              <p class="px-1 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-widest text-slate-500">Web</p>
              {#each grouped.web as conversation}
                {@const agent = conversationAgent(conversation)}
                <a
                  class={`flex items-start gap-3 rounded-2xl border px-3 py-2.5 transition ${conversation.conversation_id === currentConversation?.conversation_id ? 'border-sky-400/40 bg-sky-500/10' : 'border-transparent bg-slate-950/60 hover:border-slate-700 hover:bg-slate-900'}`}
                  href={`/chat/${conversation.conversation_id}`}
                  onclick={() => { mobileListOpen = false; }}
                >
                  <AgentAvatar name={agent?.display_name ?? agent?.name ?? conversation.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-8 w-8" />
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-sm font-medium text-white">{conversationTitle(conversation)}</p>
                    <p class="mt-0.5 truncate text-xs text-slate-400">{agent?.display_name ?? agent?.name ?? conversation.agent_id}</p>
                  </div>
                </a>
              {/each}
            {/if}

            <!-- Non-web conversations -->
            {#if grouped.other.length > 0}
              <p class="px-1 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-widest text-slate-500">Other channels</p>
              {#each grouped.other as conversation}
                {@const agent = conversationAgent(conversation)}
                <a
                  class={`flex items-start gap-3 rounded-2xl border px-3 py-2.5 opacity-70 transition ${conversation.conversation_id === currentConversation?.conversation_id ? 'border-sky-400/40 bg-sky-500/10 opacity-100' : 'border-transparent bg-slate-950/60 hover:border-slate-700 hover:bg-slate-900'}`}
                  href={`/chat/${conversation.conversation_id}`}
                  onclick={() => { mobileListOpen = false; }}
                >
                  <AgentAvatar name={agent?.display_name ?? agent?.name ?? conversation.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-8 w-8" />
                  <div class="min-w-0 flex-1">
                    <div class="flex items-center gap-1.5">
                      <span class="text-xs text-slate-500" title="Non-web channel (read-only)">&#128274;</span>
                      <p class="truncate text-sm font-medium text-slate-300">{conversationTitle(conversation)}</p>
                    </div>
                    <p class="mt-0.5 truncate text-xs text-slate-500">{agent?.display_name ?? agent?.name ?? conversation.agent_id}</p>
                  </div>
                </a>
              {/each}
            {/if}
          {/if}

          {#if conversationsHasMore}
            <div class="pt-2">
              <Button class="w-full justify-center" size="sm" variant="secondary" onclick={() => loadConversationPage(false)}>Load more conversations</Button>
            </div>
          {/if}
        </div>
      </Card>
    </aside>

    <!-- Main chat area -->
    <section class={`${mobileListOpen && currentConversation ? 'hidden' : 'flex'} min-h-0 flex-col rounded-3xl border border-slate-800/80 bg-slate-900/70 shadow-card backdrop-blur xl:flex`}>
      <!-- Header -->
      <div class="border-b border-slate-800/80 px-5 py-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <div class="mb-2 xl:hidden">
              <Button size="sm" variant="secondary" onclick={() => (mobileListOpen = true)}>
                <ArrowLeft class="mr-2 h-4 w-4" />
                Conversations
              </Button>
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
              {:else}
                <span>No active conversation selected</span>
              {/if}
            </div>
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

        {#if turnInProgress}
          <div class="rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="flex items-center gap-3">
                <span class="font-medium">Agent is working...</span>
                {#if awaitingAssistantStart}
                  <span class="inline-flex items-center gap-1 text-sky-100/80">
                    <span class="h-2 w-2 animate-pulse rounded-full bg-sky-200"></span>
                    <span class="h-2 w-2 animate-pulse rounded-full bg-sky-200 [animation-delay:120ms]"></span>
                    <span class="h-2 w-2 animate-pulse rounded-full bg-sky-200 [animation-delay:240ms]"></span>
                    <span>Thinking</span>
                  </span>
                {/if}
              </div>
              {#if currentConversation}
                <Button size="sm" variant="secondary" onclick={() => currentConversation && wsClient.cancelTurn(currentConversation.conversation_id)}>Cancel turn</Button>
              {/if}
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

        {#if escalations.length > 0}
          <div class="space-y-3">
            {#each escalations as escalation (escalation.call_id)}
              <EscalationPrompt
                item={escalation}
                secondsRemaining={escalationSecondsRemaining(escalation.call_id)}
                pending={escalationBusyCallId === escalation.call_id}
                onApprove={() => handleEscalationDecision(escalation.call_id, 'approve')}
                onDeny={() => handleEscalationDecision(escalation.call_id, 'deny')}
              />
            {/each}
          </div>
        {/if}

        <!-- Timeline -->
        <div class="min-h-0 flex-1 space-y-3 overflow-y-auto rounded-3xl border border-slate-800/80 bg-slate-950/60 p-4">
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
              {:else}
                <article class={`rounded-3xl border px-4 py-4 text-sm shadow-card ${item.tone === 'warning' ? 'border-amber-500/30 bg-amber-500/10 text-amber-100' : item.tone === 'error' ? 'border-rose-500/30 bg-rose-500/10 text-rose-100' : 'border-slate-700 bg-slate-900 text-slate-200'}`}>
                  <h3 class="font-semibold">{item.title}</h3>
                  <p class="mt-2 leading-6">{item.description}</p>
                </article>
              {/if}
            {/each}
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
        {:else}
          <form class="space-y-3 rounded-3xl border border-slate-800/80 bg-slate-900/80 p-4" onsubmit={(event) => { event.preventDefault(); void handleSend(); }}>
            <textarea
              bind:this={composerElement}
              bind:value={composer}
              class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
              disabled={!currentConversation || isReadOnly(currentConversation) || isLlmUnavailableForSetup()}
              onkeydown={handleComposerKeydown}
              placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : 'Send a message to Cognis...'}
            ></textarea>
            <div class="flex flex-wrap items-center justify-between gap-3">
              <div class="space-y-1">
                <p class="text-xs uppercase tracking-[0.2em] text-slate-500">
                  Reconnect-safe streaming via first-message-auth WebSocket
                </p>
                <label class="flex items-center gap-2 text-xs text-slate-400">
                  <input bind:checked={enterToSend} class="h-4 w-4 rounded border-slate-700 bg-slate-950" onchange={persistEnterToSendPreference} type="checkbox" />
                  <span>Press Enter to send</span>
                </label>
              </div>
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
    </section>

    <!-- Sub-session panel (slide-out) -->
    {#if subSessionPanelOpen}
      <aside class="flex min-h-0 w-full flex-col rounded-3xl border border-slate-800/80 bg-slate-900/70 shadow-card backdrop-blur xl:w-[420px]">
        <div class="flex items-center justify-between border-b border-slate-800/80 px-4 py-3">
          <div class="min-w-0">
            <p class="text-xs font-medium uppercase tracking-[0.2em] text-slate-400">Sub-session</p>
            <p class="mt-0.5 truncate font-mono text-xs text-slate-500">{subSessionId.slice(0, 16)}</p>
          </div>
          <Button size="sm" variant="ghost" onclick={closeSubSessionPanel}>Close</Button>
        </div>
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
                <ChatMessage {item} />
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
  </div>
{/if}
