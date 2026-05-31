<script lang="ts">
  import { onMount, tick } from 'svelte';
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';

  import { api, asApiError } from '$lib/api/client';
  import {
    getNextHistoryAfterSeq,
    nextPollDelayMs,
    SESSION_LOG_BOOTSTRAP_MAX_PAGES,
    SESSION_LOG_PAGE_SIZE,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { applyActiveThinkingSnapshots, latestTodoSnapshot, normalizeHistory, type ThinkingTimelineItem, type TimelineItem, type TodoSnapshotItem } from '$lib/chat';
  import { incompleteTodos } from '$lib/todos';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ThinkingBlock from '$lib/components/ThinkingBlock.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import type { ActiveThinkingSnapshot, Agent, Escalation, MessageEvent } from '$lib/types/api';

  let {
    conversationId,
    sessionId,
    stepName = '',
    agent = null,
    backLabel = 'Parent session',
    onBack,
    onViewSession,
    onclose
  } = $props<{
    conversationId: string;
    sessionId: string;
    stepName?: string;
    agent?: Agent | null;
    backLabel?: string;
    onBack?: (() => void) | undefined;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    onclose: () => void;
  }>();

  let loading = $state(true);
  let error = $state('');
  let events = $state<MessageEvent[]>([]);
  let timeline = $state<TimelineItem[]>([]);
  let lastSeq = $state(0);
  let pollDelayMs = $state(SESSION_LOG_POLL_INTERVAL_MS);
  let currentSessionStatus = $state<string | null>(null);
  let chatTodoDrawerOpen = $state(true);
  let escalations = $state<Escalation[]>([]);
  let escalationBusyCallId = $state<string | null>(null);

  let initialLoadDone = $state(false);
  let timelineEl = $state<HTMLDivElement | null>(null);
  let timelineContentEl = $state<HTMLDivElement | null>(null);
  let footerChromeEl = $state<HTMLDivElement | null>(null);
  let userScrolledUp = $state(false);
  let programmaticScroll = false;

  type SessionTodo = TodoSnapshotItem;

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

  let sessionTodos = $derived.by(() => latestTodoSnapshot(timeline));
  let activeSessionTodos = $derived.by(() => incompleteTodos(sessionTodos));
  let shouldShowTodoDrawer = $derived(sessionTodos.length > 0);
  let todoCounts = $derived.by(() => ({
    inProgress: activeSessionTodos.filter((todo) => todo.status === 'in_progress').length,
    pending: activeSessionTodos.filter((todo) => todo.status === 'pending').length,
  }));

  const liveSessionStates = new Set(['active', 'idle', 'running']);

  function scrollToBottom(force = false): void {
    if (!timelineEl || (!force && userScrolledUp)) return;
    programmaticScroll = true;
    requestAnimationFrame(() => {
      if (timelineEl) timelineEl.scrollTop = timelineEl.scrollHeight;
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
    scrollToBottom(true);
  }

  function isSessionLive(status: string | null): boolean {
    if (!status) return false;
    return liveSessionStates.has(status);
  }

  function sortEscalations(items: Escalation[]): Escalation[] {
    return [...items].sort((left, right) => (left.received_at ?? 0) - (right.received_at ?? 0));
  }

  function escalationSecondsRemaining(item: Escalation): number {
    const timeoutSeconds = item.timeout_seconds ?? 300;
    const receivedAt = item.received_at ?? Date.now();
    return Math.max(Math.ceil(timeoutSeconds - (Date.now() - receivedAt) / 1000), 0);
  }

  async function refreshEscalations(): Promise<void> {
    const notifications = await api.notifications.list(null, { sessionId });
    escalations = sortEscalations(
      notifications
        .filter((item) => item.notification_type === 'escalation' && item.session_id === sessionId)
        .map((item) => ({
          call_id: item.notification_id,
          session_id: item.session_id,
          tool_name: typeof item.payload.tool_name === 'string' ? item.payload.tool_name : null,
          decision: 'escalate',
          resolved: false,
          reasoning: typeof item.payload.reasoning === 'string' ? item.payload.reasoning : null,
          risk: typeof item.payload.risk === 'string' ? item.payload.risk : null,
          timeout_seconds: typeof item.payload.timeout_seconds === 'number' ? item.payload.timeout_seconds : 300,
          received_at: item.created_at ? Date.parse(item.created_at) : Date.now(),
        }) satisfies Escalation)
    );
  }

  async function resolveEscalation(notificationId: string, decision: 'approve' | 'deny'): Promise<void> {
    escalationBusyCallId = notificationId;
    try {
      await api.notifications.resolve(notificationId, { decision });
      await refreshEscalations();
    } finally {
      if (escalationBusyCallId === notificationId) {
        escalationBusyCallId = null;
      }
    }
  }

  async function refreshSessionStatus(): Promise<void> {
    const sessions = await api.conversations.sessions(conversationId);
    currentSessionStatus = sessions.find((session) => session.session_id === sessionId)?.status ?? null;
  }

  async function loadEvents(refresh = false): Promise<void> {
    // Only show loading spinner on the first load — background refreshes
    // update the timeline in-place without blanking the UI.
    if (!initialLoadDone) loading = true;
    error = '';
    try {
      await refreshSessionStatus();
      try {
        await refreshEscalations();
      } catch {
        // Session logs remain usable even if escalation polling fails.
      }

      if (refresh || !initialLoadDone) {
        const history: MessageEvent[] = [];
        let afterSeq = 0;
        let pageCount = 0;
        let finalLastSeq = 0;
        let activeThinking: ActiveThinkingSnapshot[] = [];
        while (pageCount < SESSION_LOG_BOOTSTRAP_MAX_PAGES) {
          const result = await api.conversations.sessionEvents(conversationId, sessionId, afterSeq, SESSION_LOG_PAGE_SIZE);
          history.push(...(result.items ?? []));
          finalLastSeq = getNextHistoryAfterSeq(result);
          activeThinking = result.active_thinking ?? [];
          pageCount += 1;
          if (!result.has_more || result.items.length === 0) break;
          afterSeq = finalLastSeq;
          if (afterSeq === 0) break;
        }
        if (pageCount >= SESSION_LOG_BOOTSTRAP_MAX_PAGES) {
          history.push({
            seq: null,
            type: 'history_gap',
            data: { reason: 'bootstrap_cap_reached', session_id: sessionId },
            timestamp: new Date().toISOString()
          });
        }
        events = history;
        lastSeq = finalLastSeq;
        timeline = applyActiveThinkingSnapshots(normalizeHistory(history), activeThinking);
        await tick();
        userScrolledUp = false;
        scrollToBottom(true);
      } else {
        const shouldFollow = !userScrolledUp;
        const nextEvents: MessageEvent[] = [];
        let afterSeq = lastSeq;
        let finalLastSeq = lastSeq;
        let activeThinking: ActiveThinkingSnapshot[] = [];
        let pageCount = 0;
        while (pageCount < SESSION_LOG_BOOTSTRAP_MAX_PAGES) {
          const result = await api.conversations.sessionEvents(conversationId, sessionId, afterSeq, SESSION_LOG_PAGE_SIZE);
          nextEvents.push(...(result.items ?? []));
          activeThinking = result.active_thinking ?? [];
          finalLastSeq = getNextHistoryAfterSeq(result);
          pageCount += 1;
          if (!result.has_more || result.items.length === 0) break;
          if (finalLastSeq === 0 || finalLastSeq === afterSeq) break;
          afterSeq = finalLastSeq;
        }
        if (nextEvents.length > 0) {
          events = [...events, ...nextEvents];
        }
        timeline = applyActiveThinkingSnapshots(normalizeHistory(events), activeThinking);
        if (nextEvents.length > 0 || activeThinking.length > 0 || timeline.some((item) => item.kind === 'thinking' && item.streaming)) {
          await tick();
          if (shouldFollow) scrollToBottom(true);
        }
        lastSeq = finalLastSeq;
      }
      pollDelayMs = SESSION_LOG_POLL_INTERVAL_MS;
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      pollDelayMs = nextPollDelayMs(pollDelayMs);
    } finally {
      loading = false;
      initialLoadDone = true;
    }
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') onclose();
  }

  function handleBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) onclose();
  }

  let pollTimer: number | null = null;

  onMount(() => {
    void loadEvents();
    const schedule = (): void => {
      pollTimer = window.setTimeout(async () => {
        if (!document.hidden) {
          await loadEvents();
        }
        schedule();
      }, pollDelayMs);
    };
    schedule();
    return () => {
      if (pollTimer !== null) window.clearTimeout(pollTimer);
    };
  });

  $effect(() => {
    if (shouldShowTodoDrawer) {
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
</script>

<svelte:window onkeydown={handleKeydown} />

<!--
  Session logs drawer. The overlay is bounded by the shared app-shell
  header/tab offsets so the panel lives entirely inside the visible
  viewport instead of tucking under the mobile chrome.
-->
<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="app-viewport-overlay z-[90] flex justify-end overflow-hidden bg-black/60" onclick={handleBackdropClick} role="presentation">
  <aside class="ml-auto flex h-full min-h-0 w-full max-w-2xl flex-col overflow-hidden border-l border-slate-700 bg-slate-900 shadow-2xl animate-slide-in-right">
    <div class="flex items-center justify-between border-b border-slate-800 px-4 py-3">
      <div class="flex min-w-0 items-center gap-3">
        {#if onBack}
          <Button size="sm" variant="ghost" class="shrink-0" onclick={onBack} title={`Back to ${backLabel}`}>
            <ArrowLeft class="mr-1.5 h-3.5 w-3.5" />
            Back
          </Button>
        {/if}
        <div class="min-w-0">
          <p class="text-xs uppercase tracking-widest text-slate-500">{onBack ? 'Sub-session logs' : 'Session logs'}</p>
          <h3 class="truncate text-sm font-semibold text-white">{stepName || sessionId}</h3>
          <div class="mt-2">
            <LiveDots inline={true} size="sm" tone={userScrolledUp ? 'slate' : 'sky'} label={userScrolledUp ? 'Live follow paused' : 'Following latest'} />
          </div>
        </div>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <Button size="sm" variant="secondary" onclick={() => loadEvents(true)}>Refresh</Button>
        <button
          class="inline-flex h-9 w-9 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-800 hover:text-white"
          onclick={onclose}
          aria-label="Close"
          type="button"
        >
          &times;
        </button>
      </div>
    </div>

    {#if escalations.length > 0}
      {@const activeEscalation = escalations[0]}
      <div class="border-b border-slate-800 px-4 py-3">
        <EscalationPrompt
          item={activeEscalation}
          secondsRemaining={escalationSecondsRemaining(activeEscalation)}
          pending={escalationBusyCallId === activeEscalation.call_id}
          queuedCount={escalations.length - 1}
          onApprove={() => resolveEscalation(activeEscalation.call_id, 'approve')}
          onDeny={() => resolveEscalation(activeEscalation.call_id, 'deny')}
        />
      </div>
    {/if}

    <div
      class="relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain px-4 py-4 pb-4"
      bind:this={timelineEl}
      onscroll={handleTimelineScroll}
    >
      <div bind:this={timelineContentEl} class="space-y-4">
        {#if loading}
          <LoadingState />
        {:else if error}
          <p class="text-sm text-rose-400">{error}</p>
        {:else if timeline.length === 0}
          <p class="text-sm text-slate-500">No events recorded yet.</p>
        {:else}
            {#each timeline as item (item.id)}
            {#if item.kind === 'message'}
              <div class={`flex ${item.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <ChatMessage {item} {agent} compact />
              </div>
            {:else if item.kind === 'thinking'}
              <ThinkingBlock item={item as ThinkingTimelineItem} compact />
            {:else if item.kind === 'tool_call'}
              <ToolCallBlock {item} />
            {:else if item.kind === 'delegation'}
              <DelegationCard {item} {onViewSession} />
            {:else if item.kind === 'notice'}
              <div class="rounded-xl border border-slate-800/60 bg-slate-900/50 px-3 py-2 text-xs text-slate-400">
                <p class="font-medium">{item.title}</p>
                {#if item.description}<p class="mt-1 opacity-75">{item.description}</p>{/if}
              </div>
            {:else if item.kind === 'system_message'}
              <div class="rounded-xl border border-sky-900/50 bg-sky-950/30 px-3 py-2 text-sm text-sky-100/90">
                {item.text}
              </div>
            {:else if item.kind === 'compaction'}
              <div class="rounded-xl border border-sky-900/40 bg-sky-950/20 px-3 py-3 text-sm text-sky-100/90">
                <p class="font-medium">Conversation compacted</p>
                <p class="mt-1 text-sky-100/70">{item.summaryPreview}</p>
              </div>
            {/if}
          {/each}
        {/if}
      </div>

      {#if userScrolledUp}
        <button class="sticky bottom-2 left-1/2 z-10 -translate-x-1/2 rounded-full border border-slate-700 bg-slate-900/90 p-2 shadow-lg transition hover:bg-slate-800" onclick={jumpToBottom} type="button" title="Scroll to latest">
          <ArrowDown class="h-4 w-4 text-slate-300" />
        </button>
      {:else if !loading && !error && isSessionLive(currentSessionStatus)}
        <div class="sticky bottom-2 left-1/2 z-10 w-fit -translate-x-1/2">
          <LiveDots label="Reading latest logs" size="sm" />
        </div>
      {/if}
    </div>

    {#if shouldShowTodoDrawer}
      <div bind:this={footerChromeEl} class="shrink-0 border-t border-slate-800/80 px-4 py-3">
        <div class="rounded-xl border border-slate-800/60 bg-slate-900/40">
          <button
            class="flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left text-sm transition hover:bg-slate-800/40"
            onclick={() => { chatTodoDrawerOpen = !chatTodoDrawerOpen; }}
            type="button"
          >
            <span class="truncate text-slate-300">
              <span class="font-medium text-slate-200">Todos</span>
              <span class="text-slate-500"> · {activeSessionTodos.length} active{#if todoCounts.inProgress > 0} · {todoCounts.inProgress} in progress{/if}{#if todoCounts.pending > 0} · {todoCounts.pending} pending{/if}</span>
            </span>
            {#if chatTodoDrawerOpen}
              <ChevronUp class="h-3.5 w-3.5 shrink-0 text-slate-500" />
            {:else}
              <ChevronDown class="h-3.5 w-3.5 shrink-0 text-slate-500" />
            {/if}
          </button>
          {#if chatTodoDrawerOpen}
            <ul class="divide-y divide-slate-800/40 border-t border-slate-800/60">
              {#each sessionTodos as todo}
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
      </div>
    {/if}
  </aside>
</div>

<style>
  @keyframes slide-in-right {
    from { transform: translateX(100%); }
    to { transform: translateX(0); }
  }
  .animate-slide-in-right {
    animation: slide-in-right 0.25s ease-out forwards;
  }
</style>
