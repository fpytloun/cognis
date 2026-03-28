<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

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
  let agents: Agent[] = [];
  let currentConversation: Conversation | null = null;
  let sessions: Session[] = [];
  let composer = '';
  let createTitle = '';
  let selectedAgentId = '';
  let creatingConversation = false;
  let archivingConversation = false;
  let deletingConversation = false;
  let queuedCount = 0;
  let timeline: TimelineItem[] = [];
  let visibleStartIndex = 0;
  let activeConversationId = '';
  let escalationTimeoutSeconds = 300;
  let escalations: Escalation[] = [];
  let escalationBusyCallId: string | null = null;

  const escalationFirstSeen = new Map<string, number>();
  const sessionIds = new Set<string>();

  let unsubscribeWs: (() => void) | null = null;
  let visibilityHandler: (() => void) | null = null;
  let escalationPollTimer: number | null = null;

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
    [agents, conversations] = await Promise.all([api.agents.listAll(), api.conversations.listAll()]);
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
      sessions = sessionList;
      resetSessionFilter();
      timeline = normalizeHistory(events);
      syncVisibleWindow();
      queuedCount = 0;

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
      await goto(`/chat/${conversation.conversation_id}`);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      creatingConversation = false;
    }
  }

  async function archiveConversation(): Promise<void> {
    if (!currentConversation) {
      return;
    }

    archivingConversation = true;
    try {
      currentConversation = await api.conversations.update(currentConversation.conversation_id, { archived: true });
      await refreshSidebarData();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      archivingConversation = false;
    }
  }

  async function deleteConversation(): Promise<void> {
    if (!currentConversation) {
      return;
    }

    deletingConversation = true;
    try {
      await api.conversations.remove(currentConversation.conversation_id);
      await refreshSidebarData();
      const nextConversation = conversations.find((conversation) => conversation.conversation_id !== currentConversation?.conversation_id);
      await goto(nextConversation ? `/chat/${nextConversation.conversation_id}` : '/chat/new');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
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
    composer = '';
    syncVisibleWindow();
    wsClient.sendMessage(currentConversation.conversation_id, content);
  }

  async function handleEscalationDecision(callId: string, decision: 'approve' | 'deny'): Promise<void> {
    escalationBusyCallId = callId;
    try {
      await api.escalations.resolve(callId, { decision });
      await refreshEscalations();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
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
      error = event.message;
      return;
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
    unsubscribeWs = wsClient.subscribe(handleSocketEvent);
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
    <aside class="space-y-4 rounded-3xl border border-slate-800/80 bg-slate-900/70 p-4 shadow-card backdrop-blur">
      <Card class="p-4">
        <div class="space-y-4">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">New conversation</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Create chat</h2>
          </div>

          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Agent</span>
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

          <Button class="w-full justify-center" disabled={creatingConversation} onclick={createConversation}>
            {creatingConversation ? 'Creating…' : 'Start conversation'}
          </Button>
        </div>
      </Card>

      <Card class="p-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Conversations</p>
            <h2 class="mt-1 text-lg font-semibold text-white">History</h2>
          </div>
          <Button size="sm" variant="secondary" onclick={() => goto('/chat/new')}>New</Button>
        </div>

        <div class="mt-4 space-y-2">
          {#if conversations.length === 0}
            <p class="rounded-2xl border border-dashed border-slate-700 px-4 py-6 text-sm text-slate-400">
              No conversations yet.
            </p>
          {:else}
            {#each conversations as conversation}
              {@const agent = conversationAgent(conversation)}
              <a
                class={`flex items-start gap-3 rounded-2xl border px-3 py-3 transition ${conversation.conversation_id === currentConversation?.conversation_id ? 'border-sky-400/40 bg-sky-500/10' : 'border-transparent bg-slate-950/60 hover:border-slate-700 hover:bg-slate-900'}`}
                href={`/chat/${conversation.conversation_id}`}
              >
                <AgentAvatar name={agent?.display_name ?? agent?.name ?? conversation.agent_id} avatarUrl={agent?.avatar_url ?? null} class="h-9 w-9" />
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm font-medium text-white">{conversationTitle(conversation)}</p>
                  <p class="mt-1 truncate text-xs text-slate-400">{agent?.display_name ?? agent?.name ?? conversation.agent_id}</p>
                </div>
              </a>
            {/each}
          {/if}
        </div>
      </Card>
    </aside>

    <section class="flex min-h-0 flex-col rounded-3xl border border-slate-800/80 bg-slate-900/70 shadow-card backdrop-blur">
      <div class="border-b border-slate-800/80 px-5 py-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
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
          <p class="mt-3 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
            {error}
          </p>
        {/if}
      </div>

      <div class="flex min-h-0 flex-1 flex-col gap-4 p-4">
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
            bind:value={composer}
            class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
            disabled={!currentConversation || currentConversation.status !== 'active'}
            placeholder={currentConversation?.status === 'active' ? 'Send a message to Cognis…' : 'Archived conversations are read-only.'}
          ></textarea>
          <div class="flex flex-wrap items-center justify-between gap-3">
            <p class="text-xs uppercase tracking-[0.2em] text-slate-500">
              Reconnect-safe streaming via first-message-auth WebSocket
            </p>
            <div class="flex gap-2">
              <Button size="sm" variant="secondary" type="button" onclick={() => currentConversation && wsClient.cancelTurn(currentConversation.conversation_id)}>
                Cancel turn
              </Button>
              <Button size="sm" type="submit" disabled={!composer.trim() || !currentConversation || currentConversation.status !== 'active'}>
                Send
              </Button>
            </div>
          </div>
        </form>
      </div>
    </section>
  </div>
{/if}
