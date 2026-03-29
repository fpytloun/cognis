<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { ArrowLeft, Search } from 'lucide-svelte';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ToolCallCard from '$lib/components/ToolCallCard.svelte';
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
  let createTitle = '';
  let selectedAgentId = '';
  let creatingConversation = false;
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

  function filteredConversations(): Conversation[] {
    const query = conversationSearch.trim().toLowerCase();
    if (!query) {
      return conversations;
    }
    return conversations.filter((conversation) => conversationTitle(conversation).toLowerCase().includes(query));
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
    if (typeof window === 'undefined') {
      return;
    }
    window.localStorage.setItem('cognis-chat-enter-to-send', enterToSend ? '1' : '0');
  }

  function restoreEnterToSendPreference(): void {
    if (typeof window === 'undefined') {
      return;
    }
    enterToSend = window.localStorage.getItem('cognis-chat-enter-to-send') !== '0';
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
    selectedAgentId = selectedAgentId || agents.find((agent) => agent.status === 'active')?.agent_id || agents[0]?.agent_id || '';
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
    if (document.hidden) {
      return;
    }

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

  function startEscalationPolling(): void {
    stopEscalationPolling();
    if (typeof document === 'undefined' || document.hidden) {
      return;
    }
    escalationPollTimer = window.setInterval(() => {
      void refreshEscalations();
    }, 5000);
  }

  function syncVisibleWindow(): void {
    visibleStartIndex = Math.max(0, timeline.length - 100);
  }

  async function openConversation(conversationId: string): Promise<void> {
    if (!conversationId || conversationId === activeConversationId) {
      return;
    }

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

  async function createConversation(): Promise<void> {
    if (!selectedAgentId) {
      error = 'Create or activate an agent before starting a conversation.';
      return;
    }

    creatingConversation = true;
    try {
      const conversation = await api.conversations.create({
        agent_id: selectedAgentId,
        title: createTitle || null,
        context: {
          type: 'web',
          ref: null,
          platform_data: {},
          memory_labels: {}
        }
      });
      createTitle = '';
      await refreshSidebarData();
      addToast('Conversation created.', 'success');
      await goto(`/chat/${conversation.conversation_id}`);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to create conversation');
    } finally {
      creatingConversation = false;
    }
  }

  async function archiveConversation(): Promise<void> {
    if (!currentConversation) {
      return;
    }

    const confirmed = await confirmAction({
      title: 'Archive conversation?',
      message: 'The conversation will become read-only until you manually reactivate it.',
      confirmLabel: 'Archive conversation'
    });
    if (!confirmed) {
      return;
    }

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
    if (!currentConversation) {
      return;
    }

    const confirmed = await confirmAction({
      title: 'Delete conversation?',
      message: 'This removes the conversation from the workspace. Use purge from the API only when you need permanent deletion.',
      confirmLabel: 'Delete conversation'
    });
    if (!confirmed) {
      return;
    }

    deletingConversation = true;
    try {
      await api.conversations.remove(currentConversation.conversation_id);
      await refreshSidebarData();
      const nextConversation = conversations.find((conversation) => conversation.conversation_id !== currentConversation?.conversation_id);
      addToast('Conversation deleted.', 'success');
      await goto(nextConversation ? `/chat/${nextConversation.conversation_id}` : '/chat/new');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to delete conversation');
    } finally {
      deletingConversation = false;
    }
  }

  async function handleSend(): Promise<void> {
    const content = composer.trim();
    if (!content || !currentConversation || currentConversation.status !== 'active') {
      return;
    }

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
    if (!currentConversation || !lastSubmittedMessage) {
      return;
    }
    composer = lastSubmittedMessage;
    await handleSend();
  }

  function handleComposerKeydown(event: KeyboardEvent): void {
    if (!enterToSend || event.key !== 'Enter' || event.shiftKey) {
      return;
    }
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

    if (event.type === 'chunk' || event.type === 'tool_call' || event.type === 'delegation_started') {
      awaitingAssistantStart = false;
    }

    if (event.type === 'message_complete' || event.type === 'workflow_failed' || event.type === 'workflow_cancelled') {
      awaitingAssistantStart = false;
      turnInProgress = false;
    }

    timeline = applyWebSocketEvent(timeline, event);
    if (event.type !== 'tool_call') {
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
  <div class="grid min-h-[calc(100vh-12rem)] gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
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
        <Card class="p-4">
          <div class="space-y-4">
            <div>
              <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">New conversation</p>
              <h2 class="mt-1 text-lg font-semibold text-white">Create chat</h2>
            </div>

            <label class="block space-y-2 text-sm font-medium text-slate-200">
              <span>Agent <span class="text-rose-300">*</span></span>
              <select bind:value={selectedAgentId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                {#each agents as agent}
                  <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
                {/each}
              </select>
            </label>

            <label class="block space-y-2 text-sm font-medium text-slate-200">
              <span>Title</span>
              <Input bind:value={createTitle} placeholder="Optional conversation title" />
            </label>

            <Button class="w-full justify-center" disabled={creatingConversation || !selectedAgentId} onclick={createConversation}>
              {creatingConversation ? 'Creating…' : 'Start conversation'}
            </Button>
          </div>
        </Card>
      {/if}

      <Card class="p-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Conversations</p>
            <h2 class="mt-1 text-lg font-semibold text-white">History</h2>
          </div>
          <Button size="sm" variant="secondary" onclick={() => goto('/chat/new')}>New</Button>
        </div>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Search</span>
          <div class="relative">
            <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
            <Input bind:value={conversationSearch} class="pl-9" placeholder="Filter by title" />
          </div>
        </label>

        <div class="mt-4 space-y-2">
          {#if filteredConversations().length === 0}
            <p class="rounded-2xl border border-dashed border-slate-700 px-4 py-6 text-sm text-slate-400">
              No conversations loaded yet.
            </p>
          {:else}
            {#each filteredConversations() as conversation}
              {@const agent = conversationAgent(conversation)}
              <a
                class={`flex items-start gap-3 rounded-2xl border px-3 py-3 transition ${conversation.conversation_id === currentConversation?.conversation_id ? 'border-sky-400/40 bg-sky-500/10' : 'border-transparent bg-slate-950/60 hover:border-slate-700 hover:bg-slate-900'}`}
                href={`/chat/${conversation.conversation_id}`}
                onclick={() => {
                  mobileListOpen = false;
                }}
              >
                <AgentAvatar name={agent?.display_name ?? agent?.name ?? conversation.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-9 w-9" />
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm font-medium text-white">{conversationTitle(conversation)}</p>
                  <p class="mt-1 truncate text-xs text-slate-400">{agent?.display_name ?? agent?.name ?? conversation.agent_id}</p>
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
      </Card>
    </aside>

    <section class={`${mobileListOpen && currentConversation ? 'hidden' : 'flex'} min-h-0 flex-col rounded-3xl border border-slate-800/80 bg-slate-900/70 shadow-card backdrop-blur xl:flex`}>
      <div class="border-b border-slate-800/80 px-5 py-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div class="mb-2 xl:hidden">
              <Button size="sm" variant="secondary" onclick={() => (mobileListOpen = true)}>
                <ArrowLeft class="mr-2 h-4 w-4" />
                Conversations
              </Button>
            </div>
            <h1 class="text-xl font-semibold text-white">{currentConversation ? conversationTitle(currentConversation) : 'Conversation'}</h1>
            <p class="mt-1 text-sm text-slate-400">
              {currentConversation ? currentConversation.agent_id : 'No active conversation selected'}
            </p>
          </div>

          <div class="flex flex-wrap gap-2">
            <Button size="sm" variant="secondary" disabled={!currentConversation || archivingConversation} onclick={archiveConversation}>
              {archivingConversation ? 'Archiving…' : 'Archive'}
            </Button>
            <Button size="sm" variant="danger" disabled={!currentConversation || deletingConversation} onclick={deleteConversation}>
              {deletingConversation ? 'Deleting…' : 'Delete'}
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
                <span class="font-medium">Agent is working…</span>
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

        <div class="min-h-0 flex-1 space-y-4 overflow-y-auto rounded-3xl border border-slate-800/80 bg-slate-950/60 p-4">
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
                  <ChatMessage item={item} />
                </div>
              {:else if item.kind === 'tool_call'}
                <ToolCallCard item={item} />
              {:else if item.kind === 'delegation'}
                <DelegationCard item={item} />
              {:else}
                <article class={`rounded-3xl border px-4 py-4 text-sm shadow-card ${item.tone === 'warning' ? 'border-amber-500/30 bg-amber-500/10 text-amber-100' : item.tone === 'error' ? 'border-rose-500/30 bg-rose-500/10 text-rose-100' : 'border-slate-700 bg-slate-900 text-slate-200'}`}>
                  <h3 class="font-semibold">{item.title}</h3>
                  <p class="mt-2 leading-6">{item.description}</p>
                </article>
              {/if}
            {/each}
          {/if}
        </div>

        <form class="space-y-3 rounded-3xl border border-slate-800/80 bg-slate-900/80 p-4" onsubmit={(event) => { event.preventDefault(); void handleSend(); }}>
          <textarea
            bind:this={composerElement}
            bind:value={composer}
            class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
            disabled={!currentConversation || currentConversation.status !== 'active' || isLlmUnavailableForSetup()}
            onkeydown={handleComposerKeydown}
            placeholder={isLlmUnavailableForSetup() ? 'Configure an LLM provider to start chatting.' : currentConversation?.status === 'active' ? 'Send a message to Cognis…' : 'Archived conversations are read-only.'}
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
              <Button size="sm" type="submit" disabled={!composer.trim() || !currentConversation || currentConversation.status !== 'active' || isLlmUnavailableForSetup()}>
                Send
              </Button>
            </div>
          </div>
        </form>
      </div>
    </section>
  </div>
{/if}
