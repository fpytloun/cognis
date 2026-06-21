<script lang="ts">
  import { onMount, tick } from 'svelte';
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';

  import { api, asApiError } from '$lib/api/client';
  import {
    nextPollDelayMs,
    SESSION_LOG_POLL_INTERVAL_MS
  } from '$lib/chat-page';
  import { loadSessionLog, refreshSessionLog, type SessionLogState } from '$lib/session-log';
  import { latestTodoSnapshot, type TimelineItem } from '$lib/chat';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import TimelineTodoDrawer from '$lib/components/timeline/TimelineTodoDrawer.svelte';
  import TimelineViewport from '$lib/components/timeline/TimelineViewport.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import type { Agent, Escalation } from '$lib/types/api';

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
  let timeline = $state<TimelineItem[]>([]);
  let sessionLog = $state<SessionLogState | null>(null);
  let pollDelayMs = $state(SESSION_LOG_POLL_INTERVAL_MS);
  let currentSessionStatus = $state<string | null>(null);
  let chatTodoDrawerOpen = $state(true);
  let escalations = $state<Escalation[]>([]);
  let escalationBusyCallId = $state<string | null>(null);

  let initialLoadDone = $state(false);
  let timelineEl = $state<HTMLDivElement | null>(null);
  let timelineContentEl = $state<HTMLDivElement | null>(null);
  let userScrolledUp = $state(false);
  let sessionTodos = $derived.by(() => latestTodoSnapshot(timeline));
  let shouldShowTodoDrawer = $derived(sessionTodos.length > 0);

  const liveSessionStates = new Set(['active', 'idle', 'running']);

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

      if (refresh || !initialLoadDone || !sessionLog) {
        const result = await loadSessionLog(
          sessionId,
          (afterSeq, limit) => api.conversations.sessionEvents(conversationId, sessionId, afterSeq, limit),
        );
        sessionLog = result;
        timeline = result.timeline;
        await tick();
        userScrolledUp = false;
      } else {
        const result = await refreshSessionLog(
          sessionLog,
          (afterSeq, limit) => api.conversations.sessionEvents(conversationId, sessionId, afterSeq, limit),
        );
        const previousTimelineLength = timeline.length;
        sessionLog = result;
        timeline = result.timeline;
        if (timeline.length !== previousTimelineLength || result.activeThinking.length > 0 || timeline.some((item) => item.kind === 'thinking' && item.streaming)) {
          await tick();
        }
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

    <TimelineViewport
      items={timeline}
      {agent}
      compact
      {loading}
      {error}
      emptyLabel="No events recorded yet."
      live={isSessionLive(currentSessionStatus)}
      bind:viewportElement={timelineEl}
      bind:contentElement={timelineContentEl}
      bind:userScrolledUp={userScrolledUp}
      {onViewSession}
    />

    {#if shouldShowTodoDrawer}
      <div class="shrink-0 border-t border-slate-800/80 px-4 py-3">
        <TimelineTodoDrawer todos={sessionTodos} bind:open={chatTodoDrawerOpen} />
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
