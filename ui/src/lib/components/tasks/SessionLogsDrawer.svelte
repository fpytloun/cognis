<script lang="ts">
  import { onMount } from 'svelte';
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';
  import { api } from '$lib/api/client';
  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';
  import SessionDetailsPanel from '$lib/components/session/SessionDetailsPanel.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import type { Agent, Escalation, Notification, StepRun } from '$lib/types/api';
  import type { TimelineScope } from '$lib/chat-v2/types';

  let {
    conversationId,
    sessionId,
    stepRunId = null,
    taskId = null,
    stepName = '',
    agent = null,
    stepRun = null,
    backLabel = 'Parent session',
    onBack,
    onViewSession,
    onclose
  } = $props<{
    conversationId: string;
    sessionId: string;
    stepRunId?: string | null;
    taskId?: string | null;
    stepName?: string;
    agent?: Agent | null;
    stepRun?: StepRun | null;
    backLabel?: string;
    onBack?: (() => void) | undefined;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    onclose: () => void;
  }>();

  let panel = $state<HTMLElement | null>(null);
  let overlayId = $state<string | null>(null);
  let previousFocus: HTMLElement | null = null;
  let runtimeActive = $state(false);
  let escalations = $state<Escalation[]>([]);
  let escalationBusy = $state<string | null>(null);
  let sessionDetailsOpen = $state(false);

  const scope = $derived.by<TimelineScope>(() => stepRunId && taskId
    ? {
        key: `task_step:${stepRunId}`,
        kind: 'task_step',
        conversation_id: conversationId,
        session_id: sessionId,
        task_id: taskId,
        step_run_id: stepRunId,
        label: stepName
      }
    : {
        key: `session:${sessionId}`,
        kind: 'session',
        conversation_id: conversationId,
        session_id: sessionId,
        label: stepName
      });

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      onclose();
      return;
    }
    if (event.key !== 'Tab' || !panel) return;
    const elements = Array.from(panel.querySelectorAll<HTMLElement>('button, [href], [tabindex]:not([tabindex="-1"])'))
      .filter((element) => !element.hasAttribute('disabled'));
    if (elements.length === 0) return;
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) onclose();
  }

  function notificationEscalation(item: Notification): Escalation | null {
    if (item.notification_type !== 'escalation' || item.status !== 'pending' || item.session_id !== sessionId) return null;
    return {
      call_id: item.notification_id,
      session_id: item.session_id,
      tool_name: typeof item.payload.tool_name === 'string' ? item.payload.tool_name : null,
      arguments_display:
        item.payload.arguments_display
        && typeof item.payload.arguments_display === 'object'
        && !Array.isArray(item.payload.arguments_display)
          ? item.payload.arguments_display as Record<string, unknown>
          : null,
      decision: 'escalate',
      resolved: false,
      reasoning: typeof item.payload.reasoning === 'string' ? item.payload.reasoning : null,
      risk: typeof item.payload.risk === 'string' ? item.payload.risk : null,
      timeout_seconds: typeof item.payload.timeout_seconds === 'number' ? item.payload.timeout_seconds : 300,
      received_at: item.created_at ? Date.parse(item.created_at) : Date.now(),
    };
  }

  async function refreshEscalations(): Promise<void> {
    const notifications = await api.notifications.list(conversationId, { sessionId });
    escalations = notifications
      .map(notificationEscalation)
      .filter((item): item is Escalation => item !== null)
      .sort((left, right) => (left.received_at ?? 0) - (right.received_at ?? 0));
  }

  function secondsRemaining(item: Escalation): number {
    return Math.max(Math.ceil((item.timeout_seconds ?? 300) - (Date.now() - (item.received_at ?? Date.now())) / 1000), 0);
  }

  async function resolveEscalation(item: Escalation, decision: 'approve' | 'deny'): Promise<void> {
    escalationBusy = item.call_id;
    try {
      await api.notifications.resolve(item.call_id, { decision });
      await refreshEscalations();
    } finally {
      escalationBusy = null;
    }
  }

  const activityLabel = $derived(stepRun
    ? stepRun.status === 'evaluating'
      ? 'Evaluator is reviewing…'
      : stepRun.status === 'running'
        ? 'Agent is working…'
        : stepRun.status || 'Paused'
    : runtimeActive
      ? 'Agent is working…'
      : 'Paused'
  );
  const activityLive = $derived(stepRun
    ? stepRun.status === 'running' || stepRun.status === 'evaluating'
    : runtimeActive
  );

  onMount(() => {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const handle = registerOverlay({ kind: 'sheet', blocksChrome: true });
    overlayId = handle.id;
    queueMicrotask(() => panel?.querySelector<HTMLElement>('button')?.focus());
    void refreshEscalations();
    return () => {
      handle.unregister();
      overlayId = null;
      queueMicrotask(() => previousFocus?.focus());
    };
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="app-viewport-overlay z-[90] flex justify-end overflow-hidden bg-black/60" onclick={handleBackdropClick} role="presentation">
  <div bind:this={panel} class="app-safe-side-panel ml-auto flex h-full min-h-0 w-full max-w-2xl flex-col overflow-hidden overscroll-contain border-l border-slate-700 bg-slate-900 shadow-2xl" role="dialog" aria-modal="true" aria-label="Session logs">
    <div class="flex shrink-0 items-center justify-between border-b border-slate-800 px-4 py-3">
      <div class="flex min-w-0 items-center gap-3">
        {#if onBack}
          <Button size="sm" variant="ghost" class="shrink-0" onclick={onBack} title={`Back to ${backLabel}`}>
            <ArrowLeft class="mr-1.5 h-3.5 w-3.5" /> Back
          </Button>
        {/if}
        <div class="min-w-0">
          <p class="text-xs uppercase tracking-widest text-slate-500">{onBack ? 'Sub-session logs' : 'Session logs'}</p>
          <h3 class="truncate text-sm font-semibold text-white">{stepName || sessionId}</h3>
          <div class="mt-1 text-xs text-slate-400" data-testid="session-activity-status">
            {#if activityLive}<LiveDots inline size="sm" label={activityLabel} />{:else}{activityLabel}{/if}
          </div>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <SessionDetailsButton
          open={sessionDetailsOpen}
          ariaControls={`session-logs-info-${sessionId}`}
          testId="session-logs-header-info"
          onclick={() => { sessionDetailsOpen = !sessionDetailsOpen; }}
        />
        <button class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-700 text-slate-400 hover:bg-slate-800 hover:text-white sm:h-8 sm:w-8" onclick={onclose} aria-label="Close" type="button">&times;</button>
      </div>
    </div>
    {#if sessionDetailsOpen}
      <div id={`session-logs-info-${sessionId}`}>
        <SessionDetailsPanel {sessionId} />
      </div>
    {/if}
    {#if escalations.length > 0}
      {@const escalation = escalations[0]}
      <div class="shrink-0 border-b border-slate-800 px-4 py-3">
        <EscalationPrompt
          item={escalation}
          secondsRemaining={secondsRemaining(escalation)}
          pending={escalationBusy === escalation.call_id}
          queuedCount={escalations.length - 1}
          onApprove={() => resolveEscalation(escalation, 'approve')}
          onDeny={() => resolveEscalation(escalation, 'deny')}
        />
      </div>
    {/if}
    <ScopedChatV2Timeline
      {scope}
      {agent}
      {onViewSession}
      activityStatus={stepRun?.status ?? ''}
      {stepRun}
      onRuntimeActiveChange={(active) => { runtimeActive = active; }}
      emptyLabel="No events recorded yet."
    />
  </div>
</div>
