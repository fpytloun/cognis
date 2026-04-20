<script lang="ts">
  import { onMount, tick } from 'svelte';
  import ArrowDown from 'lucide-svelte/icons/arrow-down';

  import { api, asApiError } from '$lib/api/client';
  import {
    nextPollDelayMs,
    SESSION_LOG_BOOTSTRAP_MAX_PAGES,
    SESSION_LOG_PAGE_SIZE,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { normalizeHistory, type TimelineItem } from '$lib/chat';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import DelegationCard from '$lib/components/DelegationCard.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import ReasoningBlock from '$lib/components/ReasoningBlock.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import type { MessageEvent } from '$lib/types/api';

  let {
    conversationId,
    sessionId,
    stepName = '',
    onclose
  } = $props<{
    conversationId: string;
    sessionId: string;
    stepName?: string;
    onclose: () => void;
  }>();

  let loading = $state(true);
  let error = $state('');
  let events = $state<MessageEvent[]>([]);
  let timeline = $state<TimelineItem[]>([]);
  let lastSeq = $state(0);
  let pollDelayMs = $state(SESSION_LOG_POLL_INTERVAL_MS);
  let currentSessionStatus = $state<string | null>(null);

  let initialLoadDone = $state(false);
  let timelineEl = $state<HTMLDivElement | null>(null);
  let userScrolledUp = $state(false);
  let programmaticScroll = false;

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

  function hasStreamingTimelineItem(items: TimelineItem[]): boolean {
    return items.some((item) => {
      if (item.kind === 'message' || item.kind === 'reasoning') {
        return item.streaming === true;
      }
      return false;
    });
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

      if (refresh || !initialLoadDone) {
        const history: MessageEvent[] = [];
        let afterSeq = 0;
        let pageCount = 0;
        let finalLastSeq = 0;
        while (pageCount < SESSION_LOG_BOOTSTRAP_MAX_PAGES) {
          const result = await api.conversations.sessionEvents(conversationId, sessionId, afterSeq, SESSION_LOG_PAGE_SIZE);
          history.push(...(result.items ?? []));
          finalLastSeq = result.last_seq;
          pageCount += 1;
          if (!result.has_more || result.items.length === 0) break;
          afterSeq = result.last_seq;
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
        timeline = normalizeHistory(history);
        await tick();
        userScrolledUp = false;
        scrollToBottom(true);
      } else {
        const result = await api.conversations.sessionEvents(conversationId, sessionId, lastSeq, SESSION_LOG_PAGE_SIZE);
        if ((result.items ?? []).length > 0) {
          const shouldFollow = !userScrolledUp;
          events = [...events, ...(result.items ?? [])];
          timeline = normalizeHistory(events);
          await tick();
          if (shouldFollow) scrollToBottom(true);
        }
        lastSeq = result.last_seq;
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
      <div class="min-w-0">
        <p class="text-xs uppercase tracking-widest text-slate-500">Session logs</p>
        <h3 class="truncate text-sm font-semibold text-white">{stepName || sessionId}</h3>
        <div class="mt-2">
          <LiveDots inline={true} size="sm" tone={userScrolledUp ? 'amber' : 'sky'} label={userScrolledUp ? 'Live follow paused' : 'Following latest'} />
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

    <div
      class="relative min-h-0 flex-1 space-y-4 overflow-y-auto overflow-x-hidden overscroll-contain px-4 py-4 pb-4"
      bind:this={timelineEl}
      onscroll={handleTimelineScroll}
    >
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
              <ChatMessage {item} compact />
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
          {:else if item.kind === 'system_message'}
            <div class="rounded-xl border border-amber-900/50 bg-amber-950/30 px-3 py-2 text-sm text-amber-100/90">
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

      {#if userScrolledUp}
        <button class="sticky bottom-2 left-1/2 z-10 -translate-x-1/2 rounded-full border border-slate-700 bg-slate-900/90 p-2 shadow-lg transition hover:bg-slate-800" onclick={jumpToBottom} type="button" title="Scroll to latest">
          <ArrowDown class="h-4 w-4 text-slate-300" />
        </button>
      {:else if !loading && !error && currentSessionStatus === 'active' && !hasStreamingTimelineItem(timeline)}
        <div class="sticky bottom-2 left-1/2 z-10 w-fit -translate-x-1/2">
          <LiveDots label="Reading latest logs" size="sm" />
        </div>
      {/if}
    </div>
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
